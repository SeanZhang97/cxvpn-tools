use crate::{ipc::{create_named_pipe, read_exact, write_all}, pipe_io::perform,
    util::AppResult, vpn_routes::{Policy, Shared}};
use serde::Deserialize;
use std::{sync::{Arc, atomic::{AtomicBool, AtomicUsize, Ordering}}, time::{Duration, Instant}};
use windows_sys::Win32::{Foundation::{CloseHandle, GetLastError, ERROR_PIPE_CONNECTED, HANDLE},
    System::Pipes::{ConnectNamedPipe, DisconnectNamedPipe, GetNamedPipeClientProcessId}};

pub const PIPE: &str = r"\\.\pipe\CXVPNRouteLease.v1";
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request { op: String, epoch: String, id: String, #[serde(default)] vpn: String, #[serde(default)] ip: String }

pub fn start(state: Shared, stop: Arc<AtomicBool>, owner: String) {
    let worker_state = state.clone();
    let worker_stop = stop.clone();
    std::thread::spawn(move || {
        let mut last_error = String::new();
        while !worker_stop.load(Ordering::Relaxed) {
            if let Ok(mut s) = worker_state.try_lock() {
                let result = s.sweep(false);
                if let Err(error) = result {
                    if error != last_error { s.log(&format!("route cleanup failed: {error}")); last_error = error; }
                } else { last_error.clear(); }
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    });
    std::thread::spawn(move || listen(state, stop, owner));
}
fn listen(state: Shared, stop: Arc<AtomicBool>, owner: String) {
    let count = Arc::new(AtomicUsize::new(0));
    while !stop.load(Ordering::Relaxed) {
        if count.load(Ordering::Relaxed) >= 32 { std::thread::sleep(Duration::from_millis(10)); continue; }
        let pipe = match create_named_pipe(&owner, PIPE) {
            Ok(pipe) => pipe,
            Err(error) => {
                if let Ok(s) = state.lock() { s.log(&format!("route listener failed: {error}")); }
                std::thread::sleep(Duration::from_secs(1)); continue;
            }
        };
        let connected = perform(pipe, Instant::now() + Duration::from_secs(1), Some(&stop), |overlapped, _| unsafe {
            ConnectNamedPipe(pipe, overlapped) != 0 || GetLastError() == ERROR_PIPE_CONNECTED
        });
        if connected.is_err() { unsafe { CloseHandle(pipe); } continue; }
        count.fetch_add(1, Ordering::Relaxed);
        let active = count.clone();
        let state = state.clone();
        let raw = pipe as usize;
        std::thread::spawn(move || {
            let pipe = raw as HANDLE;
            let deadline = Instant::now() + Duration::from_secs(2);
            let result = serve(pipe, &state, deadline);
            let body = match result {
                Ok(interface) => serde_json::json!({"ok": true, "interface": interface}),
                Err(error) => serde_json::json!({"ok": false, "error": error}),
            };
            if let Ok(body) = serde_json::to_vec(&body) {
                let until = Instant::now() + Duration::from_millis(300);
                let _ = write_all(pipe, &(body.len() as u32).to_le_bytes(), until)
                    .and_then(|_| write_all(pipe, &body, until));
                let _ = read_exact(pipe, &mut [0], until);
            }
            unsafe { DisconnectNamedPipe(pipe); CloseHandle(pipe); }
            active.fetch_sub(1, Ordering::Relaxed);
        });
    }
    if let Ok(mut s) = state.lock() {
        if let Err(error) = s.reset(Policy::default()) { s.log(&format!("route shutdown cleanup failed: {error}")); }
    }
}
fn serve(pipe: HANDLE, state: &Shared, deadline: Instant) -> AppResult<Option<String>> {
    let mut pid = 0;
    if unsafe { GetNamedPipeClientProcessId(pipe, &mut pid) } == 0 { return Err("cannot identify route client".into()); }
    let mut header = [0; 4];
    read_exact(pipe, &mut header, deadline)?;
    let size = u32::from_le_bytes(header) as usize;
    if size == 0 || size > 4096 { return Err("route request exceeds frame limit".into()); }
    let mut body = vec![0; size];
    read_exact(pipe, &mut body, deadline)?;
    let request: Request = serde_json::from_slice(&body).map_err(|_| "invalid route request")?;
    if request.id.is_empty() || request.id.len() > 20 || !request.id.bytes().all(|b| b.is_ascii_digit()) {
        return Err("invalid route request id".into());
    }
    loop {
        if Instant::now() >= deadline { return Err("route request timeout waiting for worker".into()); }
        match state.try_lock() {
            Ok(mut state) => {
                if !state.policy.permits(pid, &request.epoch) { return Err("route client generation not permitted".into()); }
                let result = match request.op.as_str() {
                    "acquire" => state.acquire(&request.id, &request.vpn,
                        request.ip.parse().map_err(|_| "invalid route IP")?, deadline),
                    "release" => state.release(&request.id),
                    _ => Err("unsupported route operation".into()),
                };
                if let Err(error) = &result { state.log(&format!("request failed id={}: {error}", request.id)); }
                result?;
                return if request.op == "acquire" { state.outbound(&request.id).map(Some) } else { Ok(None) };
            },
            Err(std::sync::TryLockError::Poisoned(_)) => return Err("route state unavailable".into()),
            Err(_) => std::thread::sleep(Duration::from_millis(5)),
        }
    }
}
