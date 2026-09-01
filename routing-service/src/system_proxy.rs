use crate::util::{atomic_write, AppResult};
use serde::{Deserialize, Serialize};
use std::{ffi::c_void, fs, path::Path};
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

#[derive(Debug, Serialize, Deserialize)]
struct Snapshot {
    proxy_enable: Option<u32>,
    proxy_server: Option<String>,
    proxy_override: Option<String>,
    auto_config_url: Option<String>,
}

pub fn is_active(base: &Path) -> bool {
    base.join(SNAPSHOT_FILE).is_file()
}

pub fn activate(base: &Path, owner_sid: &str, port: u16) -> AppResult<()> {
    let path = base.join(SNAPSHOT_FILE);
    let key = open(owner_sid, KEY_READ | KEY_SET_VALUE)?;
    let result = (|| -> AppResult<()> {
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
        set_string(key, "ProxyOverride", SAFE_BYPASS)?;
        delete_value(key, "AutoConfigURL")?;
        set_dword(key, "ProxyEnable", 1)?;
        notify();
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
    let snapshot: Snapshot =
        serde_json::from_slice(&data).map_err(|e| format!("解析系统代理快照失败: {e}"))?;
    let key = open(owner_sid, KEY_SET_VALUE)?;
    let result = (|| -> AppResult<()> {
        restore_string(key, "ProxyServer", snapshot.proxy_server.as_deref())?;
        restore_string(key, "ProxyOverride", snapshot.proxy_override.as_deref())?;
        restore_string(key, "AutoConfigURL", snapshot.auto_config_url.as_deref())?;
        // 最后恢复启用位，避免短暂把原启用状态指向候选端口。
        restore_dword(key, "ProxyEnable", snapshot.proxy_enable)?;
        notify();
        fs::remove_file(&path).map_err(|e| format!("清理系统代理快照失败: {e}"))?;
        Ok(())
    })();
    unsafe { RegCloseKey(key) };
    result
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
