//! UI-independent logic: answer layout and state reduction. Unit-tested.

use docket_proto::api::{AskResult, Citation, Phase, SourceInfo, StatusResponse, WorkerState};
use docket_proto::task::{TaskEventBody, TaskState};

#[derive(Debug, PartialEq, Eq)]
pub enum Segment<'a> {
    Text(&'a str),
    /// Position in the numbering order returned alongside the segments.
    Cite(usize),
}

/// Split an answer around its citation labels. Returns the segments and the
/// citation indices in order of first appearance, followed by any citation
/// whose label never appears in the text.
pub fn layout<'a>(answer: &'a str, cites: &[Citation]) -> (Vec<Segment<'a>>, Vec<usize>) {
    let mut order: Vec<usize> = Vec::new();
    let mut segs = Vec::new();
    let mut rest = answer;
    loop {
        let mut best: Option<(usize, usize)> = None; // (byte pos, citation idx)
        for (i, c) in cites.iter().enumerate() {
            if c.label.is_empty() {
                continue;
            }
            if let Some(pos) = rest.find(&c.label) {
                let better = match best {
                    None => true,
                    Some((bp, bi)) => {
                        pos < bp || (pos == bp && c.label.len() > cites[bi].label.len())
                    }
                };
                if better {
                    best = Some((pos, i));
                }
            }
        }
        let Some((pos, idx)) = best else {
            if !rest.is_empty() {
                segs.push(Segment::Text(rest));
            }
            break;
        };
        if pos > 0 {
            segs.push(Segment::Text(&rest[..pos]));
        }
        let n = match order.iter().position(|&o| o == idx) {
            Some(n) => n,
            None => {
                order.push(idx);
                order.len() - 1
            }
        };
        segs.push(Segment::Cite(n));
        rest = &rest[pos + cites[idx].label.len()..];
    }
    for i in 0..cites.len() {
        if !order.contains(&i) {
            order.push(i);
        }
    }
    (segs, order)
}

#[derive(Debug, Clone, Default)]
pub struct Connection {
    pub service_up: bool,
    pub worker: Option<WorkerState>,
    pub model: String,
    pub chunks: Option<u64>,
    pub busy: bool,
    /// The daemon offers the task API (and with it, the Stop button).
    pub tasks_v1: bool,
    pub note: String,
}

impl Connection {
    pub fn apply_status(&mut self, s: Result<StatusResponse, String>) {
        match s {
            Ok(s) => {
                self.service_up = true;
                self.tasks_v1 = s.has_capability("tasks.v1");
                self.worker = Some(s.worker.state);
                self.model = s.worker.model;
                self.chunks = s.worker.chunks;
                self.busy = s.busy;
                self.note.clear();
            }
            Err(e) => {
                self.service_up = false;
                self.worker = None;
                self.busy = false;
                self.tasks_v1 = false;
                self.note = e;
            }
        }
    }
}

/// The in-flight question: its phase and the answer text streamed so far.
/// The text is NEVER trustworthy: citations are only checked after the last
/// delta, so it is shown as a draft and dropped when the final answer (or an
/// error) arrives.
#[derive(Debug, Clone, PartialEq)]
pub struct Pending {
    pub phase: Phase,
    pub text: String,
    /// Known once the daemon has accepted the question as a task.
    pub task_id: Option<String>,
    /// The user pressed Stop. The draft text is discarded and stays discarded.
    pub stop_requested: bool,
    stop_sent: bool,
}

impl Pending {
    pub fn new() -> Self {
        Self {
            phase: Phase::Retrieving,
            text: String::new(),
            task_id: None,
            stop_requested: false,
            stop_sent: false,
        }
    }

    /// The daemon named the task. Returns the id to cancel if Stop was
    /// pressed before it was known.
    pub fn set_task(&mut self, id: String) -> Option<String> {
        self.task_id = Some(id);
        self.take_cancel_target()
    }

    /// Stop pressed: discard the partial text now. Returns the task to cancel
    /// (once, and only when its id is known).
    pub fn request_stop(&mut self) -> Option<String> {
        self.stop_requested = true;
        self.text.clear();
        self.take_cancel_target()
    }

    /// The cancel request could not be sent: let the user try again.
    pub fn stop_failed(&mut self) {
        self.stop_requested = false;
        self.stop_sent = false;
    }

    fn take_cancel_target(&mut self) -> Option<String> {
        if self.stop_requested && !self.stop_sent {
            if let Some(id) = self.task_id.clone() {
                self.stop_sent = true;
                return Some(id);
            }
        }
        None
    }

    pub fn set_phase(&mut self, p: Phase) {
        self.phase = p;
    }

    pub fn push_delta(&mut self, piece: &str) {
        if !self.stop_requested {
            self.text.push_str(piece);
        }
    }

    /// Text is a draft until the daemon says citations were validated.
    pub fn is_draft(&self) -> bool {
        !self.text.is_empty()
    }

    pub fn is_validating(&self) -> bool {
        self.phase == Phase::Validating
    }
}

impl Default for Pending {
    fn default() -> Self {
        Self::new()
    }
}

/// Shown after a stopped question. It states what is known and what is not:
/// the daemon ended its own worker process, but cannot see the model engine.
pub const STOPPED_TITLE: &str = "Stopped. The partial answer was discarded.";
pub const STOPPED_NOTE: &str = "Docket ended its answering process. It cannot confirm that the language model itself stopped generating.";

pub enum Message {
    Cancelled,
    User(String),
    Answer { result: AskResult, wall_s: f64 },
    Error(String),
}

/// What one task event means for the question on screen.
#[derive(Debug, Clone, PartialEq)]
pub enum TaskStep {
    Phase(Phase),
    Delta(String),
    Answer(Box<AskResult>),
    Failed(String),
    Cancelled,
    /// Nothing for the UI (state changes, the cancel facts).
    Skip,
}

/// Reduce a task event. `last_error` remembers the message of the latest
/// `error` event so a following `failed` or `timed_out` state can show it.
pub fn task_step(body: &TaskEventBody, last_error: &mut Option<String>) -> TaskStep {
    match body {
        TaskEventBody::Phase { phase } => TaskStep::Phase(*phase),
        TaskEventBody::Delta { text } => TaskStep::Delta(text.clone()),
        TaskEventBody::Result { result } => TaskStep::Answer(result.clone()),
        TaskEventBody::Error { message, .. } => {
            *last_error = Some(message.clone());
            TaskStep::Skip
        }
        TaskEventBody::State { state } => match state {
            TaskState::Cancelled => TaskStep::Cancelled,
            TaskState::Failed | TaskState::TimedOut => TaskStep::Failed(
                last_error
                    .take()
                    .unwrap_or_else(|| "the question could not be answered".into()),
            ),
            _ => TaskStep::Skip,
        },
        TaskEventBody::CancelRequested { .. }
        | TaskEventBody::WorkerTerminated { .. }
        | TaskEventBody::InferenceStopped { .. } => TaskStep::Skip,
    }
}

pub fn status_label(status: &str) -> &'static str {
    match status {
        "active" => "Ready",
        "missing" => "Folder missing",
        "revoked" => "Disconnected",
        _ => "Unavailable",
    }
}

pub fn source_ready(s: &SourceInfo) -> bool {
    s.status == "active"
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(label: &str) -> Citation {
        Citation {
            label: label.into(),
            source_display_name: "x".into(),
            chunk_id: "id".into(),
            excerpt: String::new(),
        }
    }

    #[test]
    fn pending_accumulates_deltas_and_tracks_validation() {
        let mut p = Pending::new();
        assert!(!p.is_draft() && !p.is_validating());
        p.set_phase(Phase::Answering);
        p.push_delta("The rate ");
        p.push_delta("is 2,650 [a#1].");
        assert_eq!(p.text, "The rate is 2,650 [a#1].");
        assert!(p.is_draft() && !p.is_validating());
        p.set_phase(Phase::Validating);
        assert!(p.is_validating());
    }

    #[test]
    fn stop_discards_the_draft_and_cancels_once() {
        let mut p = Pending::new();
        p.push_delta("partial ");
        // Stop before the task id is known: cancel is deferred until it is.
        assert_eq!(p.request_stop(), None);
        assert!(p.stop_requested && p.text.is_empty() && !p.is_draft());
        p.push_delta("late text");
        assert!(p.text.is_empty(), "discarded text stays discarded");
        assert_eq!(p.set_task("t-1".into()), Some("t-1".into()));
        assert_eq!(p.request_stop(), None, "only one cancel is sent");
        // Stop with a known id cancels right away; a failed send can be retried.
        let mut q = Pending::new();
        q.set_task("t-2".into());
        assert_eq!(q.request_stop(), Some("t-2".into()));
        q.stop_failed();
        assert!(!q.stop_requested);
        assert_eq!(q.request_stop(), Some("t-2".into()));
    }

    #[test]
    fn task_events_reduce_to_ui_steps() {
        use docket_proto::task::{CancelSource, InferenceStatus, WorkerExit};
        let mut err = None;
        let st = |s| TaskEventBody::State { state: s };
        assert_eq!(
            task_step(&TaskEventBody::Delta { text: "a".into() }, &mut err),
            TaskStep::Delta("a".into())
        );
        for b in [
            TaskEventBody::CancelRequested {
                by: CancelSource::User,
            },
            TaskEventBody::WorkerTerminated {
                how: WorkerExit::Killed,
            },
            TaskEventBody::InferenceStopped {
                status: InferenceStatus::Unverified,
            },
            st(TaskState::Cancelling),
            st(TaskState::Running),
        ] {
            assert_eq!(task_step(&b, &mut err), TaskStep::Skip);
        }
        assert_eq!(
            task_step(&st(TaskState::Cancelled), &mut err),
            TaskStep::Cancelled
        );
        task_step(
            &TaskEventBody::Error {
                code: "timeout".into(),
                message: "too slow".into(),
            },
            &mut err,
        );
        assert_eq!(
            task_step(&st(TaskState::TimedOut), &mut err),
            TaskStep::Failed("too slow".into())
        );
        assert!(matches!(
            task_step(&st(TaskState::Failed), &mut err),
            TaskStep::Failed(m) if m.contains("could not")
        ));
    }

    #[test]
    fn stopped_wording_never_claims_the_model_stopped() {
        assert!(STOPPED_NOTE.contains("cannot confirm"));
        assert!(!STOPPED_TITLE.contains("model stopped"));
    }

    #[test]
    fn multibyte_deltas_concatenate_losslessly() {
        let mut p = Pending::new();
        for piece in ["EUR ", "2\u{202f}650", " \u{2014} ok"] {
            p.push_delta(piece);
        }
        assert_eq!(p.text, "EUR 2\u{202f}650 \u{2014} ok");
    }

    #[test]
    fn numbers_by_first_appearance_and_keeps_unreferenced() {
        let cites = [c("[b#2]"), c("[a#1]"), c("[z#9]")];
        let (segs, order) = layout("Rate [a#1] and [b#2] then [a#1].", &cites);
        assert_eq!(
            segs,
            vec![
                Segment::Text("Rate "),
                Segment::Cite(0),
                Segment::Text(" and "),
                Segment::Cite(1),
                Segment::Text(" then "),
                Segment::Cite(0),
                Segment::Text("."),
            ]
        );
        assert_eq!(order, vec![1, 0, 2]);
    }

    #[test]
    fn longest_label_wins_at_same_position() {
        let cites = [c("[a#1]"), c("[a#1]x")];
        let (segs, _) = layout("[a#1]x", &cites);
        assert_eq!(segs, vec![Segment::Cite(0)]);
    }

    #[test]
    fn no_citations_is_plain_text() {
        let (segs, order) = layout("I don't know.", &[]);
        assert_eq!(segs, vec![Segment::Text("I don't know.")]);
        assert!(order.is_empty());
    }
}
