use crate::{
    constants::{
        MIHOMO_BINARY, MIHOMO_SHA256, PROTOCOL_VERSION, SERVICE_VERSION, TRANSACTION_TIMEOUT_SECS,
    },
    protocol::{ProviderFile, Request, Response},
    system_proxy,
    util::{
        atomic_write, copy_dir, remove_any, safe_provider_name, sha256_bytes, sha256_file,
        transaction_id, AppResult,
    },
};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    fs::{self, OpenOptions},
    io::Write,
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const MAX_CONFIG_BYTES: usize = 4 * 1024 * 1024;
const MAX_PROVIDER_BYTES: usize = 10 * 1024 * 1024;
const SYSTEM_PROXY_BYPASS_FILE: &str = "system-proxy-bypass.json";

#[derive(Debug)]
struct PendingTransaction {
    id: String,
    rollback_dir: PathBuf,
    had_config: bool,
    had_data: bool,
    had_marker: bool,
    had_mode_marker: bool,
    had_fast_toggle_marker: bool,
    had_system_proxy_bypass: bool,
    deadline: Instant,
}

#[derive(Debug, Serialize, Deserialize)]
struct PendingRecord {
    id: String,
    rollback_dir: String,
    had_config: bool,
    had_data: bool,
    had_marker: bool,
    #[serde(default)]
    had_mode_marker: bool,
    #[serde(default)]
    had_fast_toggle_marker: bool,
    #[serde(default)]
    had_system_proxy_bypass: bool,
}

pub struct RuntimeManager {
    base: PathBuf,
    child: Option<Child>,
    pending: Option<PendingTransaction>,
    last_restart: Instant,
    owner_sid: String,
    crash_times: Vec<Instant>,
    crash_fused: bool,
}

impl RuntimeManager {
    pub fn new(base: PathBuf, owner_sid: String) -> AppResult<Self> {
        fs::create_dir_all(&base).map_err(|e| format!("创建服务目录失败: {e}"))?;
        Self::recover_interrupted(&base)?;
        let mut manager = Self {
            base,
            child: None,
            pending: None,
            last_restart: Instant::now() - Duration::from_secs(30),
            owner_sid,
            crash_times: Vec::new(),
            crash_fused: false,
        };
        // v0.3 及更早的 active 系统代理配置本身就是完整运行配置；升级时
        // 可安全补写快切标记。旧 standby 使用精简配置，必须经控制面重建。
        if manager.marker_path().is_file()
            && manager.runtime_mode() == "active"
            && manager.fast_toggle_marker_path().is_file() == false
            && manager.desired_proxy_port().ok().flatten().is_some()
        {
            let _ = atomic_write(&manager.fast_toggle_marker_path(), b"ready\n");
        }
        if manager.marker_path().is_file() && manager.config_path().is_file() {
            manager.start_runtime()?;
            if let Err(error) = manager.reconcile_system_proxy() {
                let _ = manager.stop_runtime();
                let _ = system_proxy::restore(&manager.base, &manager.owner_sid);
                return Err(error);
            }
        } else {
            system_proxy::restore(&manager.base, &manager.owner_sid)?;
        }
        Ok(manager)
    }

    pub fn handle(&mut self, request: Request) -> Response {
        let result = match request.op.as_str() {
            "status" => self.status(),
            "diagnostics" => self.diagnostics(),
            "apply" => self.apply(request),
            "commit" => self.commit(&request.transaction_id),
            "rollback" => self.rollback(&request.transaction_id),
            "activate_system_proxy" => self.activate_system_proxy(&request.transaction_id),
            "set_system_proxy_enabled" => self.set_system_proxy_enabled(request.enabled),
            "read_provider" => self.read_provider(&request.provider_name),
            "stop_runtime" => self.stop_runtime_command(),
            "start_runtime" => self.start_runtime_command(),
            _ => Err("不支持的服务指令".to_string()),
        };
        match result {
            Ok((message, data)) => Response::success(message, data),
            Err(error) => Response::error(error),
        }
    }

    pub fn tick(&mut self) {
        if self
            .pending
            .as_ref()
            .is_some_and(|item| Instant::now() >= item.deadline)
        {
            let id = self
                .pending
                .as_ref()
                .map(|item| item.id.clone())
                .unwrap_or_default();
            let _ = self.rollback(&id);
            self.log("配置事务等待提交超时，已自动回滚");
            return;
        }
        let exited = self
            .child
            .as_mut()
            .and_then(|child| child.try_wait().ok().flatten())
            .is_some();
        if exited {
            self.child = None;
            self.log("Mihomo 进程异常退出");
            let _ = system_proxy::restore(&self.base, &self.owner_sid);
            let now = Instant::now();
            self.crash_times
                .retain(|stamp| now.duration_since(*stamp) <= Duration::from_secs(60));
            self.crash_times.push(now);
            if self.crash_times.len() >= 3 {
                let _ = remove_any(&self.marker_path());
                let _ = remove_any(&self.mode_path());
                self.crash_fused = true;
                self.log("Mihomo 60 秒内连续异常退出 3 次，已熔断并恢复系统代理");
                return;
            }
        }
        if self.child.is_none()
            && self.pending.is_none()
            && self.marker_path().is_file()
            && self.config_path().is_file()
            && self.last_restart.elapsed() >= Duration::from_secs(10)
        {
            if self.start_runtime().is_ok() {
                let _ = self.reconcile_system_proxy();
            }
        }
    }

    pub fn shutdown(&mut self) {
        let _ = self.stop_runtime();
        let _ = system_proxy::restore(&self.base, &self.owner_sid);
    }

    fn status(&mut self) -> AppResult<(String, serde_json::Value)> {
        let running = match self.child.as_mut() {
            Some(child) => match child.try_wait() {
                Ok(None) => true,
                Ok(Some(_)) => {
                    self.child = None;
                    false
                }
                Err(_) => false,
            },
            None => false,
        };
        let hash = if self.config_path().is_file() {
            sha256_file(&self.config_path()).unwrap_or_default()
        } else {
            String::new()
        };
        let service_hash = std::env::current_exe()
            .ok()
            .and_then(|path| sha256_file(&path).ok())
            .unwrap_or_default();
        Ok((
            "服务状态已读取".to_string(),
            json!({
                "protocol_version": PROTOCOL_VERSION,
                "service_version": SERVICE_VERSION,
                "service_sha256": service_hash,
                "runtime_running": running,
                "runtime_enabled": self.marker_path().is_file(),
                "runtime_mode": self.runtime_mode(),
                "mihomo_pid": self.child.as_ref().map(|child| child.id()),
                "pending_transaction": self.pending.as_ref().map(|item| item.id.as_str()),
                "config_sha256": hash,
                "system_proxy_active": system_proxy::is_active(&self.base),
                "fast_toggle_ready": self.fast_toggle_marker_path().is_file(),
                "crash_fused": self.crash_fused,
            }),
        ))
    }

    fn apply(&mut self, request: Request) -> AppResult<(String, serde_json::Value)> {
        self.log("收到配置 apply 请求，开始校验候选文件");
        if self.pending.is_some() {
            return Err("已有配置事务等待提交，请稍后重试".to_string());
        }
        self.crash_fused = false;
        self.crash_times.clear();
        let runtime_mode = match request.runtime_mode.as_str() {
            "active" => "active",
            "standby" => "standby",
            _ => return Err("路由服务运行模式无效".to_string()),
        };
        let config = BASE64
            .decode(request.config_b64.as_bytes())
            .map_err(|_| "服务配置编码无效".to_string())?;
        if config.is_empty() || config.len() > MAX_CONFIG_BYTES {
            return Err("服务配置大小超出限制".to_string());
        }
        if !sha256_bytes(&config).eq_ignore_ascii_case(&request.config_sha256) {
            return Err("服务配置校验失败".to_string());
        }
        let bypass_domains =
            system_proxy::normalize_bypass_domains(&request.system_proxy_bypass_domains)?;

        let id = transaction_id();
        let transaction_root = self.transactions_dir().join(&id);
        let candidate = transaction_root.join("candidate");
        let rollback = transaction_root.join("rollback");
        let candidate_data = candidate.join("data");
        fs::create_dir_all(&candidate_data).map_err(|e| format!("创建候选目录失败: {e}"))?;
        copy_dir(&self.data_path(), &candidate_data)?;
        self.stage_providers(&candidate_data, &request.providers)?;
        atomic_write(&candidate.join("config.json"), &config)?;
        atomic_write(
            &candidate.join(SYSTEM_PROXY_BYPASS_FILE),
            serde_json::to_vec(&bypass_domains)
                .map_err(|e| format!("序列化系统代理绕过域名失败: {e}"))?
                .as_slice(),
        )?;
        self.validate_candidate(&candidate)?;
        self.log("候选配置与 provider 缓存校验通过，开始切换 Mihomo 运行时");

        self.stop_runtime()?;
        fs::create_dir_all(&rollback).map_err(|e| format!("创建回滚目录失败: {e}"))?;
        let record = PendingRecord {
            id: id.clone(),
            rollback_dir: rollback.to_string_lossy().into_owned(),
            had_config: self.config_path().exists(),
            had_data: self.data_path().exists(),
            had_marker: self.marker_path().exists(),
            had_mode_marker: self.mode_path().exists(),
            had_fast_toggle_marker: self.fast_toggle_marker_path().exists(),
            had_system_proxy_bypass: self.system_proxy_bypass_path().exists(),
        };
        atomic_write(
            &self.pending_record_path(),
            serde_json::to_vec(&record)
                .map_err(|e| format!("序列化事务失败: {e}"))?
                .as_slice(),
        )?;

        let swap_result = (|| -> AppResult<()> {
            if record.had_config {
                fs::rename(self.config_path(), rollback.join("config.json"))
                    .map_err(|e| format!("备份旧配置失败: {e}"))?;
            }
            if record.had_data {
                fs::rename(self.data_path(), rollback.join("data"))
                    .map_err(|e| format!("备份旧数据失败: {e}"))?;
            }
            if record.had_marker {
                fs::rename(self.marker_path(), rollback.join("enabled.marker"))
                    .map_err(|e| format!("备份运行标记失败: {e}"))?;
            }
            if record.had_mode_marker {
                fs::rename(self.mode_path(), rollback.join("runtime-mode"))
                    .map_err(|e| format!("备份运行模式失败: {e}"))?;
            }
            if record.had_fast_toggle_marker {
                fs::rename(
                    self.fast_toggle_marker_path(),
                    rollback.join("fast-toggle-ready.marker"),
                )
                .map_err(|e| format!("备份快切标记失败: {e}"))?;
            }
            if record.had_system_proxy_bypass {
                fs::rename(
                    self.system_proxy_bypass_path(),
                    rollback.join(SYSTEM_PROXY_BYPASS_FILE),
                )
                .map_err(|e| format!("备份系统代理绕过域名失败: {e}"))?;
            }
            fs::rename(candidate.join("config.json"), self.config_path())
                .map_err(|e| format!("应用新配置失败: {e}"))?;
            fs::rename(
                candidate.join(SYSTEM_PROXY_BYPASS_FILE),
                self.system_proxy_bypass_path(),
            )
            .map_err(|e| format!("应用系统代理绕过域名失败: {e}"))?;
            fs::rename(candidate_data, self.data_path())
                .map_err(|e| format!("应用新数据失败: {e}"))?;
            atomic_write(&self.marker_path(), b"enabled\n")?;
            atomic_write(&self.mode_path(), runtime_mode.as_bytes())?;
            if request.fast_toggle_ready {
                atomic_write(&self.fast_toggle_marker_path(), b"ready\n")?;
            } else {
                let _ = remove_any(&self.fast_toggle_marker_path());
            }
            self.start_runtime()?;
            if runtime_mode == "standby" || self.desired_proxy_port()?.is_none() {
                system_proxy::restore(&self.base, &self.owner_sid)?;
            }
            Ok(())
        })();

        if let Err(error) = swap_result {
            let _ = Self::restore_record(&self.base, &record);
            let _ = remove_any(&transaction_root);
            return Err(format!("新配置启动失败，已恢复旧配置: {error}"));
        }
        self.pending = Some(PendingTransaction {
            id: id.clone(),
            rollback_dir: rollback,
            had_config: record.had_config,
            had_data: record.had_data,
            had_marker: record.had_marker,
            had_mode_marker: record.had_mode_marker,
            had_fast_toggle_marker: record.had_fast_toggle_marker,
            had_system_proxy_bypass: record.had_system_proxy_bypass,
            deadline: Instant::now() + Duration::from_secs(TRANSACTION_TIMEOUT_SECS),
        });
        self.log("候选运行时已启动，等待控制面 commit 或 rollback");
        Ok((
            "新配置已启动，等待控制面确认".to_string(),
            json!({
                "transaction_id": id,
                "config_sha256": request.config_sha256.to_uppercase(),
            }),
        ))
    }

    fn commit(&mut self, transaction_id: &str) -> AppResult<(String, serde_json::Value)> {
        let pending = self.take_pending(transaction_id)?;
        let root = pending
            .rollback_dir
            .parent()
            .map(Path::to_path_buf)
            .ok_or_else(|| "回滚目录异常".to_string())?;
        remove_any(&self.pending_record_path()).map_err(|e| format!("清理事务记录失败: {e}"))?;
        remove_any(&root).map_err(|e| format!("清理回滚目录失败: {e}"))?;
        self.log("配置事务 commit 成功");
        Ok((
            "配置事务已提交".to_string(),
            json!({"transaction_id": transaction_id}),
        ))
    }

    fn rollback(&mut self, transaction_id: &str) -> AppResult<(String, serde_json::Value)> {
        let pending = self.take_pending(transaction_id)?;
        let stop_result = self.stop_runtime();
        let restore_proxy_result = system_proxy::restore(&self.base, &self.owner_sid);
        if let Err(error) = stop_result {
            self.pending = Some(pending);
            return Err(error);
        }
        if let Err(error) = restore_proxy_result {
            self.pending = Some(pending);
            return Err(error);
        }
        let record = PendingRecord {
            id: pending.id.clone(),
            rollback_dir: pending.rollback_dir.to_string_lossy().into_owned(),
            had_config: pending.had_config,
            had_data: pending.had_data,
            had_marker: pending.had_marker,
            had_mode_marker: pending.had_mode_marker,
            had_fast_toggle_marker: pending.had_fast_toggle_marker,
            had_system_proxy_bypass: pending.had_system_proxy_bypass,
        };
        Self::restore_record(&self.base, &record)?;
        let root = pending
            .rollback_dir
            .parent()
            .map(Path::to_path_buf)
            .ok_or_else(|| "回滚目录异常".to_string())?;
        let _ = remove_any(&root);
        if record.had_marker && self.config_path().is_file() {
            self.start_runtime()?;
        }
        self.reconcile_system_proxy()?;
        self.log("配置事务 rollback 成功");
        Ok((
            "已恢复上一份配置".to_string(),
            json!({"transaction_id": transaction_id}),
        ))
    }

    fn stop_runtime_command(&mut self) -> AppResult<(String, serde_json::Value)> {
        if self.pending.is_some() {
            return Err("配置事务尚未提交，不能关闭运行时".to_string());
        }
        let _ = remove_any(&self.marker_path());
        let _ = remove_any(&self.mode_path());
        let stop_result = self.stop_runtime();
        let restore_result = system_proxy::restore(&self.base, &self.owner_sid);
        stop_result?;
        restore_result?;
        Ok((
            "Mihomo 运行时已停止".to_string(),
            json!({"runtime_running": false}),
        ))
    }

    fn diagnostics(&self) -> AppResult<(String, serde_json::Value)> {
        Ok((
            "服务诊断已读取".to_string(),
            json!({
                "service_log": read_sanitized_tail(&self.base.join("service.log"), 30),
                "mihomo_log": read_sanitized_tail(&self.base.join("mihomo.log"), 30),
            }),
        ))
    }

    fn read_provider(&self, provider_name: &str) -> AppResult<(String, serde_json::Value)> {
        if !safe_provider_name(provider_name) {
            return Err("provider 文件名不合法".to_string());
        }
        let path = self.data_path().join("providers").join(provider_name);
        let content = fs::read(&path).map_err(|_| "provider 缓存不存在".to_string())?;
        if content.is_empty() || content.len() > MAX_PROVIDER_BYTES {
            return Err("provider 缓存大小超出限制".to_string());
        }
        let modified_at = path
            .metadata()
            .ok()
            .and_then(|value| value.modified().ok())
            .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
            .map(|value| value.as_secs())
            .unwrap_or(0);
        Ok((
            "provider 缓存已读取".to_string(),
            json!({
                "content_b64": BASE64.encode(content),
                "modified_at": modified_at,
            }),
        ))
    }

    fn start_runtime_command(&mut self) -> AppResult<(String, serde_json::Value)> {
        if self.pending.is_some() {
            return Err("配置事务尚未提交，不能重复启动运行时".to_string());
        }
        if !self.config_path().is_file() {
            return Err("尚未应用可运行的配置".to_string());
        }
        self.crash_fused = false;
        self.crash_times.clear();
        atomic_write(&self.marker_path(), b"enabled\n")?;
        atomic_write(&self.mode_path(), b"active")?;
        self.start_runtime()?;
        self.reconcile_system_proxy()?;
        Ok((
            "Mihomo 运行时已启动".to_string(),
            json!({"runtime_running": true}),
        ))
    }

    fn stage_providers(&self, candidate_data: &Path, providers: &[ProviderFile]) -> AppResult<()> {
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

    fn validate_candidate(&self, candidate: &Path) -> AppResult<()> {
        let output = Command::new(self.mihomo_path())
            .args(["-t", "-d"])
            .arg(candidate.join("data"))
            .arg("-f")
            .arg(candidate.join("config.json"))
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .map_err(|e| format!("无法执行 Mihomo 配置预检: {e}"))?;
        if !output.status.success() {
            let detail = String::from_utf8_lossy(if output.stderr.is_empty() {
                &output.stdout
            } else {
                &output.stderr
            });
            let last = detail.lines().last().unwrap_or("未知错误");
            return Err(format!("Mihomo 配置预检失败: {}", sanitize_detail(last)));
        }
        Ok(())
    }

    fn start_runtime(&mut self) -> AppResult<()> {
        if let Some(child) = self.child.as_mut() {
            if child.try_wait().ok().flatten().is_none() {
                return Ok(());
            }
            self.child = None;
        }
        if !sha256_file(&self.mihomo_path())?.eq_ignore_ascii_case(MIHOMO_SHA256) {
            return Err("Mihomo 运行时完整性校验失败".to_string());
        }
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.base.join("mihomo.log"))
            .map_err(|e| format!("打开 Mihomo 日志失败: {e}"))?;
        let error_log = log
            .try_clone()
            .map_err(|e| format!("复制日志句柄失败: {e}"))?;
        let child = Command::new(self.mihomo_path())
            .arg("-d")
            .arg(self.data_path())
            .arg("-f")
            .arg(self.config_path())
            .current_dir(&self.base)
            .stdin(Stdio::null())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(error_log))
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("启动 Mihomo 失败: {e}"))?;
        self.last_restart = Instant::now();
        self.child = Some(child);
        self.log("Mihomo 运行时已启动");
        Ok(())
    }

    fn activate_system_proxy(
        &mut self,
        transaction_id: &str,
    ) -> AppResult<(String, serde_json::Value)> {
        let pending = self
            .pending
            .as_ref()
            .ok_or_else(|| "没有等待处理的配置事务".to_string())?;
        if transaction_id.is_empty() || pending.id != transaction_id {
            return Err("配置事务标识不匹配".to_string());
        }
        if self.runtime_mode() != "active" {
            return Err("待机核心不能启用 Windows 系统代理".to_string());
        }
        let port = self
            .desired_proxy_port()?
            .ok_or_else(|| "候选配置不是系统代理接管模式".to_string())?;
        let bypass_domains = self.system_proxy_bypass_domains()?;
        system_proxy::activate(&self.base, &self.owner_sid, port, &bypass_domains)?;
        self.log("系统代理快照已保存并切换到 Mihomo mixed-port");
        Ok((
            "系统代理已进入候选事务".to_string(),
            json!({"system_proxy_active": true, "mixed_port": port}),
        ))
    }

    fn set_system_proxy_enabled(
        &mut self,
        enabled: bool,
    ) -> AppResult<(String, serde_json::Value)> {
        if self.pending.is_some() {
            return Err("配置事务尚未提交，不能快速切换系统代理".to_string());
        }
        if !self.fast_toggle_marker_path().is_file() {
            return Err("当前运行配置不支持系统代理快速切换".to_string());
        }
        let running = self
            .child
            .as_mut()
            .is_some_and(|child| child.try_wait().ok().flatten().is_none());
        if !running {
            return Err("Mihomo 常驻核心未运行，不能快速切换系统代理".to_string());
        }
        let port = self
            .desired_proxy_port()?
            .ok_or_else(|| "当前配置不是 Windows 系统代理模式".to_string())?;
        if enabled {
            atomic_write(&self.mode_path(), b"active")?;
            let bypass_domains = self.system_proxy_bypass_domains()?;
            if let Err(error) =
                system_proxy::activate(&self.base, &self.owner_sid, port, &bypass_domains)
            {
                let _ = atomic_write(&self.mode_path(), b"standby");
                return Err(error);
            }
            self.log("Windows 系统代理快速开启，Mihomo 常驻核心未重启");
        } else {
            atomic_write(&self.mode_path(), b"standby")?;
            if let Err(error) = system_proxy::restore(&self.base, &self.owner_sid) {
                let _ = atomic_write(&self.mode_path(), b"active");
                return Err(error);
            }
            self.log("Windows 系统代理快速关闭，Mihomo 常驻核心继续运行");
        }
        Ok((
            if enabled {
                "Windows 系统代理已快速开启".to_string()
            } else {
                "Windows 系统代理已快速关闭".to_string()
            },
            json!({
                "runtime_running": true,
                "runtime_mode": if enabled { "active" } else { "standby" },
                "system_proxy_active": enabled,
                "mixed_port": port,
            }),
        ))
    }

    fn desired_proxy_port(&self) -> AppResult<Option<u16>> {
        let data = fs::read(self.config_path()).map_err(|e| format!("读取运行配置失败: {e}"))?;
        let value: serde_json::Value =
            serde_json::from_slice(&data).map_err(|e| format!("解析运行配置失败: {e}"))?;
        let tun_enabled = value
            .get("tun")
            .and_then(|tun| tun.get("enable"))
            .and_then(|value| value.as_bool())
            .unwrap_or(false);
        let port = value
            .get("mixed-port")
            .and_then(|value| value.as_u64())
            .unwrap_or(0);
        if !tun_enabled && (1024..=65535).contains(&port) {
            Ok(Some(port as u16))
        } else {
            Ok(None)
        }
    }

    fn reconcile_system_proxy(&self) -> AppResult<()> {
        if self.marker_path().is_file() && self.runtime_mode() == "active" {
            if let Some(port) = self.desired_proxy_port()? {
                let bypass_domains = self.system_proxy_bypass_domains()?;
                return system_proxy::activate(&self.base, &self.owner_sid, port, &bypass_domains);
            }
        }
        system_proxy::restore(&self.base, &self.owner_sid)
    }

    fn system_proxy_bypass_domains(&self) -> AppResult<Vec<String>> {
        let path = self.system_proxy_bypass_path();
        if !path.is_file() {
            return Ok(Vec::new());
        }
        let data = fs::read(path).map_err(|e| format!("读取系统代理绕过域名失败: {e}"))?;
        let values: Vec<String> =
            serde_json::from_slice(&data).map_err(|e| format!("解析系统代理绕过域名失败: {e}"))?;
        system_proxy::normalize_bypass_domains(&values)
    }

    fn stop_runtime(&mut self) -> AppResult<()> {
        let Some(mut child) = self.child.take() else {
            return Ok(());
        };
        if child.try_wait().ok().flatten().is_some() {
            return Ok(());
        }
        if let Err(error) = child.kill() {
            self.child = Some(child);
            return Err(format!("停止 Mihomo 失败: {error}"));
        }
        let _ = child.wait();
        self.log("Mihomo 运行时已停止");
        Ok(())
    }

    fn take_pending(&mut self, transaction_id: &str) -> AppResult<PendingTransaction> {
        let pending = self
            .pending
            .take()
            .ok_or_else(|| "没有等待处理的配置事务".to_string())?;
        if pending.id != transaction_id || transaction_id.is_empty() {
            let expected = pending.id.clone();
            self.pending = Some(pending);
            return Err(format!("配置事务标识不匹配: {expected}"));
        }
        Ok(pending)
    }

    fn recover_interrupted(base: &Path) -> AppResult<()> {
        let path = base.join("pending.json");
        if !path.is_file() {
            return Ok(());
        }
        let data = fs::read(&path).map_err(|e| format!("读取遗留事务失败: {e}"))?;
        let record: PendingRecord =
            serde_json::from_slice(&data).map_err(|e| format!("解析遗留事务失败: {e}"))?;
        Self::restore_record(base, &record)
    }

    fn restore_record(base: &Path, record: &PendingRecord) -> AppResult<()> {
        let rollback = PathBuf::from(&record.rollback_dir);
        let expected_parent = base.join("transactions");
        if !rollback.starts_with(&expected_parent) {
            return Err("回滚目录越界".to_string());
        }
        restore_one(
            base.join("config.json"),
            rollback.join("config.json"),
            record.had_config,
        )?;
        restore_one(base.join("data"), rollback.join("data"), record.had_data)?;
        restore_one(
            base.join("enabled.marker"),
            rollback.join("enabled.marker"),
            record.had_marker,
        )?;
        restore_one(
            base.join("runtime-mode"),
            rollback.join("runtime-mode"),
            record.had_mode_marker,
        )?;
        restore_one(
            base.join("fast-toggle-ready.marker"),
            rollback.join("fast-toggle-ready.marker"),
            record.had_fast_toggle_marker,
        )?;
        restore_one(
            base.join(SYSTEM_PROXY_BYPASS_FILE),
            rollback.join(SYSTEM_PROXY_BYPASS_FILE),
            record.had_system_proxy_bypass,
        )?;
        let _ = remove_any(&base.join("pending.json"));
        Ok(())
    }

    fn config_path(&self) -> PathBuf {
        self.base.join("config.json")
    }
    fn data_path(&self) -> PathBuf {
        self.base.join("data")
    }
    fn marker_path(&self) -> PathBuf {
        self.base.join("enabled.marker")
    }
    fn mode_path(&self) -> PathBuf {
        self.base.join("runtime-mode")
    }
    fn fast_toggle_marker_path(&self) -> PathBuf {
        self.base.join("fast-toggle-ready.marker")
    }
    fn system_proxy_bypass_path(&self) -> PathBuf {
        self.base.join(SYSTEM_PROXY_BYPASS_FILE)
    }
    fn runtime_mode(&self) -> &'static str {
        if !self.marker_path().is_file() {
            return "stopped";
        }
        match fs::read_to_string(self.mode_path()) {
            Ok(value) if value.trim().eq_ignore_ascii_case("standby") => "standby",
            _ => "active",
        }
    }
    fn mihomo_path(&self) -> PathBuf {
        self.base.join(MIHOMO_BINARY)
    }
    fn transactions_dir(&self) -> PathBuf {
        self.base.join("transactions")
    }
    fn pending_record_path(&self) -> PathBuf {
        self.base.join("pending.json")
    }

    fn log(&self, message: &str) {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.base.join("service.log"))
        {
            let _ = writeln!(file, "{stamp} {message}");
        }
    }
}

fn restore_one(current: PathBuf, backup: PathBuf, had_value: bool) -> AppResult<()> {
    if backup.exists() {
        remove_any(&current).map_err(|e| format!("清理新版本失败: {e}"))?;
        fs::rename(&backup, &current).map_err(|e| format!("恢复旧版本失败: {e}"))?;
    } else if !had_value {
        remove_any(&current).map_err(|e| format!("清理新增文件失败: {e}"))?;
    }
    Ok(())
}

fn sanitize_detail(value: &str) -> String {
    let mut result = String::with_capacity(value.len().min(500));
    let mut hiding_url = false;
    let mut index = 0;
    let bytes = value.as_bytes();
    while index < bytes.len() && result.chars().count() < 500 {
        let tail = &value[index..];
        if tail.starts_with("https://") || tail.starts_with("http://") {
            result.push_str("[已隐藏 URL]");
            hiding_url = true;
        }
        let character = tail.chars().next().unwrap_or(' ');
        if hiding_url {
            if character.is_whitespace() || matches!(character, '\'' | '"' | '<' | '>') {
                hiding_url = false;
                if character >= ' ' || character == '\t' {
                    result.push(character);
                }
            }
        } else if character >= ' ' || character == '\t' {
            result.push(character);
        }
        index += character.len_utf8();
    }
    let lowered = result.to_ascii_lowercase();
    for marker in ["authorization", "bearer ", "token=", "secret=", "password="] {
        if let Some(position) = lowered.find(marker) {
            result.truncate(position);
            result.push_str("[敏感信息已隐藏]");
            break;
        }
    }
    result.trim().to_string()
}

fn read_sanitized_tail(path: &Path, line_limit: usize) -> Vec<String> {
    let Ok(data) = fs::read(path) else {
        return Vec::new();
    };
    let start = data.len().saturating_sub(64 * 1024);
    let text = String::from_utf8_lossy(&data[start..]);
    let lines: Vec<String> = text
        .lines()
        .map(sanitize_detail)
        .filter(|line| !line.is_empty())
        .collect();
    lines
        .into_iter()
        .rev()
        .take(line_limit)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::sanitize_detail;

    #[test]
    fn errors_hide_subscription_urls_and_tokens() {
        let value =
            sanitize_detail("fetch https://example.test/sub?token=secret failed token=secret");
        assert!(!value.contains("example.test"));
        assert!(!value.contains("secret"));
        assert!(value.contains("已隐藏 URL"));
    }
}
