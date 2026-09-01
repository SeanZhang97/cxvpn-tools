use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fs::{self, File},
    io::{self, Read, Write},
    os::windows::ffi::{OsStrExt, OsStringExt},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use windows_sys::Win32::Storage::FileSystem::{
    MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};
use windows_sys::Win32::{
    System::Com::CoTaskMemFree,
    UI::Shell::{FOLDERID_ProgramData, SHGetKnownFolderPath},
};

pub type AppResult<T> = Result<T, String>;

pub fn program_data_dir() -> PathBuf {
    let mut raw = std::ptr::null_mut();
    let result =
        unsafe { SHGetKnownFolderPath(&FOLDERID_ProgramData, 0, std::ptr::null_mut(), &mut raw) };
    let root = if result >= 0 && !raw.is_null() {
        let mut length = 0;
        unsafe {
            while *raw.add(length) != 0 {
                length += 1;
            }
        }
        let value = unsafe { OsString::from_wide(std::slice::from_raw_parts(raw, length)) };
        unsafe { CoTaskMemFree(raw.cast()) };
        PathBuf::from(value)
    } else {
        PathBuf::from(r"C:\ProgramData")
    };
    root.join("CXVPNManager").join("RoutingService")
}

pub fn sha256_bytes(data: &[u8]) -> String {
    format!("{:X}", Sha256::digest(data))
}

pub fn sha256_file(path: &Path) -> AppResult<String> {
    let mut file = File::open(path).map_err(|e| format!("无法读取 {}: {e}", path.display()))?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|e| format!("读取失败: {e}"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:X}", digest.finalize()))
}

pub fn atomic_write(path: &Path, data: &[u8]) -> AppResult<()> {
    let parent = path
        .parent()
        .ok_or_else(|| "目标路径没有父目录".to_string())?;
    fs::create_dir_all(parent).map_err(|e| format!("创建目录失败: {e}"))?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temp = parent.join(format!(".cxvpn-{stamp}.tmp"));
    let mut file = File::create(&temp).map_err(|e| format!("创建临时文件失败: {e}"))?;
    file.write_all(data)
        .map_err(|e| format!("写入临时文件失败: {e}"))?;
    file.sync_all()
        .map_err(|e| format!("同步临时文件失败: {e}"))?;
    drop(file);
    let source: Vec<u16> = temp.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let moved = unsafe {
        MoveFileExW(
            source.as_ptr(),
            target.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if moved == 0 {
        let error = io::Error::last_os_error();
        let _ = fs::remove_file(&temp);
        return Err(format!("提交文件失败: {error}"));
    }
    Ok(())
}

pub fn remove_any(path: &Path) -> io::Result<()> {
    if path.is_dir() {
        fs::remove_dir_all(path)
    } else if path.exists() {
        fs::remove_file(path)
    } else {
        Ok(())
    }
}

pub fn copy_dir(source: &Path, target: &Path) -> AppResult<()> {
    if !source.exists() {
        return Ok(());
    }
    fs::create_dir_all(target).map_err(|e| format!("创建数据目录失败: {e}"))?;
    for entry in fs::read_dir(source).map_err(|e| format!("读取数据目录失败: {e}"))? {
        let entry = entry.map_err(|e| format!("读取目录项失败: {e}"))?;
        let file_type = entry
            .file_type()
            .map_err(|e| format!("读取文件类型失败: {e}"))?;
        let destination = target.join(entry.file_name());
        if file_type.is_symlink() {
            return Err("服务数据目录中不允许符号链接或重解析点".to_string());
        }
        if file_type.is_dir() {
            copy_dir(&entry.path(), &destination)?;
        } else if file_type.is_file() {
            fs::copy(entry.path(), destination).map_err(|e| format!("复制服务数据失败: {e}"))?;
        }
    }
    Ok(())
}

pub fn transaction_id() -> String {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{:x}-{:x}", std::process::id(), stamp)
}

pub fn safe_provider_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value.ends_with(".yaml")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_name_rejects_path_traversal() {
        assert!(safe_provider_name("provider-alpha.yaml"));
        assert!(!safe_provider_name("../provider.yaml"));
        assert!(!safe_provider_name(r"folder\provider.yaml"));
        assert!(!safe_provider_name("provider.json"));
    }

    #[test]
    fn file_hash_uses_heap_buffer_and_matches_bytes() {
        let path = std::env::temp_dir().join(format!(
            "cxvpn-routing-service-hash-{}.tmp",
            transaction_id()
        ));
        fs::write(&path, b"cxvpn").unwrap();
        let file_hash = sha256_file(&path).unwrap();
        let _ = fs::remove_file(path);
        assert_eq!(file_hash, sha256_bytes(b"cxvpn"));
    }
}
