// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet])
        .setup(|app| {
            let shell = app.handle().shell();
            let sidecar = shell
                .sidecar("app-sidecar")
                .expect("failed to create sidecar command");
            tauri::async_runtime::spawn(async move {
                let (mut rx, _child) = sidecar.spawn().expect("failed to spawn sidecar");
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            let text = String::from_utf8_lossy(&line);
                            println!("SIDECAR_STDOUT: {}", text.trim());
                            if text.trim() == "python-sidecar-ok" {
                                println!("SIDECAR_SPAWN_TEST: PASS");
                                std::process::exit(0);
                            }
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("SIDECAR_STDERR: {}", String::from_utf8_lossy(&line).trim());
                        }
                        CommandEvent::Error(err) => {
                            eprintln!("SIDECAR_SPAWN_TEST: FAIL - {}", err);
                            std::process::exit(1);
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
