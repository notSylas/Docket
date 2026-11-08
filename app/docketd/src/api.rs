//! The local HTTP API. Served only on the Unix socket.

use crate::config::Config;
use crate::worker::{CallError, WorkerHandle};
use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Extension, Request, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::middleware::{self, Next};
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use docket_proto::api::{
    ApiError, AskRequest, AskResult, ErrorBody, Phase, SourcesResponse, StatusResponse,
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

#[derive(Clone)]
pub struct AppState {
    worker: WorkerHandle,
    /// One question at a time.
    ask_slot: Arc<Semaphore>,
    ask_timeout: Duration,
    call_timeout: Duration,
    counter: Arc<AtomicU64>,
    boot: u64,
}

impl AppState {
    pub fn new(cfg: &Config, worker: WorkerHandle) -> Self {
        let boot = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs() & 0xffff_ffff)
            .unwrap_or(0);
        Self {
            worker,
            ask_slot: Arc::new(Semaphore::new(1)),
            ask_timeout: cfg.ask_timeout,
            call_timeout: cfg.call_timeout,
            counter: Arc::new(AtomicU64::new(1)),
            boot,
        }
    }
}

#[derive(Clone)]
pub struct RequestId(pub String);

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/v1/status", get(status))
        .route("/v1/sources", get(sources))
        .route("/v1/ask", post(ask))
        .layer(DefaultBodyLimit::max(MAX_BODY))
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
    Json(StatusResponse {
        service: "ok".into(),
        version: env!("CARGO_PKG_VERSION").into(),
        worker: st.worker.info(),
        busy: st.ask_slot.available_permits() == 0,
    })
}

async fn sources(State(st): State<AppState>, Extension(rid): Extension<RequestId>) -> Response {
    match st
        .worker
        .call("sources.list", Value::Null, st.call_timeout, None)
        .await
    {
        Ok(v) => match serde_json::from_value::<SourcesResponse>(v) {
            Ok(s) => Json(s).into_response(),
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

fn phase_of(params: &Value) -> Option<Phase> {
    Phase::parse(params.get("phase")?.as_str()?)
}

async fn run_ask(
    st: &AppState,
    req: &AskRequest,
    progress: Option<mpsc::UnboundedSender<Value>>,
) -> Result<AskResult, CallError> {
    let params = serde_json::to_value(req).unwrap_or(Value::Null);
    let v = st
        .worker
        .call("query.ask", params, st.ask_timeout, progress)
        .await?;
    serde_json::from_value::<AskResult>(v).map_err(|_| {
        CallError::Rpc(docket_proto::rpc::RpcError {
            code: docket_proto::rpc::SERVER_ERROR,
            message: "unexpected worker response".into(),
            data: None,
        })
    })
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
    let Ok(permit) = st.ask_slot.clone().try_acquire_owned() else {
        return api_error(
            StatusCode::TOO_MANY_REQUESTS,
            "busy",
            "another question is already being answered",
            &rid.0,
        );
    };
    let wants_sse = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.contains("text/event-stream"));

    if !wants_sse {
        let out = run_ask(&st, &req, None).await;
        drop(permit);
        return match out {
            Ok(r) => Json(r).into_response(),
            Err(e) => {
                let (status, body) = call_error(e, &rid.0);
                (status, Json(ApiError { error: body })).into_response()
            }
        };
    }

    let (etx, erx) = mpsc::channel::<Result<Event, Infallible>>(32);
    tokio::spawn(async move {
        // The permit lives as long as the worker is busy, even if the client
        // goes away: the worker cannot be interrupted mid-question.
        let _permit = permit;
        let send_phase = |etx: &mpsc::Sender<Result<Event, Infallible>>, p: Phase| {
            let ev = Event::default()
                .event("phase")
                .json_data(serde_json::json!({ "phase": p }))
                .ok();
            let etx = etx.clone();
            async move {
                if let Some(ev) = ev {
                    let _ = etx.send(Ok(ev)).await;
                }
            }
        };
        send_phase(&etx, Phase::Retrieving).await;
        let (ptx, mut prx) = mpsc::unbounded_channel::<Value>();
        let fwd_tx = etx.clone();
        let fwd = tokio::spawn(async move {
            let mut last = Phase::Retrieving;
            while let Some(p) = prx.recv().await {
                if let Some(ph) = phase_of(&p) {
                    if ph != last && ph != Phase::Done {
                        last = ph;
                        if let Ok(ev) = Event::default()
                            .event("phase")
                            .json_data(serde_json::json!({ "phase": ph }))
                        {
                            let _ = fwd_tx.send(Ok(ev)).await;
                        }
                    }
                }
            }
        });
        let out = run_ask(&st, &req, Some(ptx)).await;
        let _ = fwd.await;
        match out {
            Ok(r) => {
                send_phase(&etx, Phase::Done).await;
                if let Ok(ev) = Event::default().event("result").json_data(&r) {
                    let _ = etx.send(Ok(ev)).await;
                }
            }
            Err(e) => {
                let (_, body) = call_error(e, &rid.0);
                if let Ok(ev) = Event::default()
                    .event("error")
                    .json_data(ApiError { error: body })
                {
                    let _ = etx.send(Ok(ev)).await;
                }
            }
        }
    });
    Sse::new(ReceiverStream::new(erx)).into_response()
}
