use crate::{constants::MIHOMO_BINARY, protocol::{ProviderFile, Request}, system_proxy,
    util::{atomic_write, copy_dir, remove_any, safe_provider_name, sha256_bytes, AppResult}};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use std::{fs::{self, File}, os::windows::process::CommandExt, io::{Read, Seek, SeekFrom}, path::{Path, PathBuf},
    process::{Command, Stdio}, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::{Duration, Instant, UNIX_EPOCH}};
const MAX_CONFIG_BYTES: usize = 4 * 1024 * 1024;
const MAX_PROVIDER_BYTES: usize = 10 * 1024 * 1024;

pub struct PreparedApply {
    pub request: Request,
    pub id: String,
    pub base: PathBuf,
    pub cancelled: Arc<AtomicBool>,
    pub deadline: Instant,
}
impl PreparedApply {
    pub fn root(&self) -> PathBuf { self.base.join("transactions").join(&self.id) }
    pub fn prepare(&self) -> AppResult<()> {
        let result = self.prepare_inner();
        if result.is_err() { let _ = remove_any(&self.root()); }
        result
    }
    fn prepare_inner(&self) -> AppResult<()> {
        self.check_current()?;
        if !matches!(self.request.runtime_mode.as_str(), "active" | "standby") {
            return Err("路由服务运行模式无效".into());
        }
        let config = BASE64.decode(self.request.config_b64.as_bytes())
            .map_err(|_| "服务配置编码无效")?;
        if config.is_empty() || config.len() > MAX_CONFIG_BYTES {
            return Err("服务配置大小超出限制".into());
        }
        if !sha256_bytes(&config).eq_ignore_ascii_case(&self.request.config_sha256) {
            return Err("服务配置校验失败".into());
        }
        let bypass = system_proxy::normalize_bypass_domains(&self.request.system_proxy_bypass_domains)?;
        let candidate = self.root().join("candidate");
        let data = candidate.join("data");
        fs::create_dir_all(&data).map_err(|e| format!("创建候选目录失败: {e}"))?;
        copy_dir(&self.base.join("data"), &data)?;
        self.check_current()?;
        stage_providers(&data, &self.request.providers)?;
        atomic_write(&candidate.join("config.json"), &config)?;
        atomic_write(&candidate.join("system-proxy-bypass.json"),
            &serde_json::to_vec(&bypass).map_err(|_| "绕过域名序列化失败")?)?;
        self.check_current()?;
        let log_path = candidate.join("preflight.log");
        let output = File::create(&log_path).map_err(|e| format!("预检日志创建失败: {e}"))?;
        let error = output.try_clone().map_err(|e| format!("预检日志句柄失败: {e}"))?;
        let mut command = Command::new(self.base.join(MIHOMO_BINARY));
        command.args(["-t", "-d"]).arg(&data).arg("-f").arg(candidate.join("config.json"))
            .stdin(Stdio::null()).stdout(output).stderr(error).creation_flags(0x0800_0000);
        let status = run_bounded(&mut command,
            self.deadline.saturating_duration_since(Instant::now()).min(Duration::from_secs(30)),
            &self.cancelled)?;
        if !status.success() {
            let mut tail = Vec::new();
            if let Ok(mut log) = File::open(log_path) {
                let start = log.metadata().map(|m| m.len().saturating_sub(65536)).unwrap_or(0);
                let _ = log.seek(SeekFrom::Start(start));
                let _ = log.take(65536).read_to_end(&mut tail);
            }
            let text = String::from_utf8_lossy(&tail);
            let detail = text.lines().last().unwrap_or("未知错误");
            return Err(format!("Mihomo 配置预检失败: {}", crate::manager::sanitize_detail(detail)));
        }
        Ok(())
    }
    fn check_current(&self) -> AppResult<()> {
        if self.cancelled.load(Ordering::Relaxed) { return Err("配置预检已取消".into()); }
        if Instant::now() >= self.deadline { return Err("配置准备超过总期限（45 秒）".into()); }
        Ok(())
    }
}

pub fn run_bounded(command: &mut Command, timeout: Duration, cancelled: &AtomicBool)
    -> AppResult<std::process::ExitStatus> {
    if cancelled.load(Ordering::Relaxed) { return Err("配置预检已取消".into()); }
    let mut child = command.spawn().map_err(|e| format!("无法执行 Mihomo 配置预检: {e}"))?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {},
            Err(e) => { let _ = child.kill(); let _ = child.wait(); return Err(format!("读取预检进程失败: {e}")); }
        }
        if cancelled.load(Ordering::Relaxed) || Instant::now() >= deadline {
            let _ = child.kill(); let _ = child.wait();
            return Err(if cancelled.load(Ordering::Relaxed) { "配置预检已取消" } else { "Mihomo 配置预检超时（30 秒）" }.into());
        }
        std::thread::sleep(Duration::from_millis(25));
    }

}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preflight_deadline_kills_only_its_child() {
        let mut command = Command::new("powershell.exe");
        command.args(["-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 10"])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .creation_flags(0x0800_0000);
        let started = Instant::now();
        let result = run_bounded(&mut command, Duration::from_millis(60), &AtomicBool::new(false));
        assert!(result.unwrap_err().contains("超时"));
        assert!(started.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn cancellation_does_not_launch_preflight() {
        let mut command = Command::new("nonexistent-cxvpn-test.exe");
        assert!(run_bounded(&mut command, Duration::from_secs(1), &AtomicBool::new(true))
            .unwrap_err().contains("取消"));
    }
}

fn stage_providers( candidate_data: &Path, providers: &[ProviderFile]) -> AppResult<()> {
        let provider_dir = candidate_data.join("providers");
        fs::create_dir_all(&provider_dir).map_err(|e| format!("创建 provider 目录失败: {e}"))?;
        for provider in providers {
            if !safe_provider_name(&provider.name) {
                return Err("provider 文件名不合法".to_string());
            }
            let content = BASE64
                .decode(provider.content_b64.as_bytes())
                .map_err(|_| "provider 缓存编码无效".to_string())?;
            if content.is_empty() || content.len() > MAX_PROVIDER_BYTES {
                return Err(format!("provider 缓存大小超出限制: {}", provider.name));
            }
            if !sha256_bytes(&content).eq_ignore_ascii_case(&provider.sha256) {
                return Err(format!("provider 缓存校验失败: {}", provider.name));
            }
            let target = provider_dir.join(&provider.name);
            let target_mtime = target
                .metadata()
                .ok()
                .and_then(|value| value.modified().ok())
                .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
                .map(|value| value.as_secs())
                .unwrap_or(0);
            if !target.exists() || provider.modified_at >= target_mtime {
                atomic_write(&target, &content)?;
            }
        }
        Ok(())
    }
