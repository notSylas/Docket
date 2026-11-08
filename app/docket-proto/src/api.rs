//! Local HTTP/JSON API types (`/v1/...`) and the worker result shapes.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Phases reported while a question runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Phase {
    Retrieving,
    Reading,
    Answering,
    Done,
}

impl Phase {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "retrieving" => Some(Self::Retrieving),
            "reading" => Some(Self::Reading),
            "answering" => Some(Self::Answering),
            "done" => Some(Self::Done),
            _ => None,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Retrieving => "Searching...",
            Self::Reading => "Reading evidence...",
            Self::Answering => "Writing answer...",
            Self::Done => "Done",
        }
    }
}

/// Restricts a question to one source or folder (both optional).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Scope {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub folder: Option<String>,
}

/// Body of `POST /v1/ask` and params of the worker's `query.ask`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AskRequest {
    pub question: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope: Option<Scope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Citation {
    pub label: String,
    pub source_display_name: String,
    pub chunk_id: String,
    #[serde(default)]
    pub excerpt: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Timings {
    #[serde(default)]
    pub total_s: f64,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub by_phase: BTreeMap<String, f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AskResult {
    pub answer: String,
    pub abstained: bool,
    #[serde(default)]
    pub citations: Vec<Citation>,
    #[serde(default)]
    pub timings: Timings,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceInfo {
    pub id: String,
    pub display_name: String,
    pub path: String,
    pub status: String,
    #[serde(default)]
    pub files: u32,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourcesResponse {
    pub sources: Vec<SourceInfo>,
}

/// Result of the worker's `status` method.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerStatus {
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub embed_model: String,
    #[serde(default)]
    pub chunks: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WorkerState {
    Starting,
    Up,
    Down,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerInfo {
    pub state: WorkerState,
    pub restarts: u64,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub chunks: Option<u64>,
}

/// Response of `GET /v1/status`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StatusResponse {
    pub service: String,
    pub version: String,
    pub worker: WorkerInfo,
    pub busy: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorBody {
    pub code: String,
    pub message: String,
    pub request_id: String,
}

/// Error envelope for every non-2xx response and SSE `error` event.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApiError {
    pub error: ErrorBody,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn ask_result_round_trip() {
        let r = AskResult {
            answer: "2,650 [a#1]".into(),
            abstained: false,
            citations: vec![Citation {
                label: "[a#1]".into(),
                source_display_name: "a.txt".into(),
                chunk_id: "c1".into(),
                excerpt: "x".into(),
            }],
            timings: Timings {
                total_s: 1.5,
                by_phase: BTreeMap::from([("generation".to_string(), 1.2)]),
            },
        };
        let s = serde_json::to_string(&r).unwrap();
        assert_eq!(serde_json::from_str::<AskResult>(&s).unwrap(), r);
    }

    #[test]
    fn worker_shape_deserializes_with_defaults() {
        let r: AskResult =
            serde_json::from_value(json!({"answer":"I don't know.","abstained":true})).unwrap();
        assert!(r.abstained && r.citations.is_empty() && r.timings.total_s == 0.0);
    }

    #[test]
    fn scope_and_phase_wire_names() {
        let a = AskRequest {
            question: "q".into(),
            scope: Some(Scope {
                source_id: Some("s".into()),
                folder: None,
            }),
        };
        assert_eq!(
            serde_json::to_value(&a).unwrap(),
            json!({"question":"q","scope":{"source_id":"s"}})
        );
        assert_eq!(
            serde_json::to_value(Phase::Reading).unwrap(),
            json!("reading")
        );
        assert_eq!(Phase::parse("done"), Some(Phase::Done));
        assert_eq!(Phase::parse("bogus"), None);
    }

    #[test]
    fn status_round_trip() {
        let s = StatusResponse {
            service: "ok".into(),
            version: "0.1.0".into(),
            worker: WorkerInfo {
                state: WorkerState::Up,
                restarts: 2,
                model: "qwen3:14b".into(),
                chunks: Some(9),
            },
            busy: false,
        };
        let v = serde_json::to_string(&s).unwrap();
        assert!(v.contains(r#""state":"up""#));
        assert_eq!(serde_json::from_str::<StatusResponse>(&v).unwrap(), s);
    }
}
