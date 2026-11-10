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
use serde_json::{json, Value};
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::time::{timeout_at, Instant};
use tracing::{debug, info, warn};

#[derive(Debug)]
pub enum CallError {
    /// No worker could be started (or one is in restart backoff).
    Unavailable(String),
    Timeout,
    /// The worker exited or closed its output mid-call.
    Crashed,
    /// The worker answered with a JSON-RPC error.
    Rpc(RpcError),
}

struct Call {
    method: String,
    params: Value,
    timeout: Duration,
    progress: Option<mpsc::UnboundedSender<Value>>,
    reply: oneshot::Sender<Result<Value, CallError>>,
}

#[derive(Clone)]
pub struct WorkerHandle {
    tx: mpsc::Sender<Call>,
    info: Arc<Mutex<WorkerInfo>>,
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
        let sup = Supervisor {
            cfg: cfg.clone(),
            info: info.clone(),
            child: None,
            next_id: 1,
            failures: 0,
            retry_at: Instant::now(),
            starts: 0,
        };
        let task = tokio::spawn(sup.run(rx, shutdown));
        (Self { tx, info }, task)
    }

    pub fn info(&self) -> WorkerInfo {
        self.info.lock().map(|g| g.clone()).unwrap_or(WorkerInfo {
            state: WorkerState::Down,
            restarts: 0,
            model: String::new(),
            chunks: None,
        })
    }

    pub async fn call(
        &self,
        method: &str,
        params: Value,
        timeout: Duration,
        progress: Option<mpsc::UnboundedSender<Value>>,
    ) -> Result<Value, CallError> {
        let (reply, rx) = oneshot::channel();
        let call = Call {
            method: method.into(),
            params,
            timeout,
            progress,
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
    child: Child,
    stdin: ChildStdin,
    lines: mpsc::Receiver<String>,
}

struct Supervisor {
    cfg: Config,
    info: Arc<Mutex<WorkerInfo>>,
    child: Option<Running>,
    next_id: u64,
    failures: u32,
    retry_at: Instant,
    starts: u64,
}

impl Supervisor {
    fn set_state(&self, state: WorkerState) {
        if let Ok(mut i) = self.info.lock() {
            i.state = state;
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
        if let Err(e) = self.exchange("ping", Value::Null, t, None).await {
            warn!(target: "docketd::worker", error = ?e_kind(&e), "idle ping failed");
        }
    }

    async fn handle(&mut self, call: Call) {
        let result = match self.ensure_started().await {
            Ok(()) => {
                self.exchange(&call.method, call.params, call.timeout, call.progress)
                    .await
            }
            Err(e) => Err(e),
        };
        let _ = call.reply.send(result);
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
        self.child = Some(Running {
            child,
            stdin,
            lines: lrx,
        });
        self.starts += 1;
        info!(target: "docketd::worker", starts = self.starts, "worker launched");

        let startup = self.cfg.startup_timeout;
        match self.exchange("ping", Value::Null, startup, None).await {
            Ok(_) => {}
            Err(e) => {
                warn!(target: "docketd::worker", error = ?e_kind(&e), "worker failed its first ping");
                return Err(CallError::Unavailable("worker did not become ready".into()));
            }
        }
        // Best effort: cache model name and index size for /v1/status.
        let t = self.cfg.call_timeout;
        if let Ok(v) = self.exchange("status", Value::Null, t, None).await {
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
    /// error discards the child and schedules a restart.
    async fn exchange(
        &mut self,
        method: &str,
        params: Value,
        timeout: Duration,
        progress: Option<mpsc::UnboundedSender<Value>>,
    ) -> Result<Value, CallError> {
        let id = self.next_id;
        self.next_id += 1;
        let line = Request::new(id, method, params).to_line();
        let deadline = Instant::now() + timeout;
        let outcome = {
            let Some(run) = self.child.as_mut() else {
                return Err(CallError::Unavailable("worker is not running".into()));
            };
            // Drop anything left over from before this call.
            while run.lines.try_recv().is_ok() {}
            let write = async {
                run.stdin.write_all(line.as_bytes()).await?;
                run.stdin.flush().await
            };
            match timeout_at(deadline, write).await {
                Ok(Ok(())) => read_response(run, id, deadline, progress).await,
                Ok(Err(_)) => Err(CallError::Crashed),
                Err(_) => Err(CallError::Timeout),
            }
        };
        match &outcome {
            Ok(_) => {
                self.failures = 0;
                self.set_state(WorkerState::Up);
            }
            Err(CallError::Rpc(_)) => {
                self.failures = 0;
                self.set_state(WorkerState::Up);
            }
            Err(_) => {
                warn!(target: "docketd::worker", method, "worker call failed; restarting worker");
                self.stop_child().await;
                self.set_state(WorkerState::Down);
                self.schedule_backoff();
            }
        }
        outcome
    }

    async fn stop_child(&mut self) {
        if let Some(mut r) = self.child.take() {
            drop(r.stdin); // EOF asks the worker to exit
            if tokio::time::timeout(Duration::from_secs(2), r.child.wait())
                .await
                .is_err()
            {
                let _ = r.child.kill().await;
            }
        }
    }
}

fn e_kind(e: &CallError) -> &'static str {
    match e {
        CallError::Unavailable(_) => "unavailable",
        CallError::Timeout => "timeout",
        CallError::Crashed => "crashed",
        CallError::Rpc(_) => "rpc",
    }
}

async fn read_response(
    run: &mut Running,
    id: u64,
    deadline: Instant,
    progress: Option<mpsc::UnboundedSender<Value>>,
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
                if method == "progress" {
                    if let Some(tx) = &progress {
                        let _ = tx.send(params);
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
