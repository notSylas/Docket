// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
//
// This file implements a persistent Rust<->Python IPC bridge over the
// `app-sidecar` sidecar process's stdin/stdout. Requests are JSON lines of
// the form `{"id": "...", "op": "...", "params": {...}}` written to the
// child's stdin; responses are JSON lines of the form
// `{"id": "...", "ok": bool, "result": ...}` or
// `{"id": "...", "ok": false, "error": {...}}` read from its stdout and
// correlated back to the caller via a map of pending oneshot senders keyed
// by request id.
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::Value;
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::oneshot;

/// Shared state for the sidecar bridge: the spawned child process (so we can
/// write to its stdin from command handlers) and the table of in-flight
/// requests awaiting a response, keyed by request id.
struct SidecarState {
    child: Mutex<Option<CommandChild>>,
    pending: Mutex<HashMap<String, oneshot::Sender<Value>>>,
    next_id: AtomicU64,
}

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

/// Send one `{op, params}` request to the sidecar and await its matching
/// response, correlated by request id.
async fn call_sidecar(state: &SidecarState, op: &str, params: Value) -> Result<Value, String> {
    let id = format!("req_{}", state.next_id.fetch_add(1, Ordering::SeqCst));

    let (tx, rx) = oneshot::channel::<Value>();
    {
        let mut pending = state
            .pending
            .lock()
            .map_err(|_| "pending-requests mutex poisoned".to_string())?;
        pending.insert(id.clone(), tx);
    }

    let request = serde_json::json!({
        "id": id,
        "op": op,
        "params": params,
    });
    let mut line = serde_json::to_string(&request).map_err(|e| e.to_string())?;
    line.push('\n');

    // Write while holding the child lock, then drop the guard before
    // awaiting the oneshot receiver below -- otherwise we'd hold the lock
    // across an .await and block every other in-flight caller's write.
    {
        let mut child_guard = state
            .child
            .lock()
            .map_err(|_| "sidecar child mutex poisoned".to_string())?;
        let child = child_guard
            .as_mut()
            .ok_or_else(|| "sidecar process is not running".to_string())?;
        child
            .write(line.as_bytes())
            .map_err(|e| format!("failed to write to sidecar stdin: {e}"))?;
    }

    let response = rx.await.map_err(|_| {
        "sidecar bridge dropped the pending request before a response arrived".to_string()
    })?;

    let ok = response
        .get("ok")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if ok {
        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    } else {
        let error = response.get("error").cloned().unwrap_or_else(|| {
            serde_json::json!({
                "type": "UnknownError",
                "message": "sidecar returned ok=false with no error detail",
            })
        });
        Err(error.to_string())
    }
}

/// Test-only wrapper for this checkpoint's verification of the bridge
/// mechanism. Real backend-op wrappers (list_sources, etc.) are a later
/// checkpoint.
#[tauri::command]
async fn echo_test(
    state: tauri::State<'_, SidecarState>,
    message: String,
) -> Result<Value, String> {
    call_sidecar(&state, "echo", serde_json::json!({ "message": message })).await
}

/// Test-only wrapper for this checkpoint's verification of the bridge
/// mechanism.
#[tauri::command]
async fn ping_sidecar(state: tauri::State<'_, SidecarState>) -> Result<Value, String> {
    call_sidecar(&state, "ping", serde_json::json!({})).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet, echo_test, ping_sidecar])
        .setup(|app| {
            // Managed before the stdout-reading task below ever tries to
            // fetch it via `app_handle.state::<SidecarState>()`.
            app.manage(SidecarState {
                child: Mutex::new(None),
                pending: Mutex::new(HashMap::new()),
                next_id: AtomicU64::new(0),
            });

            let shell = app.handle().shell();
            let sidecar = shell
                .sidecar("app-sidecar")
                .expect("failed to create sidecar command");

            // The spawned task below outlives this setup closure, so it
            // needs an owned AppHandle clone (cheap: it's just a handle),
            // not a borrow of `app`.
            let app_handle: AppHandle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let (mut rx, child) = match sidecar.spawn() {
                    Ok(pair) => pair,
                    Err(err) => {
                        eprintln!("SIDECAR_BRIDGE: failed to spawn sidecar: {err}");
                        return;
                    }
                };

                {
                    let state = app_handle.state::<SidecarState>();
                    let mut child_guard = state
                        .child
                        .lock()
                        .expect("sidecar child mutex poisoned");
                    *child_guard = Some(child);
                }

                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            let text = String::from_utf8_lossy(&line);
                            let trimmed = text.trim();
                            if trimmed.is_empty() {
                                continue;
                            }

                            let parsed: Value = match serde_json::from_str(trimmed) {
                                Ok(v) => v,
                                Err(err) => {
                                    eprintln!(
                                        "SIDECAR_BRIDGE: failed to parse stdout line as JSON ({err}): {trimmed}"
                                    );
                                    continue;
                                }
                            };

                            let id = match parsed.get("id").and_then(Value::as_str) {
                                Some(id) => id.to_string(),
                                None => {
                                    eprintln!(
                                        "SIDECAR_BRIDGE: stdout line has no \"id\" field, ignoring: {trimmed}"
                                    );
                                    continue;
                                }
                            };

                            let state = app_handle.state::<SidecarState>();
                            let sender = {
                                let mut pending = state
                                    .pending
                                    .lock()
                                    .expect("pending-requests mutex poisoned");
                                pending.remove(&id)
                            };

                            match sender {
                                Some(tx) => {
                                    if tx.send(parsed).is_err() {
                                        eprintln!(
                                            "SIDECAR_BRIDGE: no one was waiting for response id {id} (caller dropped)"
                                        );
                                    }
                                }
                                None => {
                                    eprintln!(
                                        "SIDECAR_BRIDGE: no pending request matches response id {id}, ignoring"
                                    );
                                }
                            }
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("SIDECAR_STDERR: {}", String::from_utf8_lossy(&line).trim());
                        }
                        CommandEvent::Error(err) => {
                            // Don't crash the whole app over one sidecar-side
                            // error -- log it. Any requests already pending
                            // will simply never resolve; robust lifecycle
                            // handling (timeouts, restarts) is a later
                            // checkpoint.
                            eprintln!("SIDECAR_BRIDGE: sidecar error: {err}");
                        }
                        CommandEvent::Terminated(payload) => {
                            eprintln!("SIDECAR_BRIDGE: sidecar process terminated: {:?}", payload);
                        }
                        _ => {}
                    }
                }

                eprintln!("SIDECAR_BRIDGE: sidecar stdout stream closed, bridge task exiting");
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
