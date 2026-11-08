//! Talks to `docketd` from background threads so the UI never blocks.

use docket_proto::api::{
    ApiError, AskRequest, AskResult, Phase, Scope, SourceInfo, SourcesResponse, StatusResponse,
};
use docket_proto::client::{self, sse_events};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::time::{Duration, Instant};

const SHORT: Duration = Duration::from_secs(5);
const ASK_READ: Duration = Duration::from_secs(600);

pub enum Event {
    Status(Result<StatusResponse, String>),
    Sources(Result<Vec<SourceInfo>, String>),
    Phase(Phase),
    Answer(Box<AskResult>, f64),
    AskFailed(String),
    DaemonStarted(Result<(), String>),
}

pub struct Backend {
    socket: PathBuf,
    tx: Sender<Event>,
    pub rx: Receiver<Event>,
    ctx: eframe::egui::Context,
    daemon: Option<Child>,
}

fn send(tx: &Sender<Event>, ctx: &eframe::egui::Context, ev: Event) -> bool {
    let ok = tx.send(ev).is_ok();
    ctx.request_repaint();
    ok
}

pub fn socket_path() -> Result<PathBuf, String> {
    match std::env::var_os("DOCKET_SOCKET") {
        Some(p) => Ok(PathBuf::from(p)),
        None => client::default_socket_path(),
    }
}

fn http_error(body: &str, status: u16) -> String {
    match serde_json::from_str::<ApiError>(body) {
        Ok(e) => e.error.message,
        Err(_) => format!("service returned HTTP {status}"),
    }
}

pub fn fetch_status(socket: &Path) -> Result<StatusResponse, String> {
    let r = client::get(socket, "/v1/status", SHORT).map_err(|e| e.to_string())?;
    let code = r.status;
    let body = r.text().map_err(|e| e.to_string())?;
    if code != 200 {
        return Err(http_error(&body, code));
    }
    serde_json::from_str(&body).map_err(|e| e.to_string())
}

fn fetch_sources(socket: &Path) -> Result<Vec<SourceInfo>, String> {
    let r =
        client::get(socket, "/v1/sources", Duration::from_secs(30)).map_err(|e| e.to_string())?;
    let code = r.status;
    let body = r.text().map_err(|e| e.to_string())?;
    if code != 200 {
        return Err(http_error(&body, code));
    }
    serde_json::from_str::<SourcesResponse>(&body)
        .map(|s| s.sources)
        .map_err(|e| e.to_string())
}

/// Where to find the daemon binary: `DOCKETD_BIN`, next to this executable, or PATH.
fn daemon_binary() -> PathBuf {
    if let Some(p) = std::env::var_os("DOCKETD_BIN") {
        return PathBuf::from(p);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let cand = dir.join("docketd");
            if cand.exists() {
                return cand;
            }
        }
    }
    PathBuf::from("docketd")
}

impl Backend {
    pub fn new(socket: PathBuf, ctx: eframe::egui::Context) -> Self {
        let (tx, rx) = channel();
        Self {
            socket,
            tx,
            rx,
            ctx,
            daemon: None,
        }
    }

    /// Connect to a running daemon or launch one, then keep polling it.
    pub fn start(&mut self) {
        let socket = self.socket.clone();
        let (tx, ctx) = (self.tx.clone(), self.ctx.clone());
        if fetch_status(&socket).is_err() {
            match Command::new(daemon_binary())
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .spawn()
            {
                Ok(child) => self.daemon = Some(child),
                Err(e) => {
                    send(
                        &tx,
                        &ctx,
                        Event::DaemonStarted(Err(format!("could not start docketd: {e}"))),
                    );
                }
            }
        }
        std::thread::spawn(move || {
            // Wait for the socket to answer (only matters when we launched it).
            let t0 = Instant::now();
            let mut started = false;
            while t0.elapsed() < Duration::from_secs(15) {
                if fetch_status(&socket).is_ok() {
                    started = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(150));
            }
            if started {
                send(&tx, &ctx, Event::DaemonStarted(Ok(())));
            }
            let mut have_sources = false;
            loop {
                let st = fetch_status(&socket);
                let up = st.is_ok();
                if !send(&tx, &ctx, Event::Status(st)) {
                    return;
                }
                if up && !have_sources {
                    let s = fetch_sources(&socket);
                    have_sources = s.is_ok();
                    send(&tx, &ctx, Event::Sources(s));
                }
                if !up {
                    have_sources = false;
                }
                std::thread::sleep(Duration::from_secs(2));
            }
        });
    }

    pub fn refresh_sources(&self) {
        let (socket, tx, ctx) = (self.socket.clone(), self.tx.clone(), self.ctx.clone());
        std::thread::spawn(move || {
            send(&tx, &ctx, Event::Sources(fetch_sources(&socket)));
        });
    }

    pub fn ask(&self, question: String, source_id: Option<String>) {
        let (socket, tx, ctx) = (self.socket.clone(), self.tx.clone(), self.ctx.clone());
        std::thread::spawn(move || {
            let t0 = Instant::now();
            let req = AskRequest {
                question,
                scope: source_id.map(|id| Scope {
                    source_id: Some(id),
                    folder: None,
                }),
            };
            let body = serde_json::to_string(&req).unwrap_or_default();
            let resp = match client::post_json(
                &socket,
                "/v1/ask",
                &body,
                Some("text/event-stream"),
                ASK_READ,
            ) {
                Ok(r) => r,
                Err(e) => {
                    send(
                        &tx,
                        &ctx,
                        Event::AskFailed(format!("cannot reach the service: {e}")),
                    );
                    return;
                }
            };
            if resp.status != 200 {
                let code = resp.status;
                let text = resp.text().unwrap_or_default();
                send(&tx, &ctx, Event::AskFailed(http_error(&text, code)));
                return;
            }
            for ev in sse_events(resp.body) {
                let Ok(ev) = ev else {
                    send(
                        &tx,
                        &ctx,
                        Event::AskFailed("connection to the service was lost".into()),
                    );
                    return;
                };
                match ev.event.as_str() {
                    "phase" => {
                        let p = serde_json::from_str::<serde_json::Value>(&ev.data)
                            .ok()
                            .and_then(|v| Phase::parse(v.get("phase")?.as_str()?));
                        if let Some(p) = p {
                            send(&tx, &ctx, Event::Phase(p));
                        }
                    }
                    "result" => match serde_json::from_str::<AskResult>(&ev.data) {
                        Ok(r) => {
                            send(
                                &tx,
                                &ctx,
                                Event::Answer(Box::new(r), t0.elapsed().as_secs_f64()),
                            );
                            return;
                        }
                        Err(_) => break,
                    },
                    "error" => {
                        let msg = serde_json::from_str::<ApiError>(&ev.data)
                            .map(|e| e.error.message)
                            .unwrap_or_else(|_| "the question could not be answered".into());
                        send(&tx, &ctx, Event::AskFailed(msg));
                        return;
                    }
                    _ => {}
                }
            }
            send(
                &tx,
                &ctx,
                Event::AskFailed("the service ended the answer early".into()),
            );
        });
    }
}

impl Drop for Backend {
    /// Stop a daemon this app launched; leave a pre-existing one running.
    fn drop(&mut self) {
        if let Some(mut child) = self.daemon.take() {
            let _ = Command::new("kill")
                .args(["-TERM", &child.id().to_string()])
                .status();
            let t0 = Instant::now();
            while t0.elapsed() < Duration::from_secs(4) {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}
