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
    /// Streaming only: the answer text is complete and its citations are being
    /// checked against the evidence. The streamed text is not yet trustworthy.
    Validating,
    Done,
}

impl Phase {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "retrieving" => Some(Self::Retrieving),
            "reading" => Some(Self::Reading),
            "answering" => Some(Self::Answering),
            "validating" => Some(Self::Validating),
            "done" => Some(Self::Done),
            _ => None,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Retrieving => "Searching...",
            Self::Reading => "Reading evidence...",
            Self::Answering => "Writing answer...",
            Self::Validating => "Validating citations...",
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
    /// Ask for `delta` events (the answer as it is generated, unvalidated)
    /// on the SSE stream. Absent or false: behaviour is unchanged.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub stream: bool,
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
    /// Optional feature flags, e.g. `"tasks.v1"`. Absent from the JSON when
    /// empty, so older clients see exactly the old document.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub capabilities: Vec<String>,
}

impl StatusResponse {
    pub fn has_capability(&self, name: &str) -> bool {
        self.capabilities.iter().any(|c| c == name)
    }
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

/// One decoded server-sent event of `POST /v1/ask`.
#[derive(Debug, Clone, PartialEq)]
pub enum AskEvent {
    Phase(Phase),
    /// A piece of the answer text. Unvalidated until `Result` arrives.
    Delta(String),
    /// Final, authoritative answer; replaces any streamed text.
    Result(Box<AskResult>),
    Error(String),
}

/// Decode an SSE event by name and data. `None` for unknown events and for
/// malformed data, so a newer daemon never breaks an older client.
pub fn parse_ask_event(event: &str, data: &str) -> Option<AskEvent> {
    match event {
        "phase" => {
            let v: serde_json::Value = serde_json::from_str(data).ok()?;
            Phase::parse(v.get("phase")?.as_str()?).map(AskEvent::Phase)
        }
        "delta" => {
            let v: serde_json::Value = serde_json::from_str(data).ok()?;
            Some(AskEvent::Delta(v.get("text")?.as_str()?.to_string()))
        }
        "result" => serde_json::from_str(data)
            .ok()
            .map(|r| AskEvent::Result(Box::new(r))),
        "error" => Some(AskEvent::Error(
            serde_json::from_str::<ApiError>(data)
                .map(|e| e.error.message)
                .unwrap_or_else(|_| "the question could not be answered".into()),
        )),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_ask_events() {
        assert_eq!(
            parse_ask_event("phase", r#"{"phase":"validating"}"#),
            Some(AskEvent::Phase(Phase::Validating))
        );
        assert_eq!(
            parse_ask_event("delta", r#"{"text":"2,6"}"#),
            Some(AskEvent::Delta("2,6".into()))
        );
        assert!(matches!(
            parse_ask_event("result", r#"{"answer":"a","abstained":false}"#),
            Some(AskEvent::Result(_))
        ));
        assert_eq!(
            parse_ask_event(
                "error",
                r#"{"error":{"code":"x","message":"boom","request_id":"r"}}"#
            ),
            Some(AskEvent::Error("boom".into()))
        );
        assert_eq!(
            parse_ask_event("error", "garbage"),
            Some(AskEvent::Error("the question could not be answered".into()))
        );
        // Unknown event names and malformed payloads are skipped, not fatal.
        assert_eq!(parse_ask_event("future", "{}"), None);
        assert_eq!(parse_ask_event("delta", r#"{"nope":1}"#), None);
        assert_eq!(parse_ask_event("phase", r#"{"phase":"???"}"#), None);
        assert_eq!(parse_ask_event("result", "{"), None);
    }

    #[test]
    fn stream_flag_is_omitted_unless_set() {
        let mut a = AskRequest {
            question: "q".into(),
            scope: None,
            stream: false,
        };
        assert_eq!(serde_json::to_value(&a).unwrap(), json!({"question":"q"}));
        a.stream = true;
        assert_eq!(
            serde_json::to_value(&a).unwrap(),
            json!({"question":"q","stream":true})
        );
        let old: AskRequest = serde_json::from_value(json!({"question":"q"})).unwrap();
        assert!(!old.stream);
    }

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
            stream: false,
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
            capabilities: vec![],
        };
        let v = serde_json::to_string(&s).unwrap();
        assert!(v.contains(r#""state":"up""#));
        assert!(!v.contains("capabilities"), "old shape when empty");
        assert_eq!(serde_json::from_str::<StatusResponse>(&v).unwrap(), s);
        let with = StatusResponse {
            capabilities: vec!["tasks.v1".into()],
            ..s
        };
        let v = serde_json::to_string(&with).unwrap();
        assert!(v.contains(r#""capabilities":["tasks.v1"]"#));
        assert!(with.has_capability("tasks.v1") && !with.has_capability("x"));
        assert_eq!(serde_json::from_str::<StatusResponse>(&v).unwrap(), with);
    }
}
