use crate::util::{atomic_write, transaction_id, AppResult};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet, ffi::c_void, fs, io::Write, path::Path,
    time::{Instant, SystemTime, UNIX_EPOCH},
};
use windows_sys::Win32::{
    Foundation::ERROR_SUCCESS,
    Networking::WinInet::{
        InternetSetOptionW, INTERNET_OPTION_REFRESH, INTERNET_OPTION_SETTINGS_CHANGED,
    },
    System::Registry::{
        RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW, HKEY,
        HKEY_USERS, KEY_READ, KEY_SET_VALUE, REG_DWORD, REG_SZ,
    },
};

const SNAPSHOT_FILE: &str = "system-proxy-snapshot.json";
const INTERNET_SETTINGS: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings";
const SAFE_BYPASS: &str = "<local>;localhost;127.*;[::1];*.local;*.lan;10.*;192.168.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;172.28.*;172.29.*;172.30.*;172.31.*";
// 手动补充项最多 100 个；客户端还可合并最多 2000 个本地规则包域名。
const MAX_EFFECTIVE_BYPASS_DOMAINS: usize = 2100;

#[derive(Debug, Serialize, Deserialize)]
struct Snapshot {
    proxy_enable: Option<u32>,
    proxy_server: Option<String>,
    proxy_override: Option<String>,
    auto_config_url: Option<String>,
}

impl Snapshot {
    fn normalize_empty_endpoint(&mut self) -> bool {
        // 空地址和仅含冒号的占位值不是可恢复的代理，不能重新打开启用位。
        // 不解析或改写其它格式，保留企业代理、按协议代理、IPv6 和 PAC。
        let endpoint = self.proxy_server.as_deref().unwrap_or("").trim();
        if !endpoint.is_empty() && endpoint != ":" {
            return false;
        }
        if self.proxy_enable.unwrap_or(0) == 0 && self.proxy_server.is_none() {
            return false;
        }
        self.proxy_enable = Some(0);
        self.proxy_server = None;
        true
    }

    fn normalize_managed_endpoint(&mut self, managed_port: Option<u16>) -> bool {
        if !self.is_managed_endpoint(managed_port) {
            return false;
        }
        self.proxy_enable = Some(0);
        self.proxy_server = None;
        true
    }

    fn is_managed_endpoint(&self, managed_port: Option<u16>) -> bool {
        let Some(port) = managed_port.filter(|value| *value >= 1024) else {
            return false;
        };
        self.proxy_enable.unwrap_or(0) != 0
            && self
                .proxy_server
                .as_deref()
                .unwrap_or("")
                .trim()
                .eq_ignore_ascii_case(&format!("127.0.0.1:{port}"))
    }
}

pub fn is_active(base: &Path) -> bool {
    base.join(SNAPSHOT_FILE).is_file()
}

pub fn normalize_bypass_domains(values: &[String]) -> AppResult<Vec<String>> {
    if values.len() > MAX_EFFECTIVE_BYPASS_DOMAINS {
        return Err(format!(
            "系统代理入口实际绕过域名最多 {MAX_EFFECTIVE_BYPASS_DOMAINS} 个"
        ));
    }
    let mut result = Vec::with_capacity(values.len());
    let mut seen = HashSet::new();
    for (index, raw) in values.iter().enumerate() {
        let domain = raw.trim().trim_end_matches('.').to_ascii_lowercase();
        let valid = !domain.is_empty()
            && domain.len() <= 253
            && domain.is_ascii()
            && domain.split('.').all(|label| {
                !label.is_empty()
                    && label.len() <= 63
                    && label
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                    && !label.starts_with('-')
                    && !label.ends_with('-')
            });
        if !valid {
            return Err(format!("系统代理入口绕过第 {} 个域名无效", index + 1));
        }
        if seen.insert(domain.clone()) {
            result.push(domain);
        }
    }
    Ok(result)
}

fn proxy_override(values: &[String]) -> AppResult<String> {
    let domains = normalize_bypass_domains(values)?;
    let mut result = String::from(SAFE_BYPASS);
    for domain in domains {
        result.push(';');
        result.push_str(&domain);
        result.push_str(";*.");
        result.push_str(&domain);
    }
    Ok(result)
}

pub fn activate(
    base: &Path,
    owner_sid: &str,
    port: u16,
    bypass_domains: &[String],
) -> AppResult<()> {
    let path = base.join(SNAPSHOT_FILE);
    let key = open(owner_sid, KEY_READ | KEY_SET_VALUE)?;
    let result = (|| -> AppResult<()> {
        let expected_override = proxy_override(bypass_domains)?;
        if !path.is_file() {
            let snapshot = Snapshot {
                proxy_enable: query_dword(key, "ProxyEnable")?,
                proxy_server: query_string(key, "ProxyServer")?,
                proxy_override: query_string(key, "ProxyOverride")?,
                auto_config_url: query_string(key, "AutoConfigURL")?,
            };
            let data = serde_json::to_vec(&snapshot)
                .map_err(|e| format!("序列化系统代理快照失败: {e}"))?;
            atomic_write(&path, &data)?;
        }
        set_string(key, "ProxyServer", &format!("127.0.0.1:{port}"))?;
        set_string(key, "ProxyOverride", &expected_override)?;
        delete_value(key, "AutoConfigURL")?;
        set_dword(key, "ProxyEnable", 1)?;
        notify();
        if query_string(key, "ProxyOverride")?.as_deref() != Some(expected_override.as_str()) {
            return Err("Windows 系统代理绕过域名写入后回读不一致".to_string());
        }
        Ok(())
    })();
    unsafe { RegCloseKey(key) };
    result
}

pub fn restore(base: &Path, owner_sid: &str) -> AppResult<()> {
    let path = base.join(SNAPSHOT_FILE);
    if !path.is_file() {
        return Ok(());
    }
    let data = fs::read(&path).map_err(|e| format!("读取系统代理快照失败: {e}"))?;
    let mut snapshot: Snapshot =
        serde_json::from_slice(&data).map_err(|e| format!("解析系统代理快照失败: {e}"))?;
    let started = Instant::now();
    log_restore(base, "开始恢复用户系统代理快照");
    if snapshot.normalize_empty_endpoint() {
        // 保留原始异常快照供审计；备份失败时不覆盖现场。
        atomic_write(
            &base.join(format!("system-proxy-invalid-{}.json", transaction_id())),
            &data,
        )?;
        log_restore(
            base,
            "原快照含空代理地址：已保留异常备份，将关闭手动代理并清除空地址",
        );
    }
    let key = open(owner_sid, KEY_READ | KEY_SET_VALUE)?;
    let result = (|| -> AppResult<()> {
        restore_string(key, "ProxyServer", snapshot.proxy_server.as_deref())?;
        restore_string(key, "ProxyOverride", snapshot.proxy_override.as_deref())?;
        restore_string(key, "AutoConfigURL", snapshot.auto_config_url.as_deref())?;
        // 最后恢复启用位，避免短暂把原启用状态指向候选端口。
        restore_dword(key, "ProxyEnable", snapshot.proxy_enable)?;
        notify();
        if query_dword(key, "ProxyEnable")? != snapshot.proxy_enable
            || query_string(key, "ProxyServer")? != snapshot.proxy_server
            || query_string(key, "ProxyOverride")? != snapshot.proxy_override
            || query_string(key, "AutoConfigURL")? != snapshot.auto_config_url
        {
            return Err("Windows 系统代理恢复后回读不一致，已保留恢复快照".to_string());
        }
        fs::remove_file(&path).map_err(|e| format!("清理系统代理快照失败: {e}"))?;
        Ok(())
    })();
    unsafe { RegCloseKey(key) };
    log_restore(base, &format!(
        "系统代理快照恢复{}，耗时 {} ms{}",
        if result.is_ok() { "成功" } else { "失败" },
        started.elapsed().as_millis(),
        result
            .as_ref()
            .err()
            .map(|error| format!("：{error}"))
            .unwrap_or_default(),
    ));
    result
}

pub fn suspend_for_tun(
    base: &Path,
    owner_sid: &str,
    managed_port: Option<u16>,
) -> AppResult<()> {
    let path = base.join(SNAPSHOT_FILE);
    let key = open(owner_sid, KEY_READ | KEY_SET_VALUE)?;
    let started = Instant::now();
    log_restore(base, "开始暂停 Windows 系统代理以启用 TUN");
    let result = (|| -> AppResult<()> {
        let current = Snapshot {
            proxy_enable: query_dword(key, "ProxyEnable")?,
            proxy_server: query_string(key, "ProxyServer")?,
            proxy_override: query_string(key, "ProxyOverride")?,
            auto_config_url: query_string(key, "AutoConfigURL")?,
        };
        if !path.is_file()
            && managed_port.is_some()
            && current.proxy_enable.unwrap_or(0) != 0
            && !current.is_managed_endpoint(managed_port)
        {
            return Err("TUN 启用前检测到非本软件的 Windows 手动代理".to_string());
        }
        let data = if path.is_file() {
            fs::read(&path).map_err(|e| format!("读取系统代理快照失败: {e}"))?
        } else {
            serde_json::to_vec(&current).map_err(|e| format!("序列化系统代理快照失败: {e}"))?
        };
        let mut snapshot: Snapshot =
            serde_json::from_slice(&data).map_err(|e| format!("解析系统代理快照失败: {e}"))?;
        if snapshot.normalize_empty_endpoint() {
            atomic_write(
                &base.join(format!("system-proxy-invalid-{}.json", transaction_id())),
                &data,
            )?;
            log_restore(
                base,
                "原快照含空代理地址：已保留异常备份，将长期恢复目标归一化为关闭",
            );
        } else if snapshot.normalize_managed_endpoint(managed_port) {
            atomic_write(
                &base.join(format!(
                    "system-proxy-managed-loop-{}.json",
                    transaction_id()
                )),
                &data,
            )?;
            log_restore(
                base,
                "原快照误指向本软件端口：已保留审计备份，将长期恢复目标归一化为关闭",
            );
        }
        let normalized = serde_json::to_vec(&snapshot)
            .map_err(|e| format!("序列化系统代理快照失败: {e}"))?;
        if !path.is_file() || normalized != data {
            atomic_write(&path, &normalized)?;
        }

        // 先关闭启用位，再移除候选端点和 PAC，避免短暂把应用指向空地址。
        set_dword(key, "ProxyEnable", 0)?;
        delete_value(key, "ProxyServer")?;
        delete_value(key, "AutoConfigURL")?;
        notify();
        if query_dword(key, "ProxyEnable")? != Some(0)
            || query_string(key, "ProxyServer")?.is_some()
            || query_string(key, "AutoConfigURL")?.is_some()
        {
            return Err("TUN 启用前关闭 Windows 系统代理后回读不一致".to_string());
        }
        Ok(())
    })();
    unsafe { RegCloseKey(key) };
    log_restore(
        base,
        &format!(
            "TUN 系统代理暂停{}，耗时 {} ms{}",
            if result.is_ok() { "成功" } else { "失败" },
            started.elapsed().as_millis(),
            result
                .as_ref()
                .err()
                .map(|error| format!("：{error}"))
                .unwrap_or_default(),
        ),
    );
    result
}

fn log_restore(base: &Path, message: &str) {
    // 与服务主日志一致；日志写入失败不能中断代理恢复。
    if let Ok(mut file) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join("service.log"))
    {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let _ = writeln!(file, "{stamp} {message}");
    }
}

fn open(owner_sid: &str, access: u32) -> AppResult<HKEY> {
    let subkey = wide(&format!("{owner_sid}\\{INTERNET_SETTINGS}"));
    let mut key: HKEY = std::ptr::null_mut();
    let status = unsafe { RegOpenKeyExW(HKEY_USERS, subkey.as_ptr(), 0, access, &mut key) };
    if status != ERROR_SUCCESS {
        return Err(format!("打开用户系统代理注册表失败: Windows {status}"));
    }
    Ok(key)
}

fn query_dword(key: HKEY, name: &str) -> AppResult<Option<u32>> {
    let name = wide(name);
    let mut kind = 0;
    let mut value = 0u32;
    let mut size = std::mem::size_of::<u32>() as u32;
    let status = unsafe {
        RegQueryValueExW(
            key,
            name.as_ptr(),
            std::ptr::null_mut(),
            &mut kind,
            &mut value as *mut u32 as *mut u8,
            &mut size,
        )
    };
    if status == 2 {
        return Ok(None);
    }
    if status != ERROR_SUCCESS || kind != REG_DWORD {
        return Err(format!("读取系统代理 DWORD 失败: Windows {status}"));
    }
    Ok(Some(value))
}

fn query_string(key: HKEY, name: &str) -> AppResult<Option<String>> {
    let name = wide(name);
    let mut kind = 0;
    let mut size = 0u32;
    let status = unsafe {
        RegQueryValueExW(
            key,
            name.as_ptr(),
            std::ptr::null_mut(),
            &mut kind,
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if status == 2 {
        return Ok(None);
    }
    if status != ERROR_SUCCESS || kind != REG_SZ {
        return Err(format!("读取系统代理字符串失败: Windows {status}"));
    }
    let mut buffer = vec![0u16; (size as usize / 2).max(1)];
    let status = unsafe {
        RegQueryValueExW(
            key,
            name.as_ptr(),
            std::ptr::null_mut(),
            &mut kind,
            buffer.as_mut_ptr() as *mut u8,
            &mut size,
        )
    };
    if status != ERROR_SUCCESS {
        return Err(format!("读取系统代理字符串失败: Windows {status}"));
    }
    let end = buffer
        .iter()
        .position(|value| *value == 0)
        .unwrap_or(buffer.len());
    Ok(Some(String::from_utf16_lossy(&buffer[..end])))
}

fn set_dword(key: HKEY, name: &str, value: u32) -> AppResult<()> {
    let name = wide(name);
    let status = unsafe {
        RegSetValueExW(
            key,
            name.as_ptr(),
            0,
            REG_DWORD,
            &value as *const u32 as *const u8,
            4,
        )
    };
    check(status, "写入系统代理 DWORD")
}

fn set_string(key: HKEY, name: &str, value: &str) -> AppResult<()> {
    let name = wide(name);
    let value = wide(value);
    let status = unsafe {
        RegSetValueExW(
            key,
            name.as_ptr(),
            0,
            REG_SZ,
            value.as_ptr() as *const u8,
            (value.len() * 2) as u32,
        )
    };
    check(status, "写入系统代理字符串")
}

fn delete_value(key: HKEY, name: &str) -> AppResult<()> {
    let status = unsafe { RegDeleteValueW(key, wide(name).as_ptr()) };
    if status == ERROR_SUCCESS || status == 2 {
        Ok(())
    } else {
        Err(format!("删除系统代理字段失败: Windows {status}"))
    }
}

fn restore_dword(key: HKEY, name: &str, value: Option<u32>) -> AppResult<()> {
    match value {
        Some(value) => set_dword(key, name, value),
        None => delete_value(key, name),
    }
}

fn restore_string(key: HKEY, name: &str, value: Option<&str>) -> AppResult<()> {
    match value {
        Some(value) => set_string(key, name, value),
        None => delete_value(key, name),
    }
}

fn check(status: u32, action: &str) -> AppResult<()> {
    if status == ERROR_SUCCESS {
        Ok(())
    } else {
        Err(format!("{action}失败: Windows {status}"))
    }
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

fn notify() {
    unsafe {
        InternetSetOptionW(
            std::ptr::null_mut::<c_void>(),
            INTERNET_OPTION_SETTINGS_CHANGED,
            std::ptr::null_mut(),
            0,
        );
        InternetSetOptionW(
            std::ptr::null_mut::<c_void>(),
            INTERNET_OPTION_REFRESH,
            std::ptr::null_mut(),
            0,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::{normalize_bypass_domains, proxy_override, Snapshot};

    fn snapshot(server: Option<&str>, enabled: Option<u32>) -> Snapshot {
        Snapshot {
            proxy_enable: enabled,
            proxy_server: server.map(str::to_string),
            proxy_override: Some("<local>;*.corp.test".to_string()),
            auto_config_url: Some("https://corp.test/proxy.pac".to_string()),
        }
    }

    #[test]
    fn restore_disables_empty_or_colon_endpoints_and_preserves_pac() {
        for server in [None, Some(""), Some(" "), Some(":"), Some(" : ")] {
            for enabled in [None, Some(0), Some(1)] {
                let mut value = snapshot(server, enabled);
                value.normalize_empty_endpoint();
                assert_eq!(value.proxy_enable.unwrap_or(0), 0);
                assert_eq!(value.proxy_server, None);
                assert_eq!(value.proxy_override.as_deref(), Some("<local>;*.corp.test"));
                assert_eq!(value.auto_config_url.as_deref(), Some("https://corp.test/proxy.pac"));
                assert!(!value.normalize_empty_endpoint());
            }
        }
    }

    #[test]
    fn restore_preserves_original_nonempty_proxy_settings() {
        for server in [
            "proxy.corp.test:8080",
            "http=proxy:80;https=proxy:443",
            "[::1]:7890",
            "127.0.0.1:7890",
            "代理.example:8080",
        ] {
            for enabled in [None, Some(0), Some(1)] {
                let mut value = snapshot(Some(server), enabled);
                assert!(!value.normalize_empty_endpoint());
                assert_eq!(value.proxy_enable, enabled);
                assert_eq!(value.proxy_server.as_deref(), Some(server));
            }
        }
    }

    #[test]
    fn tun_normalizes_snapshot_that_points_back_to_managed_port() {
        let mut value = snapshot(Some("127.0.0.1:17890"), Some(1));

        assert!(value.normalize_managed_endpoint(Some(17890)));
        assert_eq!(value.proxy_enable, Some(0));
        assert_eq!(value.proxy_server, None);
        assert!(!value.normalize_managed_endpoint(Some(17890)));
    }

    #[test]
    fn tun_preserves_unrelated_or_disabled_proxy_snapshot() {
        for (server, enabled, managed_port) in [
            ("proxy.corp.test:8080", Some(1), Some(17890)),
            ("127.0.0.1:17890", Some(0), Some(17890)),
            ("127.0.0.1:17890", Some(1), Some(7890)),
            ("127.0.0.1:17890", Some(1), None),
        ] {
            let mut value = snapshot(Some(server), enabled);
            assert!(!value.normalize_managed_endpoint(managed_port));
            assert_eq!(value.proxy_enable, enabled);
            assert_eq!(value.proxy_server.as_deref(), Some(server));
        }
    }

    #[test]
    fn restore_keeps_absent_manual_proxy_fields_absent() {
        let mut value = snapshot(None, None);
        assert!(!value.normalize_empty_endpoint());
        assert_eq!(value.proxy_enable, None);
        assert_eq!(value.proxy_server, None);
    }

    #[test]
    fn proxy_override_lists_each_domain_and_subdomain_pattern() {
        let value = proxy_override(&[
            "Chaoxing.com".to_string(),
            "dashscope.aliyuncs.com".to_string(),
        ])
        .expect("域名应有效");

        assert!(value.contains(";chaoxing.com;*.chaoxing.com"));
        assert!(value.contains(";dashscope.aliyuncs.com;*.dashscope.aliyuncs.com"));
    }

    #[test]
    fn bypass_domains_reject_proxy_override_injection() {
        let error = normalize_bypass_domains(&["example.com;*.evil.test".to_string()])
            .expect_err("分号必须被拒绝");

        assert!(error.contains("域名无效"));
    }

    #[test]
    fn bypass_domains_are_normalized_and_deduplicated() {
        let values =
            normalize_bypass_domains(&["Example.COM.".to_string(), "example.com".to_string()])
                .expect("域名应有效");

        assert_eq!(values, vec!["example.com"]);
    }
}
