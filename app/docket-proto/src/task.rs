//! Task API types (`/v1/tasks`, capability `tasks.v1`).
//!
//! A task is one unit of work run by the daemon. R1 knows a single kind,
//! `ask`. Every task has a stream of events with a per-task, strictly
//! increasing `seq` so a client can resume with `Last-Event-ID`.
//!
//! Cancellation is reported as three DISTINCT facts, never merged:
//! * `cancel_requested`: the daemon accepted the request. Nothing more.
//! * `worker_terminated`: the worker process was confirmed gone, and how.
//! * `inference_stopped`: whether the language-model engine stopped
//!   generating. In R1 this is always `unverified`: the daemon cannot observe
//!   the engine, and its behaviour when a client disconnects is not
//!   established. No event ever claims a confirmed stop.

use crate::api::{AskRequest, AskResult, Phase, Scope};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TaskId(pub String);

impl std::fmt::Display for TaskId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskKind {
    Ask,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskState {
    Queued,
    Running,
    Cancelling,
    Succeeded,
    Failed,
    Cancelled,
    TimedOut,
}

impl TaskState {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Succeeded | Self::Failed | Self::Cancelled | Self::TimedOut
        )
    }
}

/// Who asked for the task to stop.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CancelSource {
    User,
    Timeout,
    Shutdown,
    ClientGone,
}

/// How the worker process ended when a task was cancelled.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkerExit {
    /// Left on its own after a termination request, within the grace period.
    Exited,
    /// Had to be killed (process group SIGKILL).
    Killed,
    /// Was already gone when the daemon looked.
    AlreadyDead,
}

/// Whether the model engine stopped generating. R1 can only say `unverified`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InferenceStatus {
    Unverified,
}

/// Closed set of reasons for a `failed` task (the `code` of its `error` event).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureReason {
    WorkerUnavailable,
    WorkerCrashed,
    WorkerError,
    BadWorkerResponse,
    NoContent,
    BadRequest,
}

impl FailureReason {
    pub fn code(self) -> &'static str {
        match self {
            Self::WorkerUnavailable => "worker_unavailable",
            Self::WorkerCrashed => "worker_crashed",
            Self::WorkerError => "worker_error",
            Self::BadWorkerResponse => "bad_worker_response",
            Self::NoContent => "no_content",
            Self::BadRequest => "bad_request",
        }
    }
}

/// Body of `POST /v1/tasks`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CreateTaskRequest {
    pub kind: TaskKind,
    pub question: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope: Option<Scope>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub stream: bool,
    /// Optional run-time limit in milliseconds; never above the daemon's own.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deadline_ms: Option<u64>,
}

impl CreateTaskRequest {
    pub fn ask(&self) -> AskRequest {
        AskRequest {
            question: self.question.clone(),
            scope: self.scope.clone(),
            stream: self.stream,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CreateTaskResponse {
    pub task_id: TaskId,
    pub state: TaskState,
}

/// Optional body of `POST /v1/tasks/{id}/cancel` (an empty body is fine).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CancelRequest {}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CancelResponse {
    pub task_id: TaskId,
    /// State after the call. Cancelling is not Cancelled: see the module docs.
    pub state: TaskState,
    /// False when the task was already cancelling or finished (idempotent).
    pub accepted: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskInfo {
    pub task_id: TaskId,
    pub kind: TaskKind,
    pub state: TaskState,
    pub created_ms: u64,
    /// Run-time limit applied to this task.
    pub deadline_ms: u64,
    /// Sequence number of the newest event.
    pub last_seq: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TasksResponse {
    pub tasks: Vec<TaskInfo>,
}

/// One event of a task's stream.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TaskEvent {
    pub task_id: TaskId,
    pub seq: u64,
    pub at_ms: u64,
    #[serde(flatten)]
    pub body: TaskEventBody,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum TaskEventBody {
    State { state: TaskState },
    Phase { phase: Phase },
    /// Unvalidated answer text.
    Delta { text: String },
    /// Authoritative answer.
    Result { result: Box<AskResult> },
    Error { code: String, message: String },
    CancelRequested { by: CancelSource },
    WorkerTerminated { how: WorkerExit },
    InferenceStopped { status: InferenceStatus },
}

impl TaskEventBody {
    /// SSE event name. `phase`, `delta`, `result`, `error` keep their old meaning.
    pub fn event_name(&self) -> &'static str {
        match self {
            Self::State { .. } => "state",
            Self::Phase { .. } => "phase",
            Self::Delta { .. } => "delta",
            Self::Result { .. } => "result",
            Self::Error { .. } => "error",
            Self::CancelRequested { .. } => "cancel_requested",
            Self::WorkerTerminated { .. } => "worker_terminated",
            Self::InferenceStopped { .. } => "inference_stopped",
        }
    }
}

/// Decode one SSE event of `GET /v1/tasks/{id}/events`. `None` for malformed
/// data and for event types this build does not know.
pub fn parse_task_event(data: &str) -> Option<TaskEvent> {
    serde_json::from_str(data).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(body: TaskEventBody) -> TaskEvent {
        TaskEvent {
            task_id: TaskId("t-1-1".into()),
            seq: 7,
            at_ms: 1000,
            body,
        }
    }

    #[test]
    fn event_wire_shape_is_flat_and_tagged() {
        let v = serde_json::to_value(ev(TaskEventBody::CancelRequested {
            by: CancelSource::ClientGone,
        }))
        .unwrap();
        assert_eq!(
            v,
            json!({"task_id":"t-1-1","seq":7,"at_ms":1000,"type":"cancel_requested","by":"client_gone"})
        );
        let v = serde_json::to_value(ev(TaskEventBody::InferenceStopped {
            status: InferenceStatus::Unverified,
        }))
        .unwrap();
        assert_eq!(v["type"], "inference_stopped");
        assert_eq!(v["status"], "unverified");
        let v = serde_json::to_value(ev(TaskEventBody::WorkerTerminated {
            how: WorkerExit::AlreadyDead,
        }))
        .unwrap();
        assert_eq!(v["how"], "already_dead");
    }

    #[test]
    fn every_event_round_trips() {
        let bodies = vec![
            TaskEventBody::State {
                state: TaskState::TimedOut,
            },
            TaskEventBody::Phase {
                phase: Phase::Validating,
            },
            TaskEventBody::Delta { text: "x".into() },
            TaskEventBody::Result {
                result: Box::new(AskResult {
                    answer: "a".into(),
                    abstained: false,
                    citations: vec![],
                    timings: Default::default(),
                }),
            },
            TaskEventBody::Error {
                code: "worker_crashed".into(),
                message: "m".into(),
            },
            TaskEventBody::CancelRequested {
                by: CancelSource::User,
            },
            TaskEventBody::WorkerTerminated {
                how: WorkerExit::Killed,
            },
            TaskEventBody::InferenceStopped {
                status: InferenceStatus::Unverified,
            },
        ];
        for b in bodies {
            let e = ev(b);
            let s = serde_json::to_string(&e).unwrap();
            assert_eq!(parse_task_event(&s), Some(e), "{s}");
        }
    }

    #[test]
    fn unknown_event_types_and_garbage_are_skipped() {
        assert_eq!(
            parse_task_event(r#"{"task_id":"t","seq":1,"at_ms":1,"type":"future_thing"}"#),
            None
        );
        assert_eq!(parse_task_event("nope"), None);
    }

    #[test]
    fn state_names_and_terminality() {
        let names: Vec<_> = [
            TaskState::Queued,
            TaskState::Running,
            TaskState::Cancelling,
            TaskState::Succeeded,
            TaskState::Failed,
            TaskState::Cancelled,
            TaskState::TimedOut,
        ]
        .iter()
        .map(|s| serde_json::to_value(s).unwrap().as_str().unwrap().to_string())
        .collect();
        assert_eq!(
            names,
            [
                "queued",
                "running",
                "cancelling",
                "succeeded",
                "failed",
                "cancelled",
                "timed_out"
            ]
        );
        assert!(!TaskState::Cancelling.is_terminal() && !TaskState::Running.is_terminal());
        assert!(TaskState::TimedOut.is_terminal() && TaskState::Cancelled.is_terminal());
    }

    #[test]
    fn create_request_accepts_only_ask_and_defaults() {
        let r: CreateTaskRequest =
            serde_json::from_value(json!({"kind":"ask","question":"q"})).unwrap();
        assert!(!r.stream && r.deadline_ms.is_none());
        assert_eq!(r.ask().question, "q");
        assert!(
            serde_json::from_value::<CreateTaskRequest>(json!({"kind":"ingest","question":"q"}))
                .is_err()
        );
        assert_eq!(
            serde_json::to_value(&r).unwrap(),
            json!({"kind":"ask","question":"q"})
        );
    }

    #[test]
    fn failure_reason_codes_match_wire_names() {
        for r in [
            FailureReason::WorkerUnavailable,
            FailureReason::WorkerCrashed,
            FailureReason::WorkerError,
            FailureReason::BadWorkerResponse,
            FailureReason::NoContent,
            FailureReason::BadRequest,
        ] {
            assert_eq!(serde_json::to_value(r).unwrap(), json!(r.code()));
        }
    }

    #[test]
    fn cancel_body_may_be_empty_object() {
        assert_eq!(
            serde_json::from_str::<CancelRequest>("{}").unwrap(),
            CancelRequest {}
        );
    }
}
