//! The local HTTP API. Served only on the Unix socket.

use crate::config::Config;
use crate::health::{self, HealthCache};
use crate::tasks::{CancelOutcome, Grant, GrantLookup, Pull, Registry, SubmitError, Tasks};
use crate::worker::{CallError, CallOpts, WorkerHandle};
use axum::body::Bytes;
use axum::extract::rejection::BytesRejection;
use axum::extract::{DefaultBodyLimit, Extension, Path, Request, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::middleware::{self, Next};
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use docket_proto::api::{
    ApiError, AskRequest, AskResult, ErrorBody, SourcesResponse, StatusResponse, WorkerState,
};
use docket_proto::evidence::{EvidenceRequest, EvidenceResponse};
use docket_proto::task::{
    CancelRequest, CancelResponse, CancelSource, CreateTaskRequest, CreateTaskResponse, TaskEvent,
    TaskEventBody, TaskId, TaskState, TasksResponse,
};
use serde_json::Value;
use std::convert::Infallible;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::{mpsc, Semaphore};
use tokio_stream::wrappers::ReceiverStream;
use tracing::{info, warn};

const MAX_BODY: usize = 64 * 1024;
const MAX_QUESTION_CHARS: usize = 8_000;
/// Worker feature (and client capability) for `POST /v1/evidence/get`.
const EVIDENCE_FEATURE: &str = "evidence.v1";
/// A valid evidence request is two ids of at most 200 chars plus two digits.
const MAX_EVIDENCE_BODY: usize = 8 * 1024;
const MAX_EVIDENCE_ID_CHARS: usize = 200;
const MAX_EVIDENCE_CONTEXT: u8 = 3;

#[derive(Clone)]
pub struct AppState {
    worker: WorkerHandle,
    /// One question at a time.
    ask_slot: Arc<Semaphore>,
    tasks: Tasks,
    tasks_enabled: bool,
    call_timeout: Duration,
    evidence_timeout: Duration,
    counter: Arc<AtomicU64>,
    boot: u64,
    cfg: Arc<Config>,
    health: HealthCache,
}

impl AppState {
    pub fn new(cfg: &Config, worker: WorkerHandle) -> Self {
        let boot = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs() & 0xffff_ffff)
            .unwrap_or(0);
        let ask_slot = Arc::new(Semaphore::new(1));
        let registry = Registry::new(cfg.task_tail, cfg.retained_tasks, cfg.max_queued);
        Self {
            tasks: Tasks::new(registry, worker.clone(), ask_slot.clone(), cfg.ask_timeout),
            worker,
            ask_slot,
            tasks_enabled: cfg.tasks_enabled,
            call_timeout: cfg.call_timeout,
            evidence_timeout: cfg.evidence_timeout,
            counter: Arc::new(AtomicU64::new(1)),
            boot,
            cfg: Arc::new(cfg.clone()),
            health: HealthCache::default(),
        }
    }

    /// Start the background health refresh (see `health`).
    pub fn spawn_health(
        &self,
        cfg: &Config,
        shutdown: tokio::sync::watch::Receiver<bool>,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(crate::health::refresh_loop(
            cfg.clone(),
            self.worker.clone(),
            self.ask_slot.clone(),
            self.health.clone(),
            shutdown,
        ))
    }

    pub fn registry(&self) -> Arc<Registry> {
        self.tasks.registry.clone()
    }
}

#[derive(Clone)]
pub struct RequestId(pub String);

pub fn router(state: AppState) -> Router {
    let mut r = Router::new()
        .route("/v1/status", get(status))
        .route("/v1/sources", get(sources))
        .route("/v1/ask", post(ask));
    // `DOCKET_TASKS=off` leaves these routes (and the capability) out entirely.
    if state.tasks_enabled {
        r = r
            .route("/v1/tasks", post(create_task).get(list_tasks))
            .route("/v1/tasks/{id}/events", get(task_events))
            .route("/v1/tasks/{id}/cancel", post(cancel_task))
            .route("/v1/evidence/get", post(evidence_get));
    }
    r.layer(DefaultBodyLimit::max(MAX_BODY))
        .layer(middleware::from_fn_with_state(state.clone(), request_id))
        .with_state(state)
}

/// Assign a request id, expose it as `x-request-id`, and log one structured
/// line per request. Bodies are never logged.
async fn request_id(State(st): State<AppState>, mut req: Request, next: Next) -> Response {
    let n = st.counter.fetch_add(1, Ordering::Relaxed);
    let rid = format!("{:08x}-{:06x}", st.boot, n);
    req.extensions_mut().insert(RequestId(rid.clone()));
    let method = req.method().clone();
    let path = req.uri().path().to_string();
    let t0 = Instant::now();
    let mut resp = next.run(req).await;
    if let Ok(v) = HeaderValue::from_str(&rid) {
        resp.headers_mut().insert("x-request-id", v);
    }
    info!(target: "docketd::http", request_id = %rid, %method, %path,
        status = resp.status().as_u16(), ms = t0.elapsed().as_millis() as u64, "request");
    resp
}

fn api_error(status: StatusCode, code: &str, message: &str, rid: &str) -> Response {
    let body = ApiError {
        error: ErrorBody {
            code: code.into(),
            message: message.into(),
            request_id: rid.into(),
        },
    };
    (status, Json(body)).into_response()
}

/// Map a worker failure to an HTTP status and stable code. The log line holds
/// only the code, never the message (which may mention user content).
fn call_error(e: CallError, rid: &str) -> (StatusCode, ErrorBody) {
    let (status, code, message) = match e {
        CallError::Unavailable(m) => (StatusCode::SERVICE_UNAVAILABLE, "worker_unavailable", m),
        CallError::Timeout => (
            StatusCode::GATEWAY_TIMEOUT,
            "timeout",
            "the worker did not answer in time".to_string(),
        ),
        CallError::Crashed => (
            StatusCode::BAD_GATEWAY,
            "worker_crashed",
            "the worker stopped unexpectedly; it is being restarted".to_string(),
        ),
        CallError::Cancelled { .. } => (
            StatusCode::SERVICE_UNAVAILABLE,
            "cancelled",
            "the request was cancelled".to_string(),
        ),
        CallError::Rpc(err) => match err.kind() {
            Some("NoContentIndexed") => (
                StatusCode::CONFLICT,
                "no_content",
                "Nothing is indexed yet. Add a folder with the docket CLI first.".to_string(),
            ),
            _ if err.code == docket_proto::rpc::INVALID_PARAMS => {
                (StatusCode::BAD_REQUEST, "bad_request", err.message)
            }
            _ => (StatusCode::BAD_GATEWAY, "worker_error", err.message),
        },
    };
    warn!(target: "docketd::http", request_id = %rid, code, "call failed");
    (
        status,
        ErrorBody {
            code: code.into(),
            message,
            request_id: rid.into(),
        },
    )
}

async fn status(State(st): State<AppState>) -> Json<StatusResponse> {
    // Never calls the worker: everything here is the daemon's own state or the
    // cached health with its ages computed now.
    let info = st.worker.info();
    let meta = st.worker.meta();
    let busy = st.ask_slot.available_permits() == 0;
    let v2 =
        !st.cfg.health_interval.is_zero() && meta.features.iter().any(|f| f == health::FEATURE);
    let mut capabilities = Vec::new();
    if st.tasks_enabled {
        capabilities.push("tasks.v1".to_string());
    }
    if v2 {
        capabilities.push("status.v2".to_string());
    }
    if st.tasks_enabled && meta.features.iter().any(|f| f == EVIDENCE_FEATURE) {
        capabilities.push(EVIDENCE_FEATURE.to_string());
    }
    Json(StatusResponse {
        service: "ok".into(),
        version: env!("CARGO_PKG_VERSION").into(),
        busy,
        capabilities,
        health: v2.then(|| {
            Box::new(
                st.health
                    .view(&st.cfg, &info, &meta, busy, health::now_ms()),
            )
        }),
        worker: info,
    })
}

async fn sources(State(st): State<AppState>, Extension(rid): Extension<RequestId>) -> Response {
    match st
        .worker
        .call(
            "sources.list",
            Value::Null,
            CallOpts::timeout(st.call_timeout),
        )
        .await
    {
        Ok(v) => match serde_json::from_value::<SourcesResponse>(v) {
            Ok(mut s) => {
                // Typed pass-through: only closed-set codes and counts survive.
                for src in &mut s.sources {
                    if let Some(c) = &mut src.counts {
                        c.sanitize();
                    }
                    src.anomalies = std::mem::take(&mut src.anomalies)
                        .into_iter()
                        .map(|a| docket_proto::status::sanitize_code(&a))
                        .collect();
                }
                Json(s).into_response()
            }
            Err(_) => api_error(
                StatusCode::BAD_GATEWAY,
                "bad_worker_response",
                "unexpected worker response",
                &rid.0,
            ),
        },
        Err(e) => {
            let (status, body) = call_error(e, &rid.0);
            (status, Json(ApiError { error: body })).into_response()
        }
    }
}

fn parse_ask(body: &Bytes) -> Result<AskRequest, &'static str> {
    let req: AskRequest =
        serde_json::from_slice(body).map_err(|_| "body must be {\"question\": string}")?;
    if req.question.trim().is_empty() {
        return Err("question must not be empty");
    }
    if req.question.chars().count() > MAX_QUESTION_CHARS {
        return Err("question is too long");
    }
    Ok(req)
}

/// Cancels its task when dropped. Dropping happens when the client goes away
/// (the HTTP future or stream is dropped); after a normal finish it is a no-op.
struct CancelOnDrop {
    registry: Arc<Registry>,
    id: String,
}

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.registry
            .request_cancel(&self.id, CancelSource::ClientGone);
    }
}

fn busy(rid: &str) -> Response {
    api_error(
        StatusCode::TOO_MANY_REQUESTS,
        "busy",
        "another question is already being answered",
        rid,
    )
}

fn sse_event(name: &str, data: &impl serde::Serialize) -> Option<Event> {
    Event::default().event(name).json_data(data).ok()
}

/// HTTP status for the stable error codes of a failed task.
fn status_of(code: &str) -> StatusCode {
    match code {
        "worker_unavailable" | "cancelled" => StatusCode::SERVICE_UNAVAILABLE,
        "timeout" => StatusCode::GATEWAY_TIMEOUT,
        "no_content" => StatusCode::CONFLICT,
        "bad_request" => StatusCode::BAD_REQUEST,
        _ => StatusCode::BAD_GATEWAY,
    }
}

/// Map a task event to the OLD `/v1/ask` SSE vocabulary (`phase`, `delta`,
/// `result`, `error`); everything else is invisible to old clients.
fn compat_event(ev: &TaskEvent, rid: &str) -> Option<Event> {
    let err = |code: &str, message: &str| {
        sse_event(
            "error",
            &ApiError {
                error: ErrorBody {
                    code: code.into(),
                    message: message.into(),
                    request_id: rid.into(),
                },
            },
        )
    };
    match &ev.body {
        TaskEventBody::Phase { phase } => {
            sse_event("phase", &serde_json::json!({ "phase": phase }))
        }
        TaskEventBody::Delta { text } => sse_event("delta", &serde_json::json!({ "text": text })),
        TaskEventBody::Result { result } => sse_event("result", result),
        TaskEventBody::Error { code, message } => err(code, message),
        TaskEventBody::State {
            state: TaskState::Cancelled,
        } => err("cancelled", "the question was cancelled"),
        _ => None,
    }
}

async fn ask(
    State(st): State<AppState>,
    Extension(rid): Extension<RequestId>,
    headers: axum::http::HeaderMap,
    body: Bytes,
) -> Response {
    let req = match parse_ask(&body) {
        Ok(r) => r,
        Err(m) => return api_error(StatusCode::BAD_REQUEST, "bad_request", m, &rid.0),
    };
    let wants_sse = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.contains("text/event-stream"));
    // Plain JSON has nowhere to put deltas: never ask the worker to stream.
    let req = if wants_sse {
        req
    } else {
        AskRequest {
            stream: false,
            ..req
        }
    };
    // `/v1/ask` is a thin adapter over a task. No waiting: busy means 429.
    let registry = st.registry();
    let (id, _) = match st.tasks.submit(req, None, false) {
        Ok(t) => t,
        Err(SubmitError::Busy) => return busy(&rid.0),
    };
    let Some(mut sub) = registry.subscribe(&id.0, 0) else {
        return api_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal",
            "task vanished",
            &rid.0,
        );
    };
    let guard = CancelOnDrop {
        registry: registry.clone(),
        id: id.0.clone(),
    };

    if !wants_sse {
        let mut result: Option<AskResult> = None;
        let mut error: Option<(String, String)> = None;
        let mut state = TaskState::Queued;
        while let Pull::Event(ev) = sub.next().await {
            match ev.body {
                TaskEventBody::Result { result: r } => result = Some(*r),
                TaskEventBody::Error { code, message } => error = Some((code, message)),
                TaskEventBody::State { state: s } => state = s,
                _ => {}
            }
        }
        drop(guard);
        return match (result, error) {
            (Some(r), _) => Json(r).into_response(),
            (None, Some((code, message))) => {
                warn!(target: "docketd::http", request_id = %rid.0, code = %code, "call failed");
                api_error(status_of(&code), &code, &message, &rid.0)
            }
            (None, None) => {
                let (code, m) = if state == TaskState::Cancelled {
                    ("cancelled", "the request was cancelled")
                } else {
                    ("worker_error", "the question could not be answered")
                };
                api_error(status_of(code), code, m, &rid.0)
            }
        };
    }

    let (etx, erx) = mpsc::channel::<Result<Event, Infallible>>(32);
    let rid = rid.0;
    tokio::spawn(async move {
        // Dropped on every exit path: a client that goes away (the receiver
        // closes) cancels the task and frees the worker.
        let _guard = guard;
        loop {
            tokio::select! {
                _ = etx.closed() => break,
                n = sub.next() => match n {
                    Pull::Event(ev) => {
                        if let Some(e) = compat_event(&ev, &rid) {
                            if etx.send(Ok(e)).await.is_err() {
                                break;
                            }
                        }
                    }
                    Pull::Gap => {
                        if let Some(e) = sse_event("error", &ApiError { error: ErrorBody {
                            code: "stream_lagged".into(),
                            message: "the answer stream fell behind".into(),
                            request_id: rid.clone(),
                        }}) {
                            let _ = etx.send(Ok(e)).await;
                        }
                        break;
                    }
                    Pull::End => break,
                },
            }
        }
    });
    Sse::new(ReceiverStream::new(erx)).into_response()
}

async fn create_task(
    State(st): State<AppState>,
    Extension(rid): Extension<RequestId>,
    body: Bytes,
) -> Response {
    let req: CreateTaskRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(_) => {
            return api_error(
                StatusCode::BAD_REQUEST,
                "bad_request",
                "body must be {\"kind\":\"ask\",\"question\": string}",
                &rid.0,
            )
        }
    };
    let ask = req.ask();
    if let Err(m) = parse_ask(&Bytes::from(serde_json::to_vec(&ask).unwrap_or_default())) {
        return api_error(StatusCode::BAD_REQUEST, "bad_request", m, &rid.0);
    }
    match st.tasks.submit(ask, req.deadline_ms, true) {
        Ok((id, _)) => {
            let state = st.tasks.registry.state(&id.0).unwrap_or(TaskState::Queued);
            (
                StatusCode::ACCEPTED,
                Json(CreateTaskResponse { task_id: id, state }),
            )
                .into_response()
        }
        Err(SubmitError::Busy) => busy(&rid.0),
    }
}

async fn list_tasks(State(st): State<AppState>) -> Json<TasksResponse> {
    Json(TasksResponse {
        tasks: st.tasks.registry.list(),
    })
}

async fn task_events(
    State(st): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(id): Path<String>,
    headers: axum::http::HeaderMap,
) -> Response {
    let after = headers
        .get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(0);
    let Some(mut sub) = st.tasks.registry.subscribe(&id, after) else {
        return api_error(StatusCode::NOT_FOUND, "not_found", "no such task", &rid.0);
    };
    let (etx, erx) = mpsc::channel::<Result<Event, Infallible>>(32);
    tokio::spawn(async move {
        // Watching a task never cancels it, whether or not the watcher leaves.
        loop {
            tokio::select! {
                _ = etx.closed() => break,
                n = sub.next() => match n {
                    Pull::Event(ev) => {
                        let e = Event::default()
                            .id(ev.seq.to_string())
                            .event(ev.body.event_name())
                            .json_data(&*ev);
                        if let Ok(e) = e {
                            if etx.send(Ok(e)).await.is_err() {
                                break;
                            }
                        }
                    }
                    Pull::Gap => sub.resync(),
                    Pull::End => break,
                },
            }
        }
    });
    Sse::new(ReceiverStream::new(erx)).into_response()
}

async fn cancel_task(
    State(st): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(id): Path<String>,
    body: Bytes,
) -> Response {
    if !body.is_empty() && serde_json::from_slice::<CancelRequest>(&body).is_err() {
        return api_error(
            StatusCode::BAD_REQUEST,
            "bad_request",
            "body must be {}",
            &rid.0,
        );
    }
    let reg = &st.tasks.registry;
    let (accepted, state) = match reg.request_cancel(&id, CancelSource::User) {
        CancelOutcome::NotFound => {
            return api_error(StatusCode::NOT_FOUND, "not_found", "no such task", &rid.0)
        }
        CancelOutcome::Accepted => (true, TaskState::Cancelling),
        CancelOutcome::Already(s) => (false, s),
    };
    Json(CancelResponse {
        task_id: TaskId(id),
        state,
        accepted,
    })
    .into_response()
}

// ---------------------------------------------------------------------------
// POST /v1/evidence/get
// ---------------------------------------------------------------------------

fn valid_evidence_id(s: &str) -> bool {
    let n = s.chars().count();
    (1..=MAX_EVIDENCE_ID_CHARS).contains(&n) && !s.contains('\0')
}

/// Strict request parsing. The error is a unit: the caller answers with one
/// fixed message, so no value is ever echoed.
fn parse_evidence(body: &[u8]) -> Result<EvidenceRequest, ()> {
    if body.len() > MAX_EVIDENCE_BODY {
        return Err(());
    }
    let req: EvidenceRequest = serde_json::from_slice(body).map_err(|_| ())?;
    let window_ok = |w: Option<u8>| w.is_none_or(|n| n <= MAX_EVIDENCE_CONTEXT);
    if valid_evidence_id(&req.task_id)
        && valid_evidence_id(&req.chunk_id)
        && window_ok(req.before)
        && window_ok(req.after)
    {
        Ok(req)
    } else {
        Err(())
    }
}

/// Typed outcomes are HTTP 200 with only the outcome body; the log line (debug
/// level) carries the outcome code and nothing else.
fn evidence_reply(r: EvidenceResponse) -> Response {
    let code = match &r {
        EvidenceResponse::Ok(_) => "ok",
        EvidenceResponse::NotFound => "not_found",
        EvidenceResponse::SourceUnavailable => "source_unavailable",
        EvidenceResponse::Superseded => "superseded",
        EvidenceResponse::NotCited => "not_cited",
        EvidenceResponse::Busy => "busy",
        EvidenceResponse::Timeout => "timeout",
        EvidenceResponse::WorkerUnavailable => "worker_unavailable",
        EvidenceResponse::Unsupported | EvidenceResponse::Unknown => "unsupported",
    };
    tracing::debug!(target: "docketd::evidence", outcome = code, "evidence read");
    (StatusCode::OK, Json(r)).into_response()
}

/// Exactly the params the worker is sent. Everything comes from the grant
/// (minted from the task's own citations); the client contributes only the
/// optional context window. The worker default (1) applies when absent.
fn evidence_params(grant: &Grant, req: &EvidenceRequest) -> (String, Value) {
    let sent = grant
        .resolved_chunk_id
        .clone()
        .unwrap_or_else(|| grant.chunk_id.clone());
    let mut p = serde_json::Map::new();
    p.insert("chunk_id".into(), Value::String(sent.clone()));
    p.insert(
        "allowed_version_ids".into(),
        Value::Array(vec![Value::String(grant.evidence_version_id.clone())]),
    );
    if let Some(b) = req.before {
        p.insert("before".into(), b.into());
    }
    if let Some(a) = req.after {
        p.insert("after".into(), a.into());
    }
    if !grant.excerpt.is_empty() {
        p.insert(
            "answer_excerpt".into(),
            Value::String(grant.excerpt.clone()),
        );
    }
    (sent, Value::Object(p))
}

/// Turn the worker's answer into the client's. Defence in depth: the worker is
/// trusted to enforce `allowed_version_ids`, but data from another version,
/// another source or another chunk is never returned. Worker-originated
/// daemon-only outcomes (`not_cited`, `busy`, `timeout`, `worker_unavailable`),
/// an unknown outcome and a malformed result all become `unsupported`: a worker
/// cannot make the daemon grant or deny anything by naming them.
fn map_evidence_result(
    res: Result<Value, CallError>,
    grant: &Grant,
    sent: &str,
) -> EvidenceResponse {
    let v = match res {
        Ok(v) => v,
        Err(CallError::Timeout) => return EvidenceResponse::Timeout,
        Err(_) => return EvidenceResponse::WorkerUnavailable,
    };
    let Ok(parsed) = serde_json::from_value::<EvidenceResponse>(v) else {
        mismatch("malformed_result");
        return EvidenceResponse::Unsupported;
    };
    match parsed {
        EvidenceResponse::Ok(mut ok) => {
            let same = |p: &docket_proto::evidence::Passage| {
                p.evidence_version_id == grant.evidence_version_id && p.source_id == grant.source_id
            };
            let p = &ok.passage;
            let chunk_ok = p.chunk_id == sent
                || p.replaced_from
                    .as_deref()
                    .is_some_and(|r| r == sent || r == grant.chunk_id);
            if !(same(p)
                && chunk_ok
                && ok.context_before.iter().all(same)
                && ok.context_after.iter().all(same))
            {
                mismatch("identity_mismatch");
                return EvidenceResponse::NotFound;
            }
            ok.untrusted_content = true;
            EvidenceResponse::Ok(ok)
        }
        r @ (EvidenceResponse::NotFound
        | EvidenceResponse::SourceUnavailable
        | EvidenceResponse::Superseded) => r,
        _ => {
            mismatch("unexpected_outcome");
            EvidenceResponse::Unsupported
        }
    }
}

/// Content-free diagnostic for a worker reply that broke the evidence
/// contract: a fixed reason code only, never ids, text or the reply itself.
fn mismatch(reason: &'static str) {
    tracing::warn!(target: "docketd::evidence", reason, "evidence protocol mismatch");
}

async fn evidence_get(
    State(st): State<AppState>,
    Extension(rid): Extension<RequestId>,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    // a. Validate. One fixed message; no value is ever echoed.
    let Some(req) = body.ok().and_then(|b| parse_evidence(&b).ok()) else {
        return api_error(
            StatusCode::BAD_REQUEST,
            "bad_request",
            "invalid evidence request",
            &rid.0,
        );
    };
    // b. Authorize, before anything that depends on existence or worker state.
    let grant = match st.tasks.registry.grant_lookup(&req.task_id, &req.chunk_id) {
        GrantLookup::NotCited => return evidence_reply(EvidenceResponse::NotCited),
        GrantLookup::Unsupported => return evidence_reply(EvidenceResponse::Unsupported),
        GrantLookup::Granted(g) => g,
    };
    // c. Worker state. State is checked first: while a worker restarts its
    // feature list is momentarily empty and that must read as unavailable,
    // not as an old worker.
    if st.worker.info().state != WorkerState::Up {
        return evidence_reply(EvidenceResponse::WorkerUnavailable);
    }
    if !st
        .worker
        .meta()
        .features
        .iter()
        .any(|f| f == EVIDENCE_FEATURE)
    {
        return evidence_reply(EvidenceResponse::Unsupported);
    }
    // d. A question is running or queued: never queue behind it.
    if st.ask_slot.available_permits() == 0 {
        return evidence_reply(EvidenceResponse::Busy);
    }
    // e. Neither the ask slot nor any task state is touched. A soft timeout
    // leaves the worker running.
    let (sent, params) = evidence_params(&grant, &req);
    let res = st
        .worker
        .call("evidence.get", params, CallOpts::soft(st.evidence_timeout))
        .await;
    // f. Map.
    evidence_reply(map_evidence_result(res, &grant, &sent))
}

#[cfg(test)]
mod evidence_tests {
    use super::*;
    use serde_json::json;

    fn grant() -> Grant {
        Grant {
            chunk_id: "c1".into(),
            source_id: "s1".into(),
            evidence_version_id: "v1".into(),
            resolved_chunk_id: None,
            excerpt: String::new(),
        }
    }

    fn passage(chunk: &str, src: &str, ver: &str) -> Value {
        json!({"chunk_id":chunk,"source_id":src,"source_display_name":"a.txt",
               "evidence_version_id":ver,"ordinal":1,"text":"t"})
    }

    fn ok(p: Value, before: Vec<Value>) -> Value {
        json!({"outcome":"ok","passage":p,"context_before":before,"context_after":[]})
    }

    #[test]
    fn request_validation_is_strict() {
        assert!(parse_evidence(br#"{"task_id":"t","chunk_id":"c"}"#).is_ok());
        assert!(parse_evidence(br#"{"task_id":"t","chunk_id":"c","before":3,"after":0}"#).is_ok());
        let long = "x".repeat(201);
        let bad: Vec<String> = vec![
            "".into(),
            "nope".into(),
            r#"{"task_id":"","chunk_id":"c"}"#.into(),
            r#"{"task_id":"t","chunk_id":""}"#.into(),
            r#"{"task_id":"t","chunk_id":"a\u0000b"}"#.into(),
            format!(r#"{{"task_id":"t","chunk_id":"{long}"}}"#),
            r#"{"task_id":"t","chunk_id":"c","before":4}"#.into(),
            r#"{"task_id":"t","chunk_id":"c","after":-1}"#.into(),
            r#"{"task_id":"t","chunk_id":"c","after":1.5}"#.into(),
            r#"{"task_id":"t","chunk_id":"c","after":"1"}"#.into(),
            r#"{"task_id":"t","chunk_id":"c","evidence_version_id":"v"}"#.into(),
            r#"{"task_id":"t","chunk_id":"c","allowed_version_ids":["v"]}"#.into(),
            r#"{"task_id":"t","chunk_id":"c","scope":"all"}"#.into(),
            format!(
                r#"{{"task_id":"t","chunk_id":"c","pad":"{}"}}"#,
                "x".repeat(9000)
            ),
        ];
        for b in bad {
            assert!(parse_evidence(b.as_bytes()).is_err(), "{b}");
        }
        let two_hundred = "é".repeat(200);
        let b = format!(r#"{{"task_id":"t","chunk_id":"{two_hundred}"}}"#);
        assert!(parse_evidence(b.as_bytes()).is_ok());
    }

    #[test]
    fn params_come_only_from_the_grant() {
        let req = |before, after| EvidenceRequest {
            task_id: "t".into(),
            chunk_id: "c1".into(),
            before,
            after,
        };
        let (sent, p) = evidence_params(&grant(), &req(None, None));
        assert_eq!(sent, "c1");
        assert_eq!(p, json!({"chunk_id":"c1","allowed_version_ids":["v1"]}));
        let mut g = grant();
        g.resolved_chunk_id = Some("new".into());
        g.excerpt = "ex".into();
        let (sent, p) = evidence_params(&g, &req(Some(0), Some(3)));
        assert_eq!(sent, "new");
        assert_eq!(
            p,
            json!({"chunk_id":"new","allowed_version_ids":["v1"],
                   "before":0,"after":3,"answer_excerpt":"ex"})
        );
    }

    #[test]
    fn untrusted_content_is_forced_true_whatever_the_worker_says() {
        let g = grant();
        for flag in [json!(false), json!(null), json!("no")] {
            let mut v = ok(passage("c1", "s1", "v1"), vec![passage("c0", "s1", "v1")]);
            v["untrusted_content"] = flag;
            match map_evidence_result(Ok(v), &g, "c1") {
                EvidenceResponse::Ok(o) => assert!(o.untrusted_content),
                other => assert_eq!(
                    other,
                    EvidenceResponse::Unsupported,
                    "bad flag types are rejected, never ok"
                ),
            }
        }
        let mut v = ok(passage("c1", "s1", "v1"), vec![]);
        v["untrusted_content"] = json!(false);
        match map_evidence_result(Ok(v), &g, "c1") {
            EvidenceResponse::Ok(o) => assert!(o.untrusted_content),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn worker_result_mapping() {
        let g = grant();
        let map = |v: Value| map_evidence_result(Ok(v), &g, "c1");
        assert!(map(ok(
            passage("c1", "s1", "v1"),
            vec![passage("c0", "s1", "v1")]
        ))
        .is_ok());
        // replaced_from names the requested id.
        let mut p = passage("new", "s1", "v1");
        p["replaced_from"] = json!("c1");
        assert!(map(ok(p, vec![])).is_ok());
        for bad in [
            ok(passage("c1", "s1", "v2"), vec![]),
            ok(passage("c1", "s2", "v1"), vec![]),
            ok(passage("zz", "s1", "v1"), vec![]),
            ok(passage("c1", "s1", "v1"), vec![passage("c0", "s1", "v9")]),
            ok(passage("c1", "s1", "v1"), vec![passage("c0", "s9", "v1")]),
        ] {
            assert_eq!(map(bad), EvidenceResponse::NotFound);
        }
        assert_eq!(
            map(json!({"outcome":"superseded"})),
            EvidenceResponse::Superseded
        );
        for o in ["not_cited", "busy", "timeout", "worker_unavailable", "x"] {
            assert_eq!(map(json!({"outcome":o})), EvidenceResponse::Unsupported);
        }
        assert_eq!(map(json!({"nope":1})), EvidenceResponse::Unsupported);
        assert_eq!(map(json!(null)), EvidenceResponse::Unsupported);
        assert_eq!(
            map_evidence_result(Err(CallError::Timeout), &g, "c1"),
            EvidenceResponse::Timeout
        );
        assert_eq!(
            map_evidence_result(Err(CallError::Crashed), &g, "c1"),
            EvidenceResponse::WorkerUnavailable
        );
    }
}
