//! Talks to `docketd` from background threads so the UI never blocks.

use crate::model::{task_step, TaskStep};
use crate::status_view::FetchError;
use docket_proto::api::{
    parse_ask_event, ApiError, AskEvent, AskRequest, AskResult, Phase, Scope, SourceInfo,
    SourcesResponse, StatusResponse,
};
use docket_proto::client::{self, sse_events};
use docket_proto::task::{parse_task_event, CreateTaskRequest, CreateTaskResponse, TaskKind};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

const SHORT: Duration = Duration::from_secs(5);
const ASK_READ: Duration = Duration::from_secs(600);
/// Status is polled this often (the daemon answers from a cache, never the worker).
const STATUS_EVERY: Duration = Duration::from_secs(2);
/// `/v1/sources` asks the worker, so it is refreshed this often, and only while
/// no question runs (last good list is kept meanwhile).
const SOURCES_EVERY: Duration = Duration::from_secs(10);

pub enum Event {
    Status(Result<StatusResponse, FetchError>),
    Sources(Result<Vec<SourceInfo>, String>),
    Phase(Phase),
    /// A piece of the streamed answer: shown as unverified until `Answer`.
    Delta(String),
    Answer(Box<AskResult>, f64),
    AskFailed(String),
    DaemonStarted(Result<(), String>),
    /// The daemon accepted the question as a task (needed to stop it).
    TaskStarted(String),
    /// The question was stopped; the partial text is gone.
    Cancelled,
    /// The stop request could not be delivered.
    StopFailed(String),
}

pub struct Backend {
    socket: PathBuf,
    tx: Sender<Event>,
    pub rx: Receiver<Event>,
    ctx: eframe::egui::Context,
    daemon: Option<Child>,
    /// A question is running: do not refresh sources (it would queue behind it).
    asking: Arc<AtomicBool>,
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

/// Errors are typed, never carry message text (it could contain paths).
pub fn fetch_status(socket: &Path) -> Result<StatusResponse, FetchError> {
    let r = client::get(socket, "/v1/status", SHORT).map_err(|_| FetchError::Unreachable)?;
    let code = r.status;
    let body = r.text().map_err(|_| FetchError::Unreachable)?;
    if code != 200 {
        return Err(FetchError::Http(code));
    }
    serde_json::from_str(&body).map_err(|_| FetchError::Protocol)
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
            asking: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Tell the poller whether a question is running.
    pub fn set_asking(&self, on: bool) {
        self.asking.store(on, Ordering::Relaxed);
    }

    /// Connect to a running daemon or launch one, then keep polling it.
    pub fn start(&mut self) {
        let socket = self.socket.clone();
        let (tx, ctx) = (self.tx.clone(), self.ctx.clone());
        let asking = self.asking.clone();
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
            let in_flight = Arc::new(AtomicBool::new(false));
            let failed = Arc::new(AtomicBool::new(false));
            let mut last_sources = Instant::now();
            loop {
                let st = fetch_status(&socket);
                let up = st.is_ok();
                // Periodic refresh only against a daemon with `status.v2`
                // (an old daemon keeps today's load-once behaviour).
                let v2 = st
                    .as_ref()
                    .is_ok_and(|s| s.health.is_some() && s.has_capability("status.v2"));
                if !send(&tx, &ctx, Event::Status(st)) {
                    return;
                }
                if failed.swap(false, Ordering::AcqRel) {
                    have_sources = false; // retry soon, as before
                }
                let asking = asking.load(Ordering::Relaxed);
                let due =
                    !have_sources || (v2 && !asking && last_sources.elapsed() >= SOURCES_EVERY);
                if up && due && !in_flight.swap(true, Ordering::AcqRel) {
                    // Own thread: a slow worker never delays the status poll.
                    last_sources = Instant::now();
                    have_sources = true;
                    let (socket, tx, ctx, flag, failed) = (
                        socket.clone(),
                        tx.clone(),
                        ctx.clone(),
                        in_flight.clone(),
                        failed.clone(),
                    );
                    std::thread::spawn(move || {
                        let r = fetch_sources(&socket);
                        failed.store(r.is_err(), Ordering::Release);
                        send(&tx, &ctx, Event::Sources(r));
                        flag.store(false, Ordering::Release);
                    });
                }
                if !up {
                    have_sources = false;
                }
                std::thread::sleep(STATUS_EVERY);
            }
        });
    }

    pub fn refresh_sources(&self) {
        let (socket, tx, ctx) = (self.socket.clone(), self.tx.clone(), self.ctx.clone());
        std::thread::spawn(move || {
            send(&tx, &ctx, Event::Sources(fetch_sources(&socket)));
        });
    }

    /// Ask to stop a running task. The outcome arrives as the task's own
    /// events (`Cancelled`); only a failure to send is reported here.
    pub fn cancel(&self, task_id: String) {
        let (socket, tx, ctx) = (self.socket.clone(), self.tx.clone(), self.ctx.clone());
        std::thread::spawn(move || {
            let path = format!("/v1/tasks/{task_id}/cancel");
            let res = client::request(&socket, "POST", &path, &[], None, Some(SHORT))
                .map_err(|e| e.to_string())
                .and_then(|r| {
                    let code = r.status;
                    let body = r.text().unwrap_or_default();
                    if code == 200 {
                        Ok(())
                    } else {
                        Err(http_error(&body, code))
                    }
                });
            if let Err(e) = res {
                send(&tx, &ctx, Event::StopFailed(format!("could not stop: {e}")));
            }
        });
    }

    /// Ask a question. With `use_tasks` (the daemon advertises `tasks.v1`) it
    /// runs as a task so it can be stopped; otherwise the plain `/v1/ask`.
    pub fn ask(&self, question: String, source_id: Option<String>, use_tasks: bool) {
        if use_tasks {
            self.ask_task(question, source_id);
        } else {
            self.ask_legacy(question, source_id);
        }
    }

    fn ask_task(&self, question: String, source_id: Option<String>) {
        let (socket, tx, ctx) = (self.socket.clone(), self.tx.clone(), self.ctx.clone());
        std::thread::spawn(move || {
            let t0 = Instant::now();
            let req = CreateTaskRequest {
                kind: TaskKind::Ask,
                question,
                scope: source_id.map(|id| Scope {
                    source_id: Some(id),
                    folder: None,
                }),
                stream: true,
                deadline_ms: None,
            };
            let body = serde_json::to_string(&req).unwrap_or_default();
            let fail = |m: String| {
                send(&tx, &ctx, Event::AskFailed(m));
            };
            let resp = match client::post_json(&socket, "/v1/tasks", &body, None, SHORT) {
                Ok(r) => r,
                Err(e) => return fail(format!("cannot reach the service: {e}")),
            };
            let code = resp.status;
            let text = resp.text().unwrap_or_default();
            if code != 202 {
                return fail(http_error(&text, code));
            }
            let Ok(created) = serde_json::from_str::<CreateTaskResponse>(&text) else {
                return fail("unexpected response from the service".into());
            };
            let id = created.task_id.0;
            send(&tx, &ctx, Event::TaskStarted(id.clone()));
            let path = format!("/v1/tasks/{id}/events");
            let resp = match client::request(&socket, "GET", &path, &[], None, Some(ASK_READ)) {
                Ok(r) if r.status == 200 => r,
                Ok(r) => {
                    let code = r.status;
                    return fail(http_error(&r.text().unwrap_or_default(), code));
                }
                Err(e) => return fail(format!("cannot reach the service: {e}")),
            };
            let mut last_error = None;
            for ev in sse_events(resp.body) {
                let Ok(ev) = ev else {
                    return fail("connection to the service was lost".into());
                };
                // Unknown or malformed events are skipped, as for /v1/ask.
                let Some(te) = parse_task_event(&ev.data) else {
                    continue;
                };
                match task_step(&te.body, &mut last_error) {
                    TaskStep::Phase(p) => {
                        send(&tx, &ctx, Event::Phase(p));
                    }
                    TaskStep::Delta(t) => {
                        send(&tx, &ctx, Event::Delta(t));
                    }
                    TaskStep::Answer(r) => {
                        send(&tx, &ctx, Event::Answer(r, t0.elapsed().as_secs_f64()));
                        return;
                    }
                    TaskStep::Failed(m) => return fail(m),
                    TaskStep::Cancelled => {
                        send(&tx, &ctx, Event::Cancelled);
                        return;
                    }
                    TaskStep::Skip => {}
                }
            }
            fail("the service ended the answer early".into());
        });
    }

    fn ask_legacy(&self, question: String, source_id: Option<String>) {
        let (socket, tx, ctx) = (self.socket.clone(), self.tx.clone(), self.ctx.clone());
        std::thread::spawn(move || {
            let t0 = Instant::now();
            let req = AskRequest {
                question,
                scope: source_id.map(|id| Scope {
                    source_id: Some(id),
                    folder: None,
                }),
                stream: true,
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
                match parse_ask_event(&ev.event, &ev.data) {
                    Some(AskEvent::Phase(p)) => {
                        send(&tx, &ctx, Event::Phase(p));
                    }
                    Some(AskEvent::Delta(t)) => {
                        send(&tx, &ctx, Event::Delta(t));
                    }
                    Some(AskEvent::Result(r)) => {
                        send(&tx, &ctx, Event::Answer(r, t0.elapsed().as_secs_f64()));
                        return;
                    }
                    Some(AskEvent::Error(msg)) => {
                        send(&tx, &ctx, Event::AskFailed(msg));
                        return;
                    }
                    None if ev.event == "result" => break,
                    None => {}
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
