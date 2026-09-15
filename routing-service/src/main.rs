mod constants;
mod installer;
mod ipc;
mod prepared;
mod pipe_io;
mod manager;
mod protocol;
mod service;
mod system_proxy;
mod util;
mod core_job;
mod vpn_route_os;
mod vpn_routes;
mod vpn_route_ipc;

use std::{fs, path::PathBuf};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|value| value == "--install") {
        let owner_sid = argument(&args, "--owner-sid").unwrap_or_default();
        let result = installer::install(&owner_sid);
        finish_command(result, argument(&args, "--result-file"));
        return;
    }
    if args.iter().any(|value| value == "--uninstall") {
        let result = installer::uninstall();
        finish_command(result, argument(&args, "--result-file"));
        return;
    }
    if let Err(error) = service::dispatch() {
        let _ = fs::write(
            util::program_data_dir().join("service-start-error.log"),
            error.to_string(),
        );
    }
}

fn argument(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|value| value == name)
        .and_then(|index| args.get(index + 1))
        .cloned()
}

fn finish_command(result: Result<(), String>, result_file: Option<String>) {
    let (success, message) = match result {
        Ok(()) => (true, "OK".to_string()),
        Err(error) => (false, format!("ERROR:{error}")),
    };
    if let Some(path) = result_file.map(PathBuf::from) {
        let _ = fs::write(path, message.as_bytes());
    }
    if !success {
        std::process::exit(1);
    }
}
