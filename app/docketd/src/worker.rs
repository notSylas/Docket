//! Supervision of the Python worker process.
//!
//! One task owns the child. Callers send it `Call`s over a channel, so at most
//! one request is on the wire at a time. It restarts a dead or hung worker
//! with exponential backoff, pings it while idle, and shuts it down cleanly.
//! A policy/approval layer can later wrap `WorkerHandle::call`: every tool
//! invocation already goes through this one function.

use crate::config::Config;
use docket_proto::api::{WorkerInfo, WorkerState, WorkerStatus};
use docket_proto::rpc::{self, Incoming, Request, RpcError};
use docket_proto::task::{CancelSource, WorkerExit};
use serde_json::{json, Value};
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::time::{timeout_at, Instant};
use tracing::{debug, info, warn};

/// Cooperative cancel flag for one call. The first `cancel` wins and records
/// who asked. Cancelling never touches the worker by itself: the supervisor
/// observes the token while it owns the call.
#[derive(Clone)]
pub struct CancelToken(Arc<watch::Sender<Option<CancelSource>>>);

impl Default for CancelToken {
    fn default() -> Self {
        Self::new()
    }
}

impl CancelToken {
    pub fn new() -> Self {
        Self(Arc::new(watch::channel(None).0))
    }

    /// True if this call set the source (it was not already cancelled).
    pub fn cancel(&self, by: CancelSource) -> bool {
        self.0.send_if_modified(|v| {
            if v.is_none() {
                *v = Some(by);
                true
            } else {
                false
            }
        })
    }

    pub fn source(&self) -> Option<CancelSource> {
        *self.0.borrow()
    }

    pub fn is_cancelled(&self) -> bool {
        self.source().is_some()
    }

    pub async fn cancelled(&self) -> CancelSource {
        let mut rx = self.0.subscribe();
        loop {
            if let Some(s) = *rx.borrow_and_update() {
                return s;
            }
            if rx.changed().await.is_err() {
                std::future::pending::<()>().await;
            }
        }
    }
}

/// One progress notification from the worker, tagged with the generation of
/// the worker process that sent it (1 for the first process, +1 per launch).
#[derive(Debug, Clone)]
pub struct Progress {
    pub generation: u64,
    pub params: Value,
}

/// Per-call options. `task` only labels log lines (never user content).
#[derive(Default)]
pub struct CallOpts {
    pub timeout: Duration,
    pub progress: Option<mpsc::UnboundedSender<Progress>>,
    pub cancel: Option<CancelToken>,
    pub task: Option<String>,
}

impl CallOpts {
    pub fn timeout(timeout: Duration) -> Self {
        Self {
            timeout,
            ..Self::default()
        }
    }
}

#[derive(Debug)]
pub enum CallError {
    /// No worker could be started (or one is in restart backoff).
    Unavailable(String),
    Timeout,
    /// The worker exited or closed its output mid-call.
    Crashed,
    /// The worker answered with a JSON-RPC error.
    Rpc(RpcError),
    /// The call was cancelled. `worker` says how its process ended, or is
    /// `None` when the worker never received the request.
    Cancelled {
        by: CancelSource,
        worker: Option<WorkerExit>,
    },
}

struct Call {
    method: String,
    params: Value,
    opts: CallOpts,
    reply: oneshot::Sender<Result<Value, CallError>>,
}

/// What the supervisor knows about the worker process beyond `WorkerInfo`.
/// Read by `/v1/status`; never contains messages, only closed codes.
#[derive(Debug, Clone, Default)]
pub struct WorkerMeta {
    /// `features` of the worker's last start-up `ping` (empty for an old
    /// worker). Kept while the worker is down so the capability does not flap.
    pub features: Vec<String>,
    /// Generation of the current (or last) worker process; 0 before launch.
    pub generation: u64,
    /// Unexpected failures: crash, hung call, failed start. Not user cancels.
    pub crashes: u64,
    pub last_error_code: Option<&'static str>,
    /// Epoch ms when the worker entered its current state.
    pub since_ms: u64,
    /// Epoch ms of the last successful exchange of any kind.
    pub last_ok_ms: Option<u64>,
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Clone)]
pub struct WorkerHandle {
    tx: mpsc::Sender<Call>,
    info: Arc<Mutex<WorkerInfo>>,
    meta: Arc<Mutex<WorkerMeta>>,
}

impl WorkerHandle {
    /// Start the supervisor task. It stops when `shutdown` flips to true.
    pub fn spawn(
        cfg: &Config,
        shutdown: watch::Receiver<bool>,
    ) -> (Self, tokio::task::JoinHandle<()>) {
        let (tx, rx) = mpsc::channel(8);
        let info = Arc::new(Mutex::new(WorkerInfo {
            state: WorkerState::Down,
            restarts: 0,
            model: String::new(),
            chunks: None,
        }));
        let meta = Arc::new(Mutex::new(WorkerMeta {
            since_ms: now_ms(),
            ..WorkerMeta::default()
        }));
        let sup = Supervisor {
            cfg: cfg.clone(),
            info: info.clone(),
            meta: meta.clone(),
            child: None,
            generation: 0,
            next_id: 1,
            failures: 0,
            retry_at: Instant::now(),
            starts: 0,
        };
        let task = tokio::spawn(sup.run(rx, shutdown));
        (Self { tx, info, meta }, task)
    }

    pub fn info(&self) -> WorkerInfo {
        self.info.lock().map(|g| g.clone()).unwrap_or(WorkerInfo {
            state: WorkerState::Down,
            restarts: 0,
            model: String::new(),
            chunks: None,
        })
    }

    pub fn meta(&self) -> WorkerMeta {
        self.meta.lock().map(|g| g.clone()).unwrap_or_default()
    }

    pub async fn call(
        &self,
        method: &str,
        params: Value,
        opts: CallOpts,
    ) -> Result<Value, CallError> {
        let (reply, rx) = oneshot::channel();
        let call = Call {
            method: method.into(),
            params,
            opts,
            reply,
        };
        self.tx
            .send(call)
            .await
            .map_err(|_| CallError::Unavailable("service is shutting down".into()))?;
        rx.await.unwrap_or(Err(CallError::Unavailable(
            "service is shutting down".into(),
        )))
    }
}

struct Running {
    pid: Option<u32>,
    child: Child,
    stdin: ChildStdin,
    lines: mpsc::Receiver<String>,
}

struct Supervisor {
    cfg: Config,
    info: Arc<Mutex<WorkerInfo>>,
    meta: Arc<Mutex<WorkerMeta>>,
    child: Option<Running>,
    /// Generation of the current worker process; 0 before the first launch.
    generation: u64,
    next_id: u64,
    failures: u32,
    retry_at: Instant,
    starts: u64,
}

impl Supervisor {
    fn set_state(&self, state: WorkerState) {
        let changed = match self.info.lock() {
            Ok(mut i) => {
                let changed = i.state != state;
                i.state = state;
                changed
            }
            Err(_) => false,
        };
        if changed {
            if let Ok(mut m) = self.meta.lock() {
                m.since_ms = now_ms();
            }
        }
    }

    fn note_ok(&self) {
        if let Ok(mut m) = self.meta.lock() {
            m.last_ok_ms = Some(now_ms());
        }
    }

    fn note_failure(&self, code: &'static str) {
        if let Ok(mut m) = self.meta.lock() {
            m.crashes += 1;
            m.last_error_code = Some(code);
        }
    }

    async fn run(mut self, mut rx: mpsc::Receiver<Call>, mut shutdown: watch::Receiver<bool>) {
        let mut tick = tokio::time::interval(self.cfg.ping_interval);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        // Start eagerly so the first question does not pay for Python start-up.
        self.maintain().await;
        loop {
            tokio::select! {
                biased;
                _ = shutdown.changed() => break,
                call = rx.recv() => match call {
                    Some(c) => self.handle(c).await,
                    None => break,
                },
                _ = tick.tick() => self.maintain().await,
            }
        }
        self.stop_child().await;
        self.set_state(WorkerState::Down);
    }

    /// Idle upkeep: (re)start when due, otherwise ping.
    async fn maintain(&mut self) {
        if self.child.is_none() {
            if Instant::now() >= self.retry_at {
                let _ = self.ensure_started().await;
            }
            return;
        }
        let t = self.cfg.call_timeout;
        if let Err(e) = self
            .exchange("ping", Value::Null, CallOpts::timeout(t))
            .await
        {
            warn!(target: "docketd::worker", error = ?e_kind(&e), "idle ping failed");
        }
    }

    async fn handle(&mut self, call: Call) {
        // Cancelled while still queued behind another call: the worker never
        // saw the request, so there is nothing to terminate.
        if let Some(by) = call.opts.cancel.as_ref().and_then(CancelToken::source) {
            let _ = call
                .reply
                .send(Err(CallError::Cancelled { by, worker: None }));
            return;
        }
        let result = match self.ensure_started().await {
            Ok(()) => self.exchange(&call.method, call.params, call.opts).await,
            Err(e) => Err(e),
        };
        let cancelled_by = match &result {
            Err(CallError::Cancelled { by, .. }) => Some(*by),
            _ => None,
        };
        let _ = call.reply.send(result);
        // A user cancel is not a failure: bring the worker back immediately,
        // with no backoff. Not during shutdown.
        if cancelled_by.is_some_and(|b| b != CancelSource::Shutdown) && self.child.is_none() {
            let _ = self.ensure_started().await;
        }
    }

    fn schedule_backoff(&mut self) {
        self.failures = self.failures.saturating_add(1);
        let factor = 1u32 << (self.failures - 1).min(16);
        let delay = (self.cfg.backoff_base * factor).min(self.cfg.backoff_max);
        self.retry_at = Instant::now() + delay;
        debug!(target: "docketd::worker", delay_ms = delay.as_millis() as u64, "restart backoff");
    }

    async fn ensure_started(&mut self) -> Result<(), CallError> {
        if self.child.is_some() {
            return Ok(());
        }
        if Instant::now() < self.retry_at {
            return Err(CallError::Unavailable(
                "worker is restarting; try again shortly".into(),
            ));
        }
        self.set_state(WorkerState::Starting);
        let mut cmd = Command::new(&self.cfg.worker_cmd[0]);
        cmd.args(&self.cfg.worker_cmd[1..])
            // Own process group, so a kill reaches anything the worker spawned.
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if let Some(d) = &self.cfg.data_dir {
            cmd.env("DOCKET_DATA_DIR", d);
        }
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                warn!(target: "docketd::worker", error = %e, "worker spawn failed");
                self.note_failure("spawn_failed");
                self.set_state(WorkerState::Down);
                self.schedule_backoff();
                return Err(CallError::Unavailable("could not start the worker".into()));
            }
        };
        let (Some(stdin), Some(stdout), Some(stderr)) =
            (child.stdin.take(), child.stdout.take(), child.stderr.take())
        else {
            self.set_state(WorkerState::Down);
            self.schedule_backoff();
            return Err(CallError::Unavailable("worker pipes unavailable".into()));
        };
        let (ltx, lrx) = mpsc::channel(256);
        tokio::spawn(async move {
            let mut r = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match r.read_until(b'\n', &mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        let s = String::from_utf8_lossy(&buf).into_owned();
                        if ltx.send(s).await.is_err() {
                            break;
                        }
                    }
                }
            }
        });
        tokio::spawn(async move {
            let mut r = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = r.next_line().await {
                info!(target: "worker", "{line}");
            }
        });
        self.generation += 1;
        if let Ok(mut m) = self.meta.lock() {
            m.generation = self.generation;
            m.features.clear();
        }
        self.child = Some(Running {
            pid: child.id(),
            child,
            stdin,
            lines: lrx,
        });
        self.starts += 1;
        info!(target: "docketd::worker", starts = self.starts,
            generation = self.generation, "worker launched");

        let startup = self.cfg.startup_timeout;
        match self
            .exchange("ping", Value::Null, CallOpts::timeout(startup))
            .await
        {
            Ok(pong) => {
                // Additive capabilities; an old worker has none.
                let features: Vec<String> = pong
                    .get("features")
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .filter(|f| f.len() <= 40)
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default();
                if let Ok(mut m) = self.meta.lock() {
                    m.features = features;
                }
            }
            Err(e) => {
                warn!(target: "docketd::worker", error = ?e_kind(&e), "worker failed its first ping");
                return Err(CallError::Unavailable("worker did not become ready".into()));
            }
        }
        // Best effort: cache model name and index size for /v1/status.
        let t = self.cfg.call_timeout;
        if let Ok(v) = self
            .exchange("status", Value::Null, CallOpts::timeout(t))
            .await
        {
            if let Ok(s) = serde_json::from_value::<WorkerStatus>(v) {
                if let Ok(mut i) = self.info.lock() {
                    i.model = s.model;
                    i.chunks = s.chunks;
                }
            }
        }
        if self.starts > 1 {
            if let Ok(mut i) = self.info.lock() {
                i.restarts = self.starts - 1;
            }
        }
        Ok(())
    }

    /// One request/response exchange. Any failure other than a clean RPC
    /// error discards the child and schedules a restart. A cancel also
    /// discards the child, but is not a failure: no backoff, no counter.
    async fn exchange(
        &mut self,
        method: &str,
        params: Value,
        opts: CallOpts,
    ) -> Result<Value, CallError> {
        let id = self.next_id;
        self.next_id += 1;
        let line = Request::new(id, method, params).to_line();
        let deadline = Instant::now() + opts.timeout;
        let generation = self.generation;
        let cancel = opts.cancel.clone();
        let progress = opts.progress;
        let outcome = {
            let Some(run) = self.child.as_mut() else {
                return Err(CallError::Unavailable("worker is not running".into()));
            };
            // Drop anything left over from before this call.
            while run.lines.try_recv().is_ok() {}
            let work = async {
                let write = async {
                    run.stdin.write_all(line.as_bytes()).await?;
                    run.stdin.flush().await
                };
                match timeout_at(deadline, write).await {
                    Ok(Ok(())) => read_response(run, id, deadline, progress, generation).await,
                    Ok(Err(_)) => Err(CallError::Crashed),
                    Err(_) => Err(CallError::Timeout),
                }
            };
            let cancelled = async {
                match &cancel {
                    Some(c) => c.cancelled().await,
                    None => std::future::pending().await,
                }
            };
            tokio::select! {
                biased;
                by = cancelled => Err(CallError::Cancelled { by, worker: None }),
                r = work => r,
            }
        };
        match outcome {
            Ok(v) => {
                self.failures = 0;
                self.set_state(WorkerState::Up);
                self.note_ok();
                Ok(v)
            }
            Err(CallError::Rpc(e)) => {
                self.failures = 0;
                self.set_state(WorkerState::Up);
                self.note_ok();
                Err(CallError::Rpc(e))
            }
            Err(CallError::Cancelled { by, .. }) => {
                let how = self.terminate(self.cfg.cancel_grace, true).await;
                self.set_state(WorkerState::Down);
                // Not a failure: leave `failures` alone and allow an
                // immediate restart.
                self.retry_at = Instant::now();
                info!(target: "docketd::worker", task = ?opts.task, generation,
                    how = ?how, "worker terminated for a cancelled call");
                Err(CallError::Cancelled {
                    by,
                    worker: Some(how),
                })
            }
            Err(e) => {
                warn!(target: "docketd::worker", method, task = ?opts.task, generation,
                    "worker call failed; restarting worker");
                self.note_failure(match &e {
                    CallError::Timeout => "worker_timeout",
                    CallError::Crashed => "worker_crashed",
                    _ => "worker_unavailable",
                });
                self.stop_child().await;
                self.set_state(WorkerState::Down);
                self.schedule_backoff();
                Err(e)
            }
        }
    }

    async fn stop_child(&mut self) {
        let _ = self.terminate(Duration::from_secs(2), false).await;
    }

    /// End the current worker and everything in its process group, and report
    /// how. `urgent` (a cancel): the worker is busy and will not read stdin,
    /// so ask with SIGTERM, wait `grace`, then SIGKILL the whole group.
    /// Otherwise: close stdin (EOF asks it to exit), wait `grace`, then kill.
    async fn terminate(&mut self, grace: Duration, urgent: bool) -> WorkerExit {
        let Some(mut r) = self.child.take() else {
            return WorkerExit::AlreadyDead;
        };
        let was_dead = matches!(r.child.try_wait(), Ok(Some(_)));
        if !was_dead {
            if urgent {
                signal_group(r.pid, libc::SIGTERM);
            }
            drop(r.stdin);
        }
        let how = if was_dead {
            WorkerExit::AlreadyDead
        } else if tokio::time::timeout(grace, r.child.wait()).await.is_ok() {
            WorkerExit::Exited
        } else {
            signal_group(r.pid, libc::SIGKILL);
            let _ = tokio::time::timeout(Duration::from_secs(5), r.child.wait()).await;
            WorkerExit::Killed
        };
        // Sweep stragglers (children the worker started) in the same group.
        signal_group(r.pid, libc::SIGKILL);
        how
    }
}

/// Signal every process in the worker's process group (the worker is its
/// leader). ESRCH (group already empty) is expected and ignored.
fn signal_group(pid: Option<u32>, sig: i32) {
    if let Some(pid) = pid.and_then(|p| i32::try_from(p).ok()).filter(|p| *p > 1) {
        // SAFETY: plain kill(2) on a negative pid (a process group we created).
        unsafe {
            libc::kill(-pid, sig);
        }
    }
}

fn e_kind(e: &CallError) -> &'static str {
    match e {
        CallError::Unavailable(_) => "unavailable",
        CallError::Timeout => "timeout",
        CallError::Crashed => "crashed",
        CallError::Rpc(_) => "rpc",
        CallError::Cancelled { .. } => "cancelled",
    }
}

async fn read_response(
    run: &mut Running,
    id: u64,
    deadline: Instant,
    progress: Option<mpsc::UnboundedSender<Progress>>,
    generation: u64,
) -> Result<Value, CallError> {
    let mut noise = 0u32;
    loop {
        let line = match timeout_at(deadline, run.lines.recv()).await {
            Err(_) => return Err(CallError::Timeout),
            Ok(None) => return Err(CallError::Crashed),
            Ok(Some(l)) => l,
        };
        match rpc::parse_line(&line) {
            Ok(Incoming::Response(r)) => {
                if r.id.as_ref().and_then(Value::as_u64) != Some(id) {
                    // A parse error reply has a null id: the worker rejected
                    // a line it could not read, so this call cannot succeed.
                    if r.id.is_none() {
                        if let Some(e) = r.error {
                            return Err(CallError::Rpc(e));
                        }
                    }
                    continue;
                }
                return match (r.result, r.error) {
                    (_, Some(e)) => Err(CallError::Rpc(e)),
                    (Some(v), None) => Ok(v),
                    (None, None) => Ok(json!(null)),
                };
            }
            Ok(Incoming::Notification { method, params }) => {
                if method == "progress" || method == "delta" {
                    if let Some(tx) = &progress {
                        let _ = tx.send(Progress { generation, params });
                    }
                }
            }
            Err(rpc::Noise::Empty) => {}
            Err(_) => {
                // Never log the content: it is not ours to repeat.
                noise += 1;
                debug!(target: "docketd::worker", len = line.len(), noise, "ignored non-protocol line");
            }
        }
    }
}
