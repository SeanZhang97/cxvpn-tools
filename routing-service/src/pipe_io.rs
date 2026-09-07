use crate::util::AppResult;
use std::{ptr::null_mut, sync::atomic::{AtomicBool, Ordering}, time::Instant};
use windows_sys::Win32::{
    Foundation::{CloseHandle, GetLastError, HANDLE, ERROR_IO_PENDING},
    System::{IO::{CancelIoEx, GetOverlappedResult, OVERLAPPED},
        Threading::{CreateEventW, WaitForSingleObject}},
};

// 超时/关闭时先取消并等待 I/O 完成，保证 OVERLAPPED 和缓冲区不被提前释放。
pub fn perform<F>(pipe: HANDLE, deadline: Instant, stop: Option<&AtomicBool>, start: F) -> AppResult<u32>
where F: FnOnce(*mut OVERLAPPED, *mut u32) -> bool {
    let event = unsafe { CreateEventW(null_mut(), 1, 0, null_mut()) };
    if event.is_null() { return Err("创建管道事件失败".into()); }
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    overlapped.hEvent = event;
    let mut count = 0;
    let result = (|| {
        if start(&mut overlapped, &mut count) { return Ok(count); }
        if unsafe { GetLastError() } != ERROR_IO_PENDING { return Err("管道读写失败".into()); }
        loop {
            if Instant::now() >= deadline || stop.is_some_and(|value| value.load(Ordering::Relaxed)) {
                unsafe {
                    CancelIoEx(pipe, &overlapped);
                    GetOverlappedResult(pipe, &overlapped, &mut count, 1);
                }
                return Err("管道等待超时或已取消".into());
            }
            let timeout = deadline.saturating_duration_since(Instant::now()).as_millis().min(200) as u32;
            let state = unsafe { WaitForSingleObject(event, timeout.max(1)) };
            if state == 0 {
                return if unsafe { GetOverlappedResult(pipe, &overlapped, &mut count, 0) } != 0 {
                    Ok(count)
                } else { Err("管道连接已中断".into()) };
            }
            if state != 258 {
                unsafe {
                    CancelIoEx(pipe, &overlapped);
                    GetOverlappedResult(pipe, &overlapped, &mut count, 1);
                }
                return Err("管道事件等待失败".into());
            }
        }
    })();
    unsafe { CloseHandle(event) };
    result
}
