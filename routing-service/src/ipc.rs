use crate::{
    constants::{MAX_REQUEST_BYTES, PIPE_NAME},
    manager::RuntimeManager,
    protocol::{Request, Response},
    util::AppResult,
};
use std::{
    ffi::c_void,
    ptr::null_mut,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
};
use windows_sys::Win32::{
    Foundation::{
        CloseHandle, GetLastError, LocalFree, ERROR_PIPE_CONNECTED, HANDLE, INVALID_HANDLE_VALUE,
    },
    Security::{
        Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1},
        SECURITY_ATTRIBUTES,
    },
    Storage::FileSystem::{FlushFileBuffers, ReadFile, WriteFile, PIPE_ACCESS_DUPLEX},
    System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_READMODE_BYTE,
        PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_UNLIMITED_INSTANCES, PIPE_WAIT,
    },
};

pub fn run_listener(manager: Arc<Mutex<RuntimeManager>>, stop: Arc<AtomicBool>, owner_sid: String) {
    while !stop.load(Ordering::Relaxed) {
        let pipe = match create_pipe(&owner_sid) {
            Ok(value) => value,
            Err(_) => {
                std::thread::sleep(std::time::Duration::from_secs(1));
                continue;
            }
        };
        let connected = unsafe { ConnectNamedPipe(pipe, null_mut()) } != 0
            || unsafe { GetLastError() } == ERROR_PIPE_CONNECTED;
        if connected {
            let response = match read_request(pipe) {
                Ok(request) => match manager.lock() {
                    Ok(mut state) => state.handle(request),
                    Err(_) => Response::error("服务内部状态锁异常"),
                },
                Err(error) => Response::error(error),
            };
            let _ = write_response(pipe, &response);
            unsafe {
                FlushFileBuffers(pipe);
                DisconnectNamedPipe(pipe);
            }
        }
        unsafe { CloseHandle(pipe) };
    }
}

fn create_pipe(owner_sid: &str) -> AppResult<HANDLE> {
    if !valid_sid_text(owner_sid) {
        return Err("服务 IPC 所有者 SID 无效".to_string());
    }
    let sddl = format!("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;{owner_sid})");
    let sddl_wide = wide(&sddl);
    let mut descriptor: *mut c_void = null_mut();
    let converted = unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl_wide.as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            null_mut(),
        )
    };
    if converted == 0 {
        return Err("无法创建服务 IPC 安全描述符".to_string());
    }
    let mut attributes = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor,
        bInheritHandle: 0,
    };
    let name = wide(PIPE_NAME);
    let handle = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES,
            64 * 1024,
            64 * 1024,
            0,
            &mut attributes,
        )
    };
    unsafe { LocalFree(descriptor) };
    if handle == INVALID_HANDLE_VALUE {
        return Err("无法创建服务 IPC 管道".to_string());
    }
    Ok(handle)
}

fn read_request(pipe: HANDLE) -> AppResult<Request> {
    let mut header = [0_u8; 4];
    read_exact(pipe, &mut header)?;
    let length = u32::from_le_bytes(header);
    if length == 0 || length > MAX_REQUEST_BYTES {
        return Err("服务请求大小超出限制".to_string());
    }
    let mut body = vec![0_u8; length as usize];
    read_exact(pipe, &mut body)?;
    serde_json::from_slice(&body).map_err(|_| "服务请求格式无效".to_string())
}

fn write_response(pipe: HANDLE, response: &Response) -> AppResult<()> {
    let body = serde_json::to_vec(response).map_err(|e| format!("响应序列化失败: {e}"))?;
    let header = (body.len() as u32).to_le_bytes();
    write_all(pipe, &header)?;
    write_all(pipe, &body)
}

fn read_exact(pipe: HANDLE, buffer: &mut [u8]) -> AppResult<()> {
    let mut offset = 0;
    while offset < buffer.len() {
        let mut read = 0_u32;
        let success = unsafe {
            ReadFile(
                pipe,
                buffer[offset..].as_mut_ptr(),
                (buffer.len() - offset).min(u32::MAX as usize) as u32,
                &mut read,
                null_mut(),
            )
        };
        if success == 0 || read == 0 {
            return Err("服务请求读取失败".to_string());
        }
        offset += read as usize;
    }
    Ok(())
}

fn write_all(pipe: HANDLE, buffer: &[u8]) -> AppResult<()> {
    let mut offset = 0;
    while offset < buffer.len() {
        let mut written = 0_u32;
        let success = unsafe {
            WriteFile(
                pipe,
                buffer[offset..].as_ptr(),
                (buffer.len() - offset).min(u32::MAX as usize) as u32,
                &mut written,
                null_mut(),
            )
        };
        if success == 0 || written == 0 {
            return Err("服务响应写入失败".to_string());
        }
        offset += written as usize;
    }
    Ok(())
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

fn valid_sid_text(value: &str) -> bool {
    value.starts_with("S-1-")
        && value.len() <= 184
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'-' || byte == b'S')
}
