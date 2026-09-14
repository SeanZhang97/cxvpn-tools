use crate::{
    constants::{
        GEOIP_DATABASE, GEOIP_SHA256, MIHOMO_BINARY, MIHOMO_SHA256, SERVICE_BINARY,
        SERVICE_DESCRIPTION, SERVICE_DISPLAY_NAME, SERVICE_ID,
    },
    util::{atomic_write, program_data_dir, sha256_file, AppResult},
};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
    thread,
    time::Duration,
};

pub fn install(owner_sid: &str) -> AppResult<()> {
    validate_owner_sid(owner_sid)?;
    let source_service =
        std::env::current_exe().map_err(|e| format!("读取服务程序路径失败: {e}"))?;
    let source_dir = source_service
        .parent()
        .ok_or_else(|| "服务程序目录无效".to_string())?;
    let source_mihomo = source_dir.join(MIHOMO_BINARY);
    let source_geoip = source_dir.join(GEOIP_DATABASE);
    if !source_mihomo.is_file() {
        return Err("安装包缺少 mihomo.exe".to_string());
    }
    if !sha256_file(&source_mihomo)?.eq_ignore_ascii_case(MIHOMO_SHA256) {
        return Err("安装包中的 Mihomo 完整性校验失败".to_string());
    }
    if !source_geoip.is_file() {
        return Err("安装包缺少 Country.mmdb".to_string());
    }
    if !sha256_file(&source_geoip)?.eq_ignore_ascii_case(GEOIP_SHA256) {
        return Err("安装包中的 GeoIP 数据库完整性校验失败".to_string());
    }

    let base = program_data_dir();
    fs::create_dir_all(&base).map_err(|e| format!("创建服务目录失败: {e}"))?;
    let target_service = base.join(SERVICE_BINARY);
    let target_mihomo = base.join(MIHOMO_BINARY);
    let data_dir = base.join("data");
    let target_geoip = data_dir.join(GEOIP_DATABASE);
    let service_backup = base.join("CXVPNRoutingHost.previous.exe");
    let owner_backup = base.join("owner.previous.sid");
    let geoip_backup = base.join("Country.previous.mmdb");
    let old_host_available = target_service.is_file();
    let old_winsw = base.join(format!("{SERVICE_ID}.exe"));
    let old_winsw_available =
        old_winsw.is_file() && base.join(format!("{SERVICE_ID}.xml")).is_file();
    if old_host_available {
        fs::copy(&target_service, &service_backup)
            .map_err(|e| format!("备份旧服务程序失败: {e}"))?;
    }
    if base.join("owner.sid").is_file() {
        let _ = fs::copy(base.join("owner.sid"), &owner_backup);
    }
    let old_geoip_available = target_geoip.is_file();
    if old_geoip_available {
        fs::copy(&target_geoip, &geoip_backup)
            .map_err(|e| format!("备份旧 GeoIP 数据库失败: {e}"))?;
    }

    stop_and_delete_service()?;
    let result = (|| -> AppResult<()> {
        fs::copy(&source_service, &target_service).map_err(|e| format!("复制服务程序失败: {e}"))?;
        if !target_mihomo.is_file()
            || !sha256_file(&target_mihomo)?.eq_ignore_ascii_case(MIHOMO_SHA256)
        {
            fs::copy(&source_mihomo, &target_mihomo)
                .map_err(|e| format!("复制 Mihomo 失败: {e}"))?;
        }
        if !sha256_file(&target_mihomo)?.eq_ignore_ascii_case(MIHOMO_SHA256) {
            return Err("复制后的 Mihomo 完整性校验失败".to_string());
        }
        fs::create_dir_all(&data_dir).map_err(|e| format!("创建运行数据目录失败: {e}"))?;
        if !target_geoip.is_file()
            || !sha256_file(&target_geoip)?.eq_ignore_ascii_case(GEOIP_SHA256)
        {
            fs::copy(&source_geoip, &target_geoip)
                .map_err(|e| format!("复制 GeoIP 数据库失败: {e}"))?;
        }
        if !sha256_file(&target_geoip)?.eq_ignore_ascii_case(GEOIP_SHA256) {
            return Err("复制后的 GeoIP 数据库完整性校验失败".to_string());
        }
        atomic_write(&base.join("owner.sid"), owner_sid.as_bytes())?;
        harden_acl(&base)?;
        create_service(&target_service)?;
        start_service()?;
        Ok(())
    })();

    if let Err(error) = result {
        let _ = stop_and_delete_service();
        if old_geoip_available && geoip_backup.is_file() {
            let _ = fs::create_dir_all(&data_dir);
            let _ = fs::copy(&geoip_backup, &target_geoip);
        } else if !old_geoip_available {
            let _ = fs::remove_file(&target_geoip);
        }
        if old_host_available && service_backup.is_file() {
            let _ = fs::copy(&service_backup, &target_service);
            if owner_backup.is_file() {
                let _ = fs::copy(&owner_backup, base.join("owner.sid"));
            }
            let _ = create_service(&target_service).and_then(|_| start_service());
        } else if old_winsw_available {
            let _ = run_checked(&old_winsw, &["install"]);
            let _ = run_checked(&old_winsw, &["start"]);
        }
        return Err(format!("安装新路由服务失败，已尝试恢复旧服务: {error}"));
    }
    let _ = fs::remove_file(service_backup);
    let _ = fs::remove_file(owner_backup);
    let _ = fs::remove_file(geoip_backup);
    Ok(())
}

pub fn uninstall() -> AppResult<()> {
    stop_and_delete_service()
}

fn create_service(binary: &Path) -> AppResult<()> {
    let quoted = format!("\"{}\"", binary.display());
    run_sc(&[
        "create",
        SERVICE_ID,
        "binPath=",
        &quoted,
        "start=",
        "auto",
        "DisplayName=",
        SERVICE_DISPLAY_NAME,
    ])?;
    run_sc(&["description", SERVICE_ID, SERVICE_DESCRIPTION])?;
    let _ = run_sc(&[
        "failure",
        SERVICE_ID,
        "reset=",
        "3600",
        "actions=",
        "restart/10000/restart/30000/restart/60000",
    ]);
    let _ = run_sc(&["failureflag", SERVICE_ID, "1"]);
    let _ = run_sc(&["sidtype", SERVICE_ID, "unrestricted"]);
    Ok(())
}

fn start_service() -> AppResult<()> {
    let _ = run_sc(&["start", SERVICE_ID]);
    for _ in 0..30 {
        let output = run_output("sc.exe", &["query", SERVICE_ID])?;
        let text = String::from_utf8_lossy(&output.stdout);
        if output.status.success() && text.contains("RUNNING") {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err("Windows Service 未进入 Running 状态".to_string())
}

fn stop_and_delete_service() -> AppResult<()> {
    if query_service()?.is_none() {
        return Ok(());
    }
    let _ = run_output("sc.exe", &["stop", SERVICE_ID]);
    if !wait_until_stopped_or_missing(40)? {
        let query = match query_service()? {
            Some(value) => value,
            None => return Ok(()),
        };
        let pid = service_pid(&query.stdout)
            .ok_or_else(|| "旧路由服务未停止，且无法确认其进程 PID".to_string())?;
        let pid_text = pid.to_string();
        run_checked(
            Path::new("taskkill.exe"), &["/PID", &pid_text, "/T", "/F"])
            .map_err(|e| format!("强制结束卡住的旧路由服务失败: {e}"))?;
        if !wait_until_stopped_or_missing(40)? {
            return Err(format!(
                "旧路由服务进程 {pid} 已请求终止，但服务未在期限内停止"));
        }
    }
    let delete = run_output("sc.exe", &["delete", SERVICE_ID])?;
    for _ in 0..40 {
        if query_service()?.is_none() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
    let detail = String::from_utf8_lossy(if delete.stderr.is_empty() {
        &delete.stdout
    } else {
        &delete.stderr
    });
    Err(format!(
        "旧路由服务未完成删除: {}",
        detail.trim()))
}

fn query_service() -> AppResult<Option<Output>> {
    let output = run_output("sc.exe", &["queryex", SERVICE_ID])?;
    if output.status.success() {
        return Ok(Some(output));
    }
    let detail = String::from_utf8_lossy(if output.stderr.is_empty() {
        &output.stdout
    } else {
        &output.stderr
    });
    if detail.split(|ch: char| !ch.is_ascii_digit())
        .any(|part| part == "1060")
    {
        return Ok(None);
    }
    Err(format!("查询路由服务状态失败: {}", detail.trim()))
}

fn wait_until_stopped_or_missing(attempts: usize) -> AppResult<bool> {
    for _ in 0..attempts {
        match query_service()? {
            None => return Ok(true),
            Some(output) if service_has_state(&output.stdout, "STOPPED") => {
                return Ok(true);
            }
            Some(_) => thread::sleep(Duration::from_millis(250)),
        }
    }
    Ok(false)
}

fn service_has_state(output: &[u8], expected: &str) -> bool {
    String::from_utf8_lossy(output).lines().any(|line| {
        line.split_once(':').is_some_and(|(key, value)| {
            key.trim().eq_ignore_ascii_case("STATE")
                && value.split_whitespace().any(|part| {
                    part.eq_ignore_ascii_case(expected)
                })
        })
    })
}

fn service_pid(output: &[u8]) -> Option<u32> {
    String::from_utf8_lossy(output).lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        if !key.trim().eq_ignore_ascii_case("PID") {
            return None;
        }
        value.trim().parse::<u32>().ok().filter(|pid| *pid > 0)
    })
}

fn harden_acl(base: &Path) -> AppResult<()> {
    let value = base.to_string_lossy().into_owned();
    run_checked(
        Path::new("icacls.exe"),
        &[
            &value,
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
        ],
    )?;
    Ok(())
}

fn run_sc(args: &[&str]) -> AppResult<Output> {
    run_checked(Path::new("sc.exe"), args)
}

fn run_checked(program: &Path, args: &[&str]) -> AppResult<Output> {
    let output = Command::new(program)
        .args(args)
        .output()
        .map_err(|e| format!("无法执行 {}: {e}", program.display()))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(if output.stderr.is_empty() {
            &output.stdout
        } else {
            &output.stderr
        });
        return Err(format!("{} 执行失败: {}", program.display(), detail.trim()));
    }
    Ok(output)
}

fn run_output(program: &str, args: &[&str]) -> AppResult<Output> {
    Command::new(program)
        .args(args)
        .output()
        .map_err(|e| format!("无法执行 {program}: {e}"))
}

fn validate_owner_sid(value: &str) -> AppResult<()> {
    if value.starts_with("S-1-")
        && value.len() <= 184
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'-' || byte == b'S')
    {
        Ok(())
    } else {
        Err("当前 Windows 用户 SID 无效".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::{service_has_state, service_pid};

    #[test]
    fn parses_service_state_and_exact_pid_from_queryex() {
        let output = br#"
SERVICE_NAME: CXVPNRoutingService
        STATE              : 2  START_PENDING
        PID                : 5792
"#;

        assert!(service_has_state(output, "START_PENDING"));
        assert!(!service_has_state(output, "STOPPED"));
        assert_eq!(service_pid(output), Some(5792));
    }

    #[test]
    fn rejects_missing_or_zero_service_pid() {
        assert_eq!(service_pid(b"PID : 0"), None);
        assert_eq!(service_pid(b"STATE : 1 STOPPED"), None);
    }
}
