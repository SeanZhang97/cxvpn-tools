use crate::{
    constants::SERVICE_ID,
    ipc,
    manager::RuntimeManager,
    util::{program_data_dir, AppResult},
};
use std::{
    ffi::OsString,
    fs,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    time::Duration,
};
use windows_service::{
    define_windows_service,
    service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
        ServiceType,
    },
    service_control_handler::{self, ServiceControlHandlerResult},
    service_dispatcher,
};

const SERVICE_TYPE: ServiceType = ServiceType::OWN_PROCESS;

define_windows_service!(ffi_service_main, service_main);

pub fn dispatch() -> windows_service::Result<()> {
    service_dispatcher::start(SERVICE_ID, ffi_service_main)
}

fn service_main(_arguments: Vec<OsString>) {
    let _ = run();
}

fn run() -> AppResult<()> {
    let (shutdown_tx, shutdown_rx) = mpsc::channel();
    let event_handler = move |control_event| -> ServiceControlHandlerResult {
        match control_event {
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            ServiceControl::Stop | ServiceControl::Shutdown => {
                let _ = shutdown_tx.send(());
                ServiceControlHandlerResult::NoError
            }
            _ => ServiceControlHandlerResult::NotImplemented,
        }
    };
    let status = service_control_handler::register(SERVICE_ID, event_handler)
        .map_err(|e| format!("注册服务控制器失败: {e}"))?;
    status
        .set_service_status(service_status(ServiceState::StartPending, 1))
        .map_err(|e| format!("更新服务状态失败: {e}"))?;

    let base = program_data_dir();
    let owner_sid = fs::read_to_string(base.join("owner.sid"))
        .map_err(|e| format!("读取 IPC 所有者失败: {e}"))?
        .trim()
        .to_string();
    let manager = Arc::new(Mutex::new(RuntimeManager::new(base, owner_sid.clone())?));
    let stop = Arc::new(AtomicBool::new(false));
    {
        let manager = Arc::clone(&manager);
        let stop = Arc::clone(&stop);
        std::thread::spawn(move || ipc::run_listener(manager, stop, owner_sid));
    }

    status
        .set_service_status(service_status(ServiceState::Running, 0))
        .map_err(|e| format!("更新服务状态失败: {e}"))?;
    loop {
        match shutdown_rx.recv_timeout(Duration::from_millis(500)) {
            Ok(_) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Ok(mut state) = manager.lock() {
                    state.tick();
                }
            }
        }
    }
    stop.store(true, Ordering::Relaxed);
    if let Ok(mut state) = manager.lock() {
        state.shutdown();
    }
    status
        .set_service_status(service_status(ServiceState::Stopped, 0))
        .map_err(|e| format!("更新服务状态失败: {e}"))?;
    Ok(())
}

fn service_status(state: ServiceState, checkpoint: u32) -> ServiceStatus {
    ServiceStatus {
        service_type: SERVICE_TYPE,
        current_state: state,
        controls_accepted: if state == ServiceState::Running {
            ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN
        } else {
            ServiceControlAccept::empty()
        },
        exit_code: ServiceExitCode::Win32(0),
        checkpoint,
        wait_hint: if state == ServiceState::StartPending {
            Duration::from_secs(10)
        } else {
            Duration::default()
        },
        process_id: None,
    }
}
