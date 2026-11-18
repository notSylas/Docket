//! Task registry and the runner that drives one `ask` task over the worker.
//!
//! Invariants (tested):
//! * Events of a task get a strictly increasing `seq` (1, 2, 3, ...).
//! * A terminal state closes the task's channel under the same lock that
//!   appends its last events. Nothing is appended afterwards: a late line from
//!   an old worker process, or a late `result`, is dropped, never retained.
//! * Phase and delta events are accepted only while the task is `running`;
//!   once a cancel is accepted (`cancelling`) they are dropped.
//! * Only a bounded tail of events is kept, and only a bounded number of
//!   finished tasks.

use crate::worker::{CallError, CallOpts, CancelToken, Progress, WorkerHandle};
use docket_proto::api::{AskRequest, AskResult, Phase};
use docket_proto::rpc;
use docket_proto::task::{
    CancelSource, FailureReason, InferenceStatus, TaskEvent, TaskEventBody, TaskId, TaskInfo,
    TaskKind, TaskState,
};
use serde_json::Value;
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::{mpsc, watch, OwnedSemaphorePermit, Semaphore};
use tracing::{debug, info};

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

struct Entry {
    kind: TaskKind,
    state: TaskState,
    created_ms: u64,
    deadline: Duration,
    last_seq: u64,
    tail: VecDeque<TaskEvent>,
    closed: bool,
    /// Carries `last_seq`; wakes subscribers.
    bell: watch::Sender<u64>,
    cancel: CancelToken,
    /// Set once, inside `finish`, in the same critical section that makes the
    /// task terminal. Never mutated afterwards; dropped with the entry.
    grants: GrantState,
}

/// Longest id (source, version, chunk) accepted into a grant, in chars.
const MAX_ID_CHARS: usize = 200;
/// Longest answer-time excerpt kept in a grant, in chars.
const MAX_EXCERPT_CHARS: usize = 1000;
/// Most grants one task can hold; further citations are not granted.
const MAX_GRANTS_PER_TASK: usize = 64;

/// Permission to read one cited chunk back, minted from a citation of a
/// succeeded, non-abstained answer. Ids are the worker's, bound to the
/// citation's `chunk_id`; nothing the client sends ever enters a grant.
#[derive(Clone, PartialEq, Eq)]
pub struct Grant {
    pub chunk_id: String,
    pub source_id: String,
    pub evidence_version_id: String,
    pub resolved_chunk_id: Option<String>,
    /// Answer-time citation excerpt (at most 1000 chars).
    pub excerpt: String,
}

// Ids and text are document-derived; never print them.
impl std::fmt::Debug for Grant {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Grant")
            .field("resolved", &self.resolved_chunk_id.is_some())
            .finish_non_exhaustive()
    }
}

/// A task's evidence-grant state.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum GrantState {
    /// Not eligible: not succeeded, abstained, cancelled/failed/timed out, or
    /// no citations.
    None,
    /// Succeeded with citations, but none carried valid metadata (an older
    /// worker).
    Unsupported,
    /// Valid, unique citations by `chunk_id`.
    Granted(HashMap<String, Grant>),
}

/// Result of [`Registry::grant_lookup`].
///
/// `NotCited` deliberately covers an unknown or evicted task, a task that is
/// not `Succeeded`, an ineligible task, and a chunk that was not granted, so
/// a caller cannot probe task ids or chunk ids. `Unsupported` is a task-level
/// fact ("the worker that answered was too old to attach metadata"), not
/// chunk information, and is returned only for an existing, succeeded task
/// whose grant state is `Unsupported`. Task ids are daemon-local (boot tag +
/// counter), so knowing one already implies a local client of this daemon.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GrantLookup {
    Granted(Grant),
    Unsupported,
    NotCited,
}

fn valid_id(s: &str) -> bool {
    let n = s.chars().count();
    (1..=MAX_ID_CHARS).contains(&n) && !s.contains('\0')
}

fn truncate_chars(s: &str, max: usize) -> String {
    match s.char_indices().nth(max) {
        Some((i, _)) => s[..i].to_string(),
        None => s.to_string(),
    }
}

/// Grants for a terminal `Succeeded` task, from its published events.
fn grants_from(events: &[TaskEventBody]) -> GrantState {
    let Some(r) = events.iter().find_map(|b| match b {
        TaskEventBody::Result { result } => Some(result),
        _ => None,
    }) else {
        return GrantState::None;
    };
    if r.abstained || r.citations.is_empty() {
        return GrantState::None;
    }
    let mut seen: HashMap<&str, usize> = HashMap::new();
    for c in &r.citations {
        *seen.entry(c.chunk_id.as_str()).or_insert(0) += 1;
    }
    let mut any_valid = false;
    let mut map = HashMap::new();
    for c in &r.citations {
        let (Some(src), Some(ver)) = (c.source_id.as_deref(), c.evidence_version_id.as_deref())
        else {
            continue;
        };
        if !(valid_id(&c.chunk_id) && valid_id(src) && valid_id(ver)) {
            continue;
        }
        if c.resolved_chunk_id.as_deref().is_some_and(|x| !valid_id(x)) {
            continue;
        }
        any_valid = true;
        if seen.get(c.chunk_id.as_str()) != Some(&1) || map.len() >= MAX_GRANTS_PER_TASK {
            continue;
        }
        map.insert(
            c.chunk_id.clone(),
            Grant {
                chunk_id: c.chunk_id.clone(),
                source_id: src.to_string(),
                evidence_version_id: ver.to_string(),
                resolved_chunk_id: c.resolved_chunk_id.clone(),
                excerpt: truncate_chars(&c.excerpt, MAX_EXCERPT_CHARS),
            },
        );
    }
    if any_valid {
        GrantState::Granted(map)
    } else {
        GrantState::Unsupported
    }
}

#[derive(Default)]
struct Inner {
    tasks: HashMap<String, Entry>,
    /// Creation order, oldest first.
    order: VecDeque<String>,
}

pub struct Registry {
    inner: Mutex<Inner>,
    tail_cap: usize,
    retained: usize,
    max_queued: usize,
    boot: u64,
    counter: AtomicU64,
}

#[derive(Debug, PartialEq, Eq)]
pub enum CancelOutcome {
    NotFound,
    /// The request was accepted; the task is now `cancelling`.
    Accepted,
    /// Already cancelling or finished; nothing was changed.
    Already(TaskState),
}

#[derive(Debug, PartialEq, Eq)]
pub struct Busy;

/// What a subscriber gets next.
#[derive(Debug, PartialEq)]
pub enum Pull {
    Event(Box<TaskEvent>),
    /// The consumer fell behind the retained tail; events were lost.
    Gap,
    /// The task is finished and everything was delivered (or it was evicted).
    End,
}

pub struct Subscription {
    reg: Arc<Registry>,
    id: String,
    last: u64,
    rx: watch::Receiver<u64>,
}

impl Subscription {
    pub async fn next(&mut self) -> Pull {
        loop {
            // Mark the bell as seen before looking, so a publish after the
            // look wakes `changed()` instead of being missed.
            let _ = self.rx.borrow_and_update();
            {
                let g = self.reg.lock();
                let Some(e) = g.tasks.get(&self.id) else {
                    return Pull::End;
                };
                let want = self.last + 1;
                if let Some(first) = e.tail.front().map(|t| t.seq) {
                    if want < first {
                        return Pull::Gap;
                    }
                    if let Some(ev) = e.tail.get((want - first) as usize) {
                        self.last = ev.seq;
                        return Pull::Event(Box::new(ev.clone()));
                    }
                }
                if e.closed {
                    return Pull::End;
                }
            }
            if self.rx.changed().await.is_err() {
                return Pull::End;
            }
        }
    }
}

impl Subscription {
    /// After a `Gap`: skip to the oldest retained event (the client sees the
    /// jump in `seq`).
    pub fn resync(&mut self) {
        let g = self.reg.lock();
        if let Some(first) = g.tasks.get(&self.id).and_then(|e| e.tail.front()) {
            self.last = first.seq - 1;
        }
    }
}

impl Registry {
    pub fn new(tail_cap: usize, retained: usize, max_queued: usize) -> Arc<Self> {
        // Nanosecond resolution: a daemon restarted within the same second must not
        // mint the ids of its predecessor, or a task id held across the restart could
        // name (and so read the grants of) an unrelated new task.
        let boot = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        Arc::new(Self {
            inner: Mutex::new(Inner::default()),
            tail_cap: tail_cap.max(16),
            retained,
            max_queued,
            boot,
            counter: AtomicU64::new(1),
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn push(&self, e: &mut Entry, id: &str, body: TaskEventBody) {
        e.last_seq += 1;
        e.tail.push_back(TaskEvent {
            task_id: TaskId(id.to_string()),
            seq: e.last_seq,
            at_ms: now_ms(),
            body,
        });
        while e.tail.len() > self.tail_cap {
            e.tail.pop_front();
        }
        e.bell.send_replace(e.last_seq);
    }

    /// Register a new `queued` task. `will_wait` says it has no worker slot yet;
    /// that is refused when the wait queue is full.
    pub fn create(
        &self,
        deadline: Duration,
        will_wait: bool,
    ) -> Result<(TaskId, CancelToken), Busy> {
        let mut g = self.lock();
        if will_wait {
            let queued = g
                .tasks
                .values()
                .filter(|e| e.state == TaskState::Queued)
                .count();
            if queued >= self.max_queued {
                return Err(Busy);
            }
        }
        let id = format!(
            "t-{:08x}-{}",
            self.boot,
            self.counter.fetch_add(1, Ordering::Relaxed)
        );
        let cancel = CancelToken::new();
        let mut e = Entry {
            kind: TaskKind::Ask,
            state: TaskState::Queued,
            created_ms: now_ms(),
            deadline,
            last_seq: 0,
            tail: VecDeque::new(),
            closed: false,
            bell: watch::channel(0).0,
            cancel: cancel.clone(),
            grants: GrantState::None,
        };
        self.push(
            &mut e,
            &id,
            TaskEventBody::State {
                state: TaskState::Queued,
            },
        );
        g.tasks.insert(id.clone(), e);
        g.order.push_back(id.clone());
        self.evict(&mut g);
        Ok((TaskId(id), cancel))
    }

    /// Drop the oldest finished tasks beyond the retention limit.
    fn evict(&self, g: &mut Inner) {
        let finished = g.tasks.values().filter(|e| e.state.is_terminal()).count();
        let mut excess = finished.saturating_sub(self.retained);
        if excess == 0 {
            return;
        }
        let mut keep = VecDeque::with_capacity(g.order.len());
        for id in std::mem::take(&mut g.order) {
            let done = g.tasks.get(&id).is_some_and(|e| e.state.is_terminal());
            if done && excess > 0 {
                g.tasks.remove(&id);
                excess -= 1;
            } else {
                keep.push_back(id);
            }
        }
        g.order = keep;
    }

    /// `queued` to `running`. False if the task is not queued any more.
    pub fn start(&self, id: &str) -> bool {
        let mut g = self.lock();
        let Some(e) = g.tasks.get_mut(id) else {
            return false;
        };
        if e.state != TaskState::Queued {
            return false;
        }
        e.state = TaskState::Running;
        self.push(
            e,
            id,
            TaskEventBody::State {
                state: TaskState::Running,
            },
        );
        true
    }

    /// Append a progress event. Accepted only while `running`: after a cancel
    /// is accepted, or once the task finished, it is dropped (returns false).
    pub fn publish(&self, id: &str, body: TaskEventBody) -> bool {
        let mut g = self.lock();
        let Some(e) = g.tasks.get_mut(id) else {
            return false;
        };
        if e.closed || e.state != TaskState::Running {
            return false;
        }
        self.push(e, id, body);
        true
    }

    /// Append `events`, then the terminal `state`, and close the channel, all
    /// under one lock. False (and nothing appended) if already finished.
    pub fn finish(&self, id: &str, state: TaskState, events: Vec<TaskEventBody>) -> bool {
        debug_assert!(state.is_terminal());
        let mut g = self.lock();
        let Some(e) = g.tasks.get_mut(id) else {
            return false;
        };
        if e.closed {
            return false;
        }
        // Grants are minted here, under the same lock hold that makes the
        // task terminal and publishes its Result, and only for `Succeeded`.
        // (A cancel that won the race has already turned the outcome into
        // `Cancelled` without a Result, so it gets none.)
        e.grants = if state == TaskState::Succeeded {
            grants_from(&events)
        } else {
            GrantState::None
        };
        for b in events {
            self.push(e, id, b);
        }
        e.state = state;
        self.push(e, id, TaskEventBody::State { state });
        e.closed = true;
        info!(target: "docketd::tasks", task = id, state = ?state,
            events = e.last_seq, "task finished");
        self.evict(&mut g);
        true
    }

    pub fn request_cancel(&self, id: &str, by: CancelSource) -> CancelOutcome {
        let mut g = self.lock();
        let Some(e) = g.tasks.get_mut(id) else {
            return CancelOutcome::NotFound;
        };
        match e.state {
            TaskState::Queued | TaskState::Running => {
                e.state = TaskState::Cancelling;
                self.push(e, id, TaskEventBody::CancelRequested { by });
                self.push(
                    e,
                    id,
                    TaskEventBody::State {
                        state: TaskState::Cancelling,
                    },
                );
                e.cancel.cancel(by);
                info!(target: "docketd::tasks", task = id, by = ?by, "cancel requested");
                CancelOutcome::Accepted
            }
            s => CancelOutcome::Already(s),
        }
    }

    pub fn cancel_all(&self, by: CancelSource) {
        let ids: Vec<String> = {
            let g = self.lock();
            g.tasks
                .iter()
                .filter(|(_, e)| !e.state.is_terminal())
                .map(|(k, _)| k.clone())
                .collect()
        };
        for id in ids {
            self.request_cancel(&id, by);
        }
    }

    /// Wait until no task is unfinished, or `limit` passes. True if all done.
    pub async fn wait_all_finished(&self, limit: Duration) -> bool {
        let t0 = Instant::now();
        loop {
            if self.lock().tasks.values().all(|e| e.state.is_terminal()) {
                return true;
            }
            if t0.elapsed() >= limit {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    pub fn state(&self, id: &str) -> Option<TaskState> {
        self.lock().tasks.get(id).map(|e| e.state)
    }

    /// Look up the grant for `chunk_id` of task `task_id`. One lock hold; the
    /// answer is derived from the entry's state and grants together.
    pub fn grant_lookup(&self, task_id: &str, chunk_id: &str) -> GrantLookup {
        let g = self.lock();
        let Some(e) = g.tasks.get(task_id) else {
            return GrantLookup::NotCited;
        };
        if e.state != TaskState::Succeeded {
            return GrantLookup::NotCited;
        }
        match &e.grants {
            GrantState::None => GrantLookup::NotCited,
            GrantState::Unsupported => GrantLookup::Unsupported,
            GrantState::Granted(m) => m
                .get(chunk_id)
                .cloned()
                .map_or(GrantLookup::NotCited, GrantLookup::Granted),
        }
    }

    pub fn info(&self, id: &str) -> Option<TaskInfo> {
        let g = self.lock();
        g.tasks.get(id).map(|e| info_of(id, e))
    }

    /// Newest first.
    pub fn list(&self) -> Vec<TaskInfo> {
        let g = self.lock();
        g.order
            .iter()
            .rev()
            .filter_map(|id| g.tasks.get(id).map(|e| info_of(id, e)))
            .collect()
    }

    /// Replay everything after `after_seq` (0: from the start), then follow.
    pub fn subscribe(self: &Arc<Self>, id: &str, after_seq: u64) -> Option<Subscription> {
        let g = self.lock();
        let e = g.tasks.get(id)?;
        Some(Subscription {
            reg: self.clone(),
            id: id.to_string(),
            last: after_seq,
            rx: e.bell.subscribe(),
        })
    }
}

fn info_of(id: &str, e: &Entry) -> TaskInfo {
    TaskInfo {
        task_id: TaskId(id.to_string()),
        kind: e.kind,
        state: e.state,
        created_ms: e.created_ms,
        deadline_ms: e.deadline.as_millis() as u64,
        last_seq: e.last_seq,
    }
}

/// Everything needed to start and run tasks.
#[derive(Clone)]
pub struct Tasks {
    pub registry: Arc<Registry>,
    worker: WorkerHandle,
    /// One running question at a time.
    pub slot: Arc<Semaphore>,
    max_deadline: Duration,
}

pub enum SubmitError {
    Busy,
}

impl Tasks {
    pub fn new(
        registry: Arc<Registry>,
        worker: WorkerHandle,
        slot: Arc<Semaphore>,
        max_deadline: Duration,
    ) -> Self {
        Self {
            registry,
            worker,
            slot,
            max_deadline,
        }
    }

    /// Create an `ask` task and start driving it. With `allow_queue` a task
    /// may wait (bounded) behind the running one; without it a busy daemon
    /// refuses, exactly as `/v1/ask` always did.
    pub fn submit(
        &self,
        req: AskRequest,
        deadline_ms: Option<u64>,
        allow_queue: bool,
    ) -> Result<(TaskId, CancelToken), SubmitError> {
        let permit = self.slot.clone().try_acquire_owned().ok();
        if permit.is_none() && !allow_queue {
            return Err(SubmitError::Busy);
        }
        let deadline = deadline_ms
            .map(Duration::from_millis)
            .map_or(self.max_deadline, |d| d.min(self.max_deadline))
            .max(Duration::from_millis(1));
        let (id, cancel) = self
            .registry
            .create(deadline, permit.is_none())
            .map_err(|Busy| SubmitError::Busy)?;
        tokio::spawn(
            self.clone()
                .run_ask(id.0.clone(), cancel.clone(), req, deadline, permit),
        );
        Ok((id, cancel))
    }

    async fn run_ask(
        self,
        id: String,
        cancel: CancelToken,
        req: AskRequest,
        deadline: Duration,
        permit: Option<OwnedSemaphorePermit>,
    ) {
        let reg = &self.registry;
        let permit = match permit {
            Some(p) => Some(p),
            None => tokio::select! {
                p = self.slot.clone().acquire_owned() => p.ok(),
                _ = cancel.cancelled() => None,
            },
        };
        // The permit is held until the worker call is over, including the
        // termination of a cancelled worker, and released on every exit.
        let Some(_permit) = permit else {
            reg.finish(&id, TaskState::Cancelled, vec![]);
            return;
        };
        if !reg.start(&id) {
            // Cancelled while waiting for the slot: the worker was never used.
            reg.finish(&id, TaskState::Cancelled, vec![]);
            return;
        }
        reg.publish(
            &id,
            TaskEventBody::Phase {
                phase: Phase::Retrieving,
            },
        );

        let (ptx, mut prx) = mpsc::unbounded_channel::<Progress>();
        let fwd = {
            let (reg, id) = (self.registry.clone(), id.clone());
            let want_deltas = req.stream;
            tokio::spawn(async move {
                let mut last = Phase::Retrieving;
                let mut pinned: Option<u64> = None;
                while let Some(p) = prx.recv().await {
                    // Every line is tagged with the worker generation that
                    // sent it; stick to the first one this task saw.
                    match pinned {
                        None => pinned = Some(p.generation),
                        Some(g) if g != p.generation => {
                            debug!(target: "docketd::tasks", task = %id, "dropped line from another worker generation");
                            continue;
                        }
                        Some(_) => {}
                    }
                    if want_deltas {
                        if let Some(text) = p.params.get("text").and_then(Value::as_str) {
                            reg.publish(&id, TaskEventBody::Delta { text: text.into() });
                            continue;
                        }
                    }
                    if let Some(ph) = p
                        .params
                        .get("phase")
                        .and_then(Value::as_str)
                        .and_then(Phase::parse)
                    {
                        if ph != last && ph != Phase::Done {
                            last = ph;
                            reg.publish(&id, TaskEventBody::Phase { phase: ph });
                        }
                    }
                }
            })
        };

        let params = serde_json::to_value(&req).unwrap_or(Value::Null);
        let out = self
            .worker
            .call(
                "query.ask",
                params,
                CallOpts {
                    timeout: deadline,
                    progress: Some(ptx),
                    cancel: Some(cancel.clone()),
                    task: Some(id.clone()),
                    ..CallOpts::default()
                },
            )
            .await;
        let _ = fwd.await;
        let (state, events) = outcome(out, &cancel);
        reg.finish(&id, state, events);
    }
}

fn failed(reason: FailureReason, message: String) -> (TaskState, Vec<TaskEventBody>) {
    (
        TaskState::Failed,
        vec![TaskEventBody::Error {
            code: reason.code().into(),
            message,
        }],
    )
}

fn cancelled(worker: Option<docket_proto::task::WorkerExit>) -> (TaskState, Vec<TaskEventBody>) {
    let mut ev = Vec::new();
    if let Some(how) = worker {
        ev.push(TaskEventBody::WorkerTerminated { how });
    }
    // The daemon cannot observe the model engine, so this is never anything
    // but `unverified`.
    ev.push(TaskEventBody::InferenceStopped {
        status: InferenceStatus::Unverified,
    });
    (TaskState::Cancelled, ev)
}

/// Turn the end of a worker call into the task's final state and events.
fn outcome(out: Result<Value, CallError>, cancel: &CancelToken) -> (TaskState, Vec<TaskEventBody>) {
    match out {
        Ok(_) if cancel.is_cancelled() => {
            // The user's cancel won the race: the answer is discarded and
            // never published. The worker finished on its own.
            cancelled(None)
        }
        Ok(v) => match serde_json::from_value::<AskResult>(v) {
            Ok(r) => (
                TaskState::Succeeded,
                vec![
                    TaskEventBody::Phase { phase: Phase::Done },
                    TaskEventBody::Result {
                        result: Box::new(r),
                    },
                ],
            ),
            Err(_) => failed(
                FailureReason::WorkerError,
                "unexpected worker response".into(),
            ),
        },
        Err(CallError::Cancelled { worker, .. }) => cancelled(worker),
        Err(CallError::Crashed) if cancel.is_cancelled() => {
            cancelled(Some(docket_proto::task::WorkerExit::AlreadyDead))
        }
        Err(CallError::Timeout) => (
            TaskState::TimedOut,
            vec![TaskEventBody::Error {
                code: "timeout".into(),
                message: "the worker did not answer in time".into(),
            }],
        ),
        Err(CallError::Unavailable(m)) => failed(FailureReason::WorkerUnavailable, m),
        Err(CallError::Crashed) => failed(
            FailureReason::WorkerCrashed,
            "the worker stopped unexpectedly; it is being restarted".into(),
        ),
        Err(CallError::Rpc(err)) => match err.kind() {
            Some("NoContentIndexed") => failed(
                FailureReason::NoContent,
                "Nothing is indexed yet. Add a folder with the docket CLI first.".into(),
            ),
            _ if err.code == rpc::INVALID_PARAMS => failed(FailureReason::BadRequest, err.message),
            _ => failed(FailureReason::WorkerError, err.message),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn delta(t: &str) -> TaskEventBody {
        TaskEventBody::Delta { text: t.into() }
    }

    async fn drain(sub: &mut Subscription) -> Vec<TaskEvent> {
        let mut v = Vec::new();
        loop {
            match sub.next().await {
                Pull::Event(e) => v.push(*e),
                Pull::End => return v,
                Pull::Gap => panic!("gap"),
            }
        }
    }

    #[tokio::test]
    async fn seq_is_monotonic_and_terminal_closes_the_channel() {
        let r = Registry::new(64, 8, 1);
        let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
        assert!(r.start(&id.0));
        assert!(r.publish(&id.0, delta("a")));
        assert!(r.finish(&id.0, TaskState::Succeeded, vec![delta("b")]));
        // Everything after the terminal state is refused: late events never land.
        assert!(!r.publish(&id.0, delta("late")));
        assert!(!r.finish(&id.0, TaskState::Failed, vec![delta("late2")]));
        let evs = drain(&mut r.subscribe(&id.0, 0).unwrap()).await;
        let seqs: Vec<u64> = evs.iter().map(|e| e.seq).collect();
        assert_eq!(seqs, [1, 2, 3, 4, 5]);
        assert!(!evs
            .iter()
            .any(|e| matches!(&e.body, TaskEventBody::Delta { text } if text.starts_with("late"))));
        assert_eq!(r.state(&id.0), Some(TaskState::Succeeded));
    }

    #[tokio::test]
    async fn deltas_are_dropped_once_cancel_is_accepted() {
        let r = Registry::new(64, 8, 1);
        let (id, tok) = r.create(Duration::from_secs(1), false).unwrap();
        r.start(&id.0);
        assert!(r.publish(&id.0, delta("before")));
        assert_eq!(
            r.request_cancel(&id.0, CancelSource::User),
            CancelOutcome::Accepted
        );
        assert_eq!(tok.source(), Some(CancelSource::User));
        assert!(!r.publish(&id.0, delta("after cancel")));
        assert_eq!(
            r.request_cancel(&id.0, CancelSource::User),
            CancelOutcome::Already(TaskState::Cancelling)
        );
        r.finish(&id.0, TaskState::Cancelled, vec![]);
        assert_eq!(
            r.request_cancel(&id.0, CancelSource::User),
            CancelOutcome::Already(TaskState::Cancelled)
        );
        assert_eq!(
            r.request_cancel("nope", CancelSource::User),
            CancelOutcome::NotFound
        );
    }

    #[tokio::test]
    async fn queued_task_can_be_cancelled_and_start_is_refused() {
        let r = Registry::new(64, 8, 1);
        let (id, _) = r.create(Duration::from_secs(1), true).unwrap();
        assert_eq!(r.state(&id.0), Some(TaskState::Queued));
        assert_eq!(
            r.request_cancel(&id.0, CancelSource::User),
            CancelOutcome::Accepted
        );
        assert!(!r.start(&id.0), "a cancelled queued task never runs");
        r.finish(&id.0, TaskState::Cancelled, vec![]);
        let evs = drain(&mut r.subscribe(&id.0, 0).unwrap()).await;
        assert!(!evs.iter().any(|e| matches!(
            e.body,
            TaskEventBody::WorkerTerminated { .. } | TaskEventBody::Phase { .. }
        )));
    }

    #[tokio::test]
    async fn wait_queue_is_bounded() {
        let r = Registry::new(64, 8, 1);
        assert!(r.create(Duration::from_secs(1), true).is_ok());
        assert_eq!(r.create(Duration::from_secs(1), true).err(), Some(Busy));
        assert!(r.create(Duration::from_secs(1), false).is_ok());
    }

    #[tokio::test]
    async fn replay_from_last_event_id_has_no_gaps_or_duplicates() {
        let r = Registry::new(64, 8, 1);
        let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
        r.start(&id.0);
        for i in 0..5 {
            r.publish(&id.0, delta(&i.to_string()));
        }
        r.finish(&id.0, TaskState::Succeeded, vec![]);
        let evs = drain(&mut r.subscribe(&id.0, 4).unwrap()).await;
        let seqs: Vec<u64> = evs.iter().map(|e| e.seq).collect();
        assert_eq!(seqs, [5, 6, 7, 8]);
    }

    #[tokio::test]
    async fn tail_is_bounded_and_a_lagging_reader_sees_a_gap() {
        let r = Registry::new(16, 8, 1);
        let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
        r.start(&id.0);
        for i in 0..100 {
            r.publish(&id.0, delta(&i.to_string()));
        }
        let mut s = r.subscribe(&id.0, 0).unwrap();
        assert_eq!(s.next().await, Pull::Gap);
        // A reader that resumes inside the tail is fine.
        let mut s = r.subscribe(&id.0, 90).unwrap();
        assert!(matches!(s.next().await, Pull::Event(e) if e.seq == 91));
    }

    #[tokio::test]
    async fn finished_tasks_are_evicted_beyond_the_limit() {
        let r = Registry::new(64, 2, 1);
        let mut ids = vec![];
        for _ in 0..4 {
            let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
            r.start(&id.0);
            r.finish(&id.0, TaskState::Succeeded, vec![]);
            ids.push(id);
        }
        assert!(r.info(&ids[0].0).is_none() && r.info(&ids[1].0).is_none());
        assert_eq!(r.list().len(), 2);
        assert_eq!(r.list()[0].task_id, ids[3], "newest first");
    }

    #[tokio::test]
    async fn live_subscriber_is_woken_by_publish() {
        let r = Registry::new(64, 8, 1);
        let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
        let mut s = r.subscribe(&id.0, 0).unwrap();
        assert!(matches!(s.next().await, Pull::Event(e) if e.seq == 1));
        let r2 = r.clone();
        let id2 = id.0.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(50)).await;
            r2.start(&id2);
            r2.finish(&id2, TaskState::Succeeded, vec![]);
        });
        let rest = drain(&mut s).await;
        assert_eq!(rest.len(), 2);
    }

    // ---- evidence grants ----

    use docket_proto::api::Citation;

    fn cit(chunk: &str, src: Option<&str>, ver: Option<&str>) -> Citation {
        Citation {
            label: "[a#1]".into(),
            source_display_name: "a.txt".into(),
            chunk_id: chunk.into(),
            excerpt: format!("excerpt {chunk}"),
            source_id: src.map(Into::into),
            evidence_version_id: ver.map(Into::into),
            resolved_chunk_id: None,
        }
    }

    fn good(chunk: &str) -> Citation {
        cit(chunk, Some("s1"), Some("v1"))
    }

    fn result_ev(abstained: bool, cites: Vec<Citation>) -> TaskEventBody {
        TaskEventBody::Result {
            result: Box::new(AskResult {
                answer: "a".into(),
                abstained,
                citations: cites,
                ..Default::default()
            }),
        }
    }

    fn run(state: TaskState, ev: Vec<TaskEventBody>) -> (Arc<Registry>, String) {
        let r = Registry::new(64, 8, 1);
        let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
        r.start(&id.0);
        r.finish(&id.0, state, ev);
        (r, id.0)
    }

    fn granted(r: &Registry, id: &str, c: &str) -> bool {
        matches!(r.grant_lookup(id, c), GrantLookup::Granted(_))
    }

    #[test]
    fn grants_only_for_succeeded_non_abstained_with_citations() {
        let (r, id) = run(
            TaskState::Succeeded,
            vec![result_ev(false, vec![good("c1")])],
        );
        match r.grant_lookup(&id, "c1") {
            GrantLookup::Granted(g) => {
                assert_eq!(
                    (g.source_id.as_str(), g.evidence_version_id.as_str()),
                    ("s1", "v1")
                );
                assert_eq!(g.excerpt, "excerpt c1");
                assert!(g.resolved_chunk_id.is_none());
            }
            o => panic!("{o:?}"),
        }
        let none_cases = [
            (
                TaskState::Succeeded,
                vec![result_ev(true, vec![good("c1")])],
            ),
            (TaskState::Succeeded, vec![result_ev(false, vec![])]),
            (TaskState::Succeeded, vec![]),
            (
                TaskState::Cancelled,
                vec![result_ev(false, vec![good("c1")])],
            ),
            (TaskState::Failed, vec![result_ev(false, vec![good("c1")])]),
            (
                TaskState::TimedOut,
                vec![result_ev(false, vec![good("c1")])],
            ),
        ];
        for (st, ev) in none_cases {
            let (r, id) = run(st, ev);
            assert_eq!(r.grant_lookup(&id, "c1"), GrantLookup::NotCited, "{st:?}");
        }
    }

    #[test]
    fn cancel_winning_the_race_yields_no_grants() {
        let tok = CancelToken::new();
        tok.cancel(CancelSource::User);
        let v = serde_json::to_value(AskResult {
            answer: "a".into(),
            citations: vec![good("c1")],
            ..Default::default()
        })
        .unwrap();
        let (st, ev) = outcome(Ok(v), &tok);
        assert_eq!(st, TaskState::Cancelled);
        let (r, id) = run(st, ev);
        assert_eq!(r.grant_lookup(&id, "c1"), GrantLookup::NotCited);
    }

    #[test]
    fn grant_state_follows_validation_rules() {
        let nul = cit("cn", Some("s\0"), Some("v1"));
        let long = cit("cl", Some(&"s".repeat(201)), Some("v1"));
        let empty_ver = cit("ce", Some("s1"), Some(""));
        let only_src = cit("co", Some("s1"), None);
        let bad_resolved = Citation {
            resolved_chunk_id: Some("x\0".into()),
            ..good("cr")
        };
        let ok_long = cit(&"k".repeat(200), Some(&"s".repeat(200)), Some("v1"));
        let (r, id) = run(
            TaskState::Succeeded,
            vec![result_ev(
                false,
                vec![
                    good("c1"),
                    good("dup"),
                    good("dup"),
                    nul,
                    long,
                    empty_ver,
                    only_src,
                    bad_resolved,
                    ok_long,
                ],
            )],
        );
        assert!(granted(&r, &id, "c1"));
        assert!(granted(&r, &id, &"k".repeat(200)));
        for c in ["dup", "cn", "cl", "ce", "co", "cr", "other"] {
            assert_eq!(r.grant_lookup(&id, c), GrantLookup::NotCited, "{c}");
        }
        // A duplicate also cancels a would-be-valid twin of an invalid one.
        let (r, id) = run(
            TaskState::Succeeded,
            vec![result_ev(
                false,
                vec![good("d"), cit("d", None, None), good("e")],
            )],
        );
        assert!(!granted(&r, &id, "d") && granted(&r, &id, "e"));
    }

    #[test]
    fn task_ids_of_registries_created_back_to_back_never_collide() {
        // A restarted daemon (new Registry) must not reuse its predecessor's task ids.
        let ids: std::collections::HashSet<String> = (0..200)
            .map(|_| {
                let r = Registry::new(64, 8, 1);
                r.create(Duration::from_secs(1), false).unwrap().0 .0
            })
            .collect();
        assert_eq!(ids.len(), 200);
    }

    #[test]
    fn metadata_less_result_is_unsupported_only_for_succeeded_task() {
        let (r, id) = run(
            TaskState::Succeeded,
            vec![result_ev(false, vec![cit("c1", None, None)])],
        );
        assert_eq!(r.grant_lookup(&id, "c1"), GrantLookup::Unsupported);
        assert_eq!(r.grant_lookup(&id, "zzz"), GrantLookup::Unsupported);
        assert_eq!(r.grant_lookup("nope", "c1"), GrantLookup::NotCited);
        // Not terminal yet: not Unsupported.
        let r2 = Registry::new(64, 8, 1);
        let (id2, _) = r2.create(Duration::from_secs(1), false).unwrap();
        assert_eq!(r2.grant_lookup(&id2.0, "c1"), GrantLookup::NotCited);
        // All-invalid metadata is also Unsupported.
        let (r, id) = run(
            TaskState::Succeeded,
            vec![result_ev(false, vec![cit("c1", Some("s\0"), Some("v"))])],
        );
        assert_eq!(r.grant_lookup(&id, "c1"), GrantLookup::Unsupported);
    }

    #[test]
    fn alias_resolved_chunk_is_retained_and_excerpt_truncated_on_char_boundary() {
        let c = Citation {
            resolved_chunk_id: Some("new".into()),
            excerpt: "é".repeat(1500),
            ..good("old")
        };
        let (r, id) = run(TaskState::Succeeded, vec![result_ev(false, vec![c])]);
        let GrantLookup::Granted(g) = r.grant_lookup(&id, "old") else {
            panic!()
        };
        assert_eq!(g.resolved_chunk_id.as_deref(), Some("new"));
        assert_eq!(g.excerpt.chars().count(), 1000);
        assert!(!format!("{g:?}").contains('é') && !format!("{g:?}").contains("old"));
    }

    #[test]
    fn same_version_citations_do_not_grant_uncited_chunks() {
        let (r, id) = run(
            TaskState::Succeeded,
            vec![result_ev(false, vec![good("c1"), good("c3")])],
        );
        assert!(granted(&r, &id, "c1") && granted(&r, &id, "c3"));
        assert_eq!(r.grant_lookup(&id, "c2"), GrantLookup::NotCited);
    }

    #[test]
    fn grants_per_task_are_bounded() {
        let cites: Vec<Citation> = (0..70).map(|i| good(&format!("c{i}"))).collect();
        let (r, id) = run(TaskState::Succeeded, vec![result_ev(false, cites)]);
        assert!(granted(&r, &id, "c0") && granted(&r, &id, "c63"));
        assert!(!granted(&r, &id, "c64") && !granted(&r, &id, "c69"));
    }

    #[test]
    fn grants_are_immutable_after_finish() {
        let (r, id) = run(
            TaskState::Succeeded,
            vec![result_ev(false, vec![good("c1")])],
        );
        let before = r.grant_lookup(&id, "c1");
        assert!(!r.finish(
            &id,
            TaskState::Succeeded,
            vec![result_ev(false, vec![good("c9")])]
        ));
        assert!(!r.finish(&id, TaskState::Failed, vec![]));
        assert_eq!(r.grant_lookup(&id, "c1"), before);
        assert_eq!(r.grant_lookup(&id, "c9"), GrantLookup::NotCited);
        assert_eq!(r.state(&id), Some(TaskState::Succeeded));
    }

    #[test]
    fn grants_expire_with_eviction() {
        let r = Registry::new(64, 1, 1);
        let mut ids = vec![];
        for _ in 0..3 {
            let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
            r.start(&id.0);
            r.finish(
                &id.0,
                TaskState::Succeeded,
                vec![result_ev(false, vec![good("c1")])],
            );
            ids.push(id.0);
        }
        assert_eq!(r.grant_lookup(&ids[0], "c1"), GrantLookup::NotCited);
        assert!(r.info(&ids[0]).is_none());
        assert!(granted(&r, &ids[2], "c1"));
    }

    #[test]
    fn state_and_grants_become_visible_together() {
        for _ in 0..50 {
            let r = Registry::new(64, 8, 1);
            let (id, _) = r.create(Duration::from_secs(1), false).unwrap();
            r.start(&id.0);
            let barrier = Arc::new(std::sync::Barrier::new(5));
            let readers: Vec<_> = (0..4)
                .map(|_| {
                    let (r, id, b) = (r.clone(), id.0.clone(), barrier.clone());
                    std::thread::spawn(move || {
                        b.wait();
                        loop {
                            // Reverse order of the writer's effects: a grant
                            // seen implies Succeeded; Succeeded seen implies
                            // a grant (state is only read after the lookup,
                            // and again before it).
                            let st_before = r.state(&id);
                            let l = r.grant_lookup(&id, "c1");
                            let st_after = r.state(&id);
                            if st_before == Some(TaskState::Succeeded) {
                                assert!(
                                    matches!(l, GrantLookup::Granted(_)),
                                    "succeeded without grant"
                                );
                            }
                            if matches!(l, GrantLookup::Granted(_)) {
                                assert_eq!(st_after, Some(TaskState::Succeeded));
                                return;
                            }
                        }
                    })
                })
                .collect();
            barrier.wait();
            r.finish(
                &id.0,
                TaskState::Succeeded,
                vec![result_ev(false, vec![good("c1")])],
            );
            for h in readers {
                h.join().unwrap();
            }
        }
    }
}
