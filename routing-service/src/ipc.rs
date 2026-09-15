use crate::{
    constants::{MAX_REQUEST_BYTES, PIPE_NAME},
    manager::RuntimeManager,
    pipe_io::perform,
    protocol::{Request, Response},
    util::AppResult,
};
use std::{
    ffi::c_void,
    ptr::null_mut,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
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
    Storage::FileSystem::{ReadFile, WriteFile, PIPE_ACCESS_DUPLEX, FILE_FLAG_OVERLAPPED},
    System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_READMODE_BYTE,
        PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_UNLIMITED_INSTANCES, PIPE_WAIT,
    },
};

pub fn run_listener(manager: Arc<Mutex<RuntimeManager>>, stop: Arc<AtomicBool>, owner_sid: String) {
    let active = Arc::new(AtomicUsize::new(0));
    while !stop.load(Ordering::Relaxed) {
        if active.load(Ordering::Relaxed) >= 8 {
            std::thread::sleep(std::time::Duration::from_millis(50));
            continue;
        }
        let pipe = match create_pipe(&owner_sid) {
            Ok(value) => value,
            Err(_) => { std::thread::sleep(std::time::Duration::from_millis(200)); continue; }
        };
        let connected = perform(pipe, std::time::Instant::now() + std::time::Duration::from_secs(1),
            Some(&stop), |overlapped, _| unsafe {
                ConnectNamedPipe(pipe, overlapped) != 0 || GetLastError() == ERROR_PIPE_CONNECTED
            }).is_ok();
        if !connected { unsafe { CloseHandle(pipe) }; continue; }
        active.fetch_add(1, Ordering::Relaxed);
        let count = Arc::clone(&active);
        let state = Arc::clone(&manager);
        // HANDLE 数值在线程间转移所有权，工作线程负责关闭。
        let raw = pipe as usize;
        std::thread::spawn(move || {
            serve_pipe(raw as HANDLE, state);
            count.fetch_sub(1, Ordering::Relaxed);
        });
    }
}

fn serve_pipe(pipe: HANDLE, manager: Arc<Mutex<RuntimeManager>>) {
    let response = match read_request(pipe) {
        Ok(request) if request.op == "apply" => {
            let prepared = match manager.lock() {
                Ok(mut state) => state.begin_prepare(request),
                Err(_) => Err("服务内部状态锁异常".into()),
            };
            match prepared {
                Ok(prepared) => {
                    // 耗时的磁盘准备和子进程预检不占用状态锁，tick/关闭可继续运行。
                    let outcome = prepared.prepare();
                    match manager.lock() {
                        Ok(mut state) => state.finish_prepare(prepared, outcome),
                        Err(_) => Response::error("服务内部状态锁异常"),
                    }
                },
                Err(error) => Response::error(error),
            }
        },
        Ok(request) => match manager.lock() {
            Ok(mut state) => state.handle(request),
            Err(_) => Response::error("服务内部状态锁异常"),
        },
        Err(error) => Response::error(error),
    };
    let _ = write_response(pipe, &response);
    // 写入完成后等待客户端读取完响应再断开；有界确认代替无限 FlushFileBuffers。
    let mut acknowledgement = [0_u8; 1];
    let _ = read_exact(pipe, &mut acknowledgement,
        std::time::Instant::now() + std::time::Duration::from_secs(2));
    unsafe { DisconnectNamedPipe(pipe); CloseHandle(pipe); }
}

fn create_pipe(owner_sid: &str) -> AppResult<HANDLE> {
    create_named_pipe(owner_sid, PIPE_NAME)
}

pub(crate) fn create_named_pipe(owner_sid: &str, pipe_name: &str) -> AppResult<HANDLE> {
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
    let name = wide(pipe_name);
    let handle = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
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
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
    let mut header = [0_u8; 4];
    read_exact(pipe, &mut header, deadline)?;
    let length = u32::from_le_bytes(header);
    if length == 0 || length > MAX_REQUEST_BYTES {
        return Err("服务请求大小超出限制".to_string());
    }
    let mut body = vec![0_u8; length as usize];
    read_exact(pipe, &mut body, deadline)?;
    serde_json::from_slice(&body).map_err(|_| "服务请求格式无效".to_string())
}

fn write_response(pipe: HANDLE, response: &Response) -> AppResult<()> {
    let body = serde_json::to_vec(response).map_err(|e| format!("响应序列化失败: {e}"))?;
    let header = (body.len() as u32).to_le_bytes();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    write_all(pipe, &header, deadline)?;
    write_all(pipe, &body, deadline)
}

pub(crate) fn read_exact(pipe: HANDLE, buffer: &mut [u8], deadline: std::time::Instant) -> AppResult<()> {
    let mut offset = 0;
    while offset < buffer.len() {
        let size = (buffer.len() - offset).min(1024 * 1024) as u32;
        let read = perform(pipe, deadline, None, |overlapped, count| unsafe {
            ReadFile(pipe, buffer[offset..].as_mut_ptr(), size, count, overlapped) != 0
        })?;
        if read == 0 { return Err("管道连接已中断".into()); }
        offset += read as usize;
    }
    Ok(())
}

pub(crate) fn write_all(pipe: HANDLE, buffer: &[u8], deadline: std::time::Instant) -> AppResult<()> {
    let mut offset = 0;
    while offset < buffer.len() {
        let size = (buffer.len() - offset).min(1024 * 1024) as u32;
        let written = perform(pipe, deadline, None, |overlapped, count| unsafe {
            WriteFile(pipe, buffer[offset..].as_ptr(), size, count, overlapped) != 0
        })?;
        if written == 0 { return Err("管道连接已中断".into()); }
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
