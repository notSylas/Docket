//! UI-independent logic: answer layout and state reduction. Unit-tested.

use docket_proto::api::{AskResult, Citation, SourceInfo, StatusResponse, WorkerState};

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
    pub note: String,
}

impl Connection {
    pub fn apply_status(&mut self, s: Result<StatusResponse, String>) {
        match s {
            Ok(s) => {
                self.service_up = true;
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
                self.note = e;
            }
        }
    }
}

pub enum Message {
    User(String),
    Answer { result: AskResult, wall_s: f64 },
    Error(String),
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
