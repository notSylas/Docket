//! Pure view model for the status UI (R2a-3). No egui, no I/O, no clocks:
//! every function takes `now_ms` (any monotonic millisecond clock) so the
//! rules are unit-tested headlessly.
//!
//! Rules this module enforces (and the tests pin):
//! * Process health, worker responsiveness, model availability, source
//!   readiness and indexing readiness are separate chips; nothing collapses to
//!   one constant "ok".
//! * Every section shows its age; stale (or age-less) data is dimmed and
//!   labelled "as of N s ago", never presented as current. A failed status
//!   poll keeps the last good snapshot, forced stale.
//! * Unknown is "unknown", never 0 and never healthy.
//! * Only closed codes are rendered, through fixed label tables; anything not
//!   in a table renders generically ("unrecognised"). No paths, file names,
//!   document text or error messages ever reach a string built here.

use docket_proto::api::{AskResult, SourceInfo, StatusResponse, WorkerState};
use docket_proto::status::{
    IndexingData, ModelState, ModelsData, Readiness, Responsiveness, Section, SectionState,
    SourceCounts, Tri, VersionCounts,
};

/// No successful status poll for this long: the service is shown unavailable
/// even if no poll has failed outright.
pub const STATUS_STALE_LOCAL_MS: u64 = 10_000;
/// A source list older than this is labelled with its age.
pub const SOURCES_STALE_MS: u64 = 30_000;

const UNRECOGNISED: &str = "unrecognised";

// ---------------------------------------------------------------------------
// label tables (closed codes -> short plain language)
// ---------------------------------------------------------------------------

fn lookup(table: &[(&'static str, &'static str)], code: &str) -> &'static str {
    table
        .iter()
        .find(|(c, _)| *c == code)
        .map(|(_, l)| *l)
        .unwrap_or(UNRECOGNISED)
}

const DB_CODES: &[(&str, &str)] = &[
    ("ok", "database fine"),
    ("db_missing", "database file missing"),
    ("db_busy", "database busy"),
    ("db_unreadable", "database unreadable"),
    ("schema_behind", "database needs an update"),
    ("schema_unknown", "database version unknown"),
];
const MODEL_CODES: &[(&str, &str)] = &[
    ("ok", "models fine"),
    ("probe_failed", "model check failed"),
    ("model_missing", "a model is not installed"),
    ("ollama_unreachable", "model server not reachable"),
    ("ollama_not_local", "model server is not local"),
    ("ollama_bad_response", "model server gave an unexpected reply"),
];
const INDEX_CODES: &[(&str, &str)] = &[
    ("ok", "index fine"),
    ("queue_dead_letters", "some files could not be indexed"),
    ("anomalies", "index has inconsistencies"),
    ("index_unreadable", "index unreadable"),
    ("db_missing", "database file missing"),
];
const REFRESH_CODES: &[(&str, &str)] = &[
    ("health_timeout", "health check timed out"),
    ("worker_timeout", "worker timed out"),
    ("worker_crashed", "worker crashed"),
    ("worker_unavailable", "worker unavailable"),
    ("worker_error", "worker reported an error"),
    ("bad_worker_response", "worker reply unreadable"),
    ("cancelled", "cancelled"),
    ("spawn_failed", "worker could not be started"),
];
const SKIP_CODES: &[(&str, &str)] = &[
    ("unsupported_type", "unsupported file type"),
    ("unsupported_other", "other file types Docket does not read"),
    ("converter_missing", "office converter not available"),
    ("conversion_failed", "conversion failed"),
    ("conversion_timeout", "conversion timed out"),
    ("too_large", "file too large"),
    ("password_protected", "password-protected"),
    ("invalid_file", "contents do not match the extension"),
    ("ignored", "ignored by a rule"),
    ("read_failed", "could not be read"),
    ("unknown_reason", "reason not recorded"),
];
const FAILURE_CODES: &[(&str, &str)] = &[
    ("password_protected", "password-protected"),
    ("conversion_failed", "conversion failed"),
    ("conversion_timeout", "conversion timed out"),
    ("converter_missing", "office converter not available"),
    ("too_large", "file too large"),
    ("unsupported_type", "unsupported file type"),
    ("invalid_file", "contents do not match the extension"),
    ("corrupt_file", "corrupt file"),
    ("timeout", "timed out"),
    ("out_of_memory", "ran out of memory"),
    ("permission_denied", "permission denied"),
    ("file_missing", "file missing"),
    ("empty_file", "empty file"),
    ("read_failed", "could not be read"),
    ("other", "other failure"),
];
const ANOMALY_CODES: &[(&str, &str)] = &[
    ("empty_ready", "marked ready but has no searchable passages"),
    ("interrupted_job", "an ingest job was interrupted"),
    ("dead_letters", "some files are dead-lettered"),
    ("failed_versions", "some files failed"),
    ("job_partial", "the last ingest was partial"),
    ("keyword_only_idle", "some files are keyword-only"),
];
const WARNING_CODES: &[(&str, &str)] = &[
    ("evidence_unavailable", "cited evidence is no longer available"),
    ("no_citations", "the answer has no citations"),
    ("empty_answer", "the answer is empty"),
    (
        "named_file_outside_scope",
        "the named file is outside the selected scope",
    ),
    ("unknown_citation", "the answer cites an unknown passage"),
    ("named_file_empty", "the named file has no searchable passages"),
    (
        "keyword_only_evidence",
        "the answer used keyword-only passages",
    ),
];
const ABSTENTION: &[(&str, &str)] = &[
    ("NO_EVIDENCE", "no evidence found"),
    ("INSUFFICIENT_SUPPORT", "evidence did not support an answer"),
    ("SOURCE_UNAVAILABLE", "a source was unavailable"),
    ("PERMISSION_CHANGED", "a source's permission changed"),
    ("UNRESOLVED_CONFLICT", "sources conflict"),
    ("INVALID_DERIVATION", "a calculation could not be verified"),
    ("VERIFIER_UNAVAILABLE", "the verifier was unavailable"),
    ("CONTEXT_BUDGET_EXCEEDED", "too much evidence for the model"),
];

pub fn skip_label(code: &str) -> &'static str {
    lookup(SKIP_CODES, code)
}
pub fn failure_label(code: &str) -> &'static str {
    lookup(FAILURE_CODES, code)
}
pub fn anomaly_label(code: &str) -> &'static str {
    lookup(ANOMALY_CODES, code)
}
pub fn warning_label(code: &str) -> &'static str {
    lookup(WARNING_CODES, code)
}
pub fn refresh_label(code: &str) -> &'static str {
    lookup(REFRESH_CODES, code)
}

/// "5 s ago" / "3 min ago" / "2 h ago".
pub fn fmt_age(ms: u64) -> String {
    let s = ms / 1000;
    if s < 90 {
        format!("{s} s ago")
    } else if s < 5400 {
        format!("{} min ago", s / 60)
    } else {
        format!("{} h ago", s / 3600)
    }
}

fn fmt_dur(s: u64) -> String {
    if s < 90 {
        format!("{s} s")
    } else if s < 5400 {
        format!("{} min", s / 60)
    } else {
        format!("{} h", s / 3600)
    }
}

fn tri(t: Tri) -> &'static str {
    match t {
        Tri::Yes => "yes",
        Tri::No => "no",
        Tri::Unknown => "unknown",
    }
}

/// A model name is a configuration id; show it only when it is plainly one.
fn plain_name(n: &str) -> Option<&str> {
    let ok = !n.is_empty()
        && n.len() <= 60
        && n.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b));
    ok.then_some(n)
}

fn plain_token(n: &str) -> Option<&str> {
    let ok = !n.is_empty()
        && n.len() <= 40
        && n.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b));
    ok.then_some(n)
}

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Good,
    /// Informational, not a problem (e.g. model not loaded right now).
    Info,
    Warn,
    Bad,
    Unknown,
}

/// Why a status poll produced no document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetchError {
    /// Could not connect / read the socket.
    Unreachable,
    /// The service answered with an HTTP error.
    Http(u16),
    /// The service answered but the document could not be understood.
    Protocol,
}

impl std::fmt::Display for FetchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FetchError::Unreachable => f.write_str("the service is not reachable"),
            FetchError::Http(c) => write!(f, "the service reported a problem (HTTP {c})"),
            FetchError::Protocol => f.write_str("the service sent an unreadable reply"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overall {
    /// Before the first status.
    Loading,
    /// Connected, every section present, fresh and fine.
    Healthy,
    /// Connected, but some section has not been reported (yet) or is unknown.
    Checking,
    /// Any section degraded / unavailable / stale, or the worker is not up.
    Degraded,
    /// The service cannot be reached (last good state kept, with age).
    Unavailable,
    /// The service answered with a typed failure.
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChipKind {
    Service,
    Worker,
    Model,
    Index,
    Database,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chip {
    pub kind: ChipKind,
    pub name: &'static str,
    /// Short value, e.g. "up", "indexing 3 of 10 (ETA ~30 s)".
    pub value: String,
    pub tone: Tone,
    /// Old data: drawn dimmed. `age` then says how old.
    pub stale: bool,
    pub age: Option<String>,
    pub tooltip: Vec<String>,
}

impl Chip {
    /// Full text as drawn: `Name: value` plus the age when not current.
    pub fn display(&self) -> String {
        let mut s = format!("{}: {}", self.name, self.value);
        if self.stale {
            match &self.age {
                Some(a) => s.push_str(&format!(" (as of {a})")),
                None => s.push_str(" (age unknown)"),
            }
        }
        s
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Banner {
    pub tone: Tone,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AskGate {
    Enabled,
    /// Asking is off; the text says why.
    Disabled(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StatusView {
    pub overall: Overall,
    /// The daemon advertises `status.v2` and sent health: draw the new bar.
    /// `false` = today's plain status line.
    pub v2: bool,
    pub chips: Vec<Chip>,
    pub banners: Vec<Banner>,
    pub ask: AskGate,
}

// ---------------------------------------------------------------------------
// the model: last-good snapshot + latest poll outcome
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
pub struct StatusModel {
    last_good: Option<StatusResponse>,
    received_ms: u64,
    error: Option<FetchError>,
}

impl StatusModel {
    pub fn apply(&mut self, r: Result<StatusResponse, FetchError>, now_ms: u64) {
        match r {
            Ok(s) => {
                self.last_good = Some(s);
                self.received_ms = now_ms;
                self.error = None;
            }
            // Never blank on an error: keep the last good snapshot.
            Err(e) => self.error = Some(e),
        }
    }

    pub fn view(&self, now_ms: u64, question_running: bool) -> StatusView {
        let Some(st) = &self.last_good else {
            return match &self.error {
                None => StatusView {
                    overall: Overall::Loading,
                    v2: false,
                    chips: vec![],
                    banners: vec![],
                    ask: AskGate::Disabled("Connecting to the Docket service...".into()),
                },
                Some(e) => {
                    let (overall, text) = failure_text(e, None);
                    StatusView {
                        overall,
                        v2: false,
                        chips: vec![],
                        banners: vec![Banner {
                            tone: Tone::Bad,
                            text: text.clone(),
                        }],
                        ask: AskGate::Disabled(format!("Asking is off: {text}.")),
                    }
                }
            };
        };
        let since = now_ms.saturating_sub(self.received_ms);
        let silent = self.error.is_none() && since > STATUS_STALE_LOCAL_MS;
        let v2 = st.health.is_some() && st.has_capability("status.v2");
        let down_err = self.error.clone().or(silent.then_some(FetchError::Unreachable));

        if !v2 {
            // Old daemon: today's behaviour, nothing new.
            let (overall, ask) = match &down_err {
                Some(e) => {
                    let (o, t) = failure_text(e, Some(since));
                    (o, AskGate::Disabled(format!("Asking is off: {t}.")))
                }
                None => (Overall::Healthy, AskGate::Enabled),
            };
            return StatusView {
                overall,
                v2: false,
                chips: vec![],
                banners: vec![],
                ask,
            };
        }
        let Some(h) = st.health.as_deref() else {
            unreachable!("v2 implies health");
        };
        // Everything in the snapshot is `since` older than when it was built.
        let forced_stale = down_err.is_some();
        let mut chips = Vec::new();
        let mut banners = Vec::new();

        // Service
        let svc = match &down_err {
            None => Chip {
                kind: ChipKind::Service,
                name: "Service",
                value: "connected".into(),
                tone: Tone::Good,
                stale: false,
                age: None,
                tooltip: vec![
                    format!("Last status update {}", fmt_age(since)),
                    match plain_token(&st.version) {
                        Some(v) => format!("Version {v}"),
                        None => "Version unknown".into(),
                    },
                ],
            },
            Some(e) => {
                let (_, text) = failure_text(e, Some(since));
                let value = match e {
                    FetchError::Unreachable => "not reachable",
                    FetchError::Http(_) => "reporting a problem",
                    FetchError::Protocol => "unreadable reply",
                };
                Chip {
                    kind: ChipKind::Service,
                    name: "Service",
                    value: value.into(),
                    tone: Tone::Bad,
                    stale: false,
                    age: None,
                    tooltip: vec![text],
                }
            }
        };
        chips.push(svc);

        // Worker (daemon-computed at response time: fresh as of the poll)
        let wp = &h.worker_process;
        let (wvalue, wtone) = match wp.state {
            WorkerState::Starting if wp.restarts > 0 || wp.crashes > 0 => {
                ("restarting".to_string(), Tone::Warn)
            }
            WorkerState::Starting => ("starting".into(), Tone::Warn),
            WorkerState::Down => ("down".into(), Tone::Bad),
            WorkerState::Up if st.busy || wp.responsiveness == Responsiveness::Busy => {
                ("busy (answering)".into(), Tone::Info)
            }
            WorkerState::Up => match wp.responsiveness {
                Responsiveness::Responsive => ("up".into(), Tone::Good),
                Responsiveness::Unresponsive => ("up, not responding".into(), Tone::Bad),
                _ => ("up, responsiveness unknown".into(), Tone::Unknown),
            },
        };
        let mut wtip = vec![
            format!("Relaunches {} · failures {}", wp.restarts, wp.crashes),
            format!("In this state for {}", fmt_dur((wp.since_ms + since) / 1000)),
        ];
        wtip.push(match wp.last_ok_age_ms {
            Some(a) => format!("Last reply {}", fmt_age(a + since)),
            None => "Last reply: unknown".into(),
        });
        wtip.push(match &wp.last_error_code {
            Some(c) => format!("Last error: {}", refresh_label(c)),
            None => "Last error: none".into(),
        });
        wtip.push("Responsiveness is judged from the worker's recent replies.".into());
        chips.push(Chip {
            kind: ChipKind::Worker,
            name: "Worker",
            value: wvalue,
            tone: wtone,
            stale: forced_stale,
            age: forced_stale.then(|| fmt_age(since)),
            tooltip: wtip,
        });

        // Sections
        let ctx = Fresh {
            since,
            forced: forced_stale,
            stale_after: h.stale_after_ms,
            refresh_failed: h.refresh_error_code.is_some(),
        };
        chips.push(model_chip(h.sections.models.as_ref(), &ctx));
        chips.push(index_chip(h.sections.indexing.as_ref(), &ctx));
        chips.push(db_chip(h.sections.database.as_ref(), &ctx));

        // Banners
        if let Some(e) = &down_err {
            let (_, text) = failure_text(e, Some(since));
            banners.push(Banner {
                tone: Tone::Bad,
                text: format!(
                    "{}. Showing the last known state from {}.",
                    capitalise(&text),
                    fmt_age(since)
                ),
            });
        } else {
            match wp.state {
                WorkerState::Starting => banners.push(Banner {
                    tone: Tone::Warn,
                    text: "The answering process is starting. Asking will be available shortly."
                        .into(),
                }),
                WorkerState::Down => banners.push(Banner {
                    tone: Tone::Bad,
                    text: match &wp.last_error_code {
                        Some(c) => format!(
                            "The answering process is not running (last error: {}). Docket retries automatically.",
                            refresh_label(c)
                        ),
                        None => "The answering process is not running. Docket retries automatically.".into(),
                    },
                }),
                WorkerState::Up => {}
            }
        }
        let stale_names: Vec<&str> = chips
            .iter()
            .filter(|c| c.kind != ChipKind::Service && c.kind != ChipKind::Worker && c.stale)
            .map(|c| c.name)
            .collect();
        if !stale_names.is_empty() && down_err.is_none() {
            let why = match &h.refresh_error_code {
                Some(c) => format!(" (latest refresh failed: {})", refresh_label(c)),
                None => String::new(),
            };
            banners.push(Banner {
                tone: Tone::Warn,
                text: format!(
                    "Out of date: {}. Showing the last known values dimmed{why}.",
                    stale_names.join(", ")
                ),
            });
        }
        for c in &chips {
            if matches!(c.kind, ChipKind::Model | ChipKind::Database | ChipKind::Index)
                && !c.stale
                && matches!(c.tone, Tone::Warn | Tone::Bad)
            {
                banners.push(Banner {
                    tone: c.tone,
                    text: format!("{}: {}.", c.name, c.value),
                });
            }
        }
        if let Some(i) = h.sections.indexing.as_ref() {
            if let Some(v) = i.data.versions.as_ref().filter(|v| v.known) {
                if v.empty_ready.unwrap_or(0) > 0 && !chips[3].stale {
                    banners.push(Banner {
                        tone: Tone::Warn,
                        text: format!(
                            "{} files are marked ready but have no searchable passages.",
                            v.empty_ready.unwrap_or(0)
                        ),
                    });
                }
            }
        }

        // Overall
        let worst = chips.iter().map(|c| (c.tone, c.stale));
        let mut degraded = false;
        let mut unknown = false;
        for (t, stale) in worst {
            match t {
                Tone::Warn | Tone::Bad => degraded = true,
                Tone::Unknown => unknown = true,
                _ => {}
            }
            if stale {
                degraded = true;
            }
        }
        // Busy is a normal state, not degradation.
        let overall = match &down_err {
            Some(FetchError::Http(_) | FetchError::Protocol) => Overall::Failed,
            Some(FetchError::Unreachable) => Overall::Unavailable,
            None if degraded => Overall::Degraded,
            None if unknown => Overall::Checking,
            None => Overall::Healthy,
        };

        // Ask gate
        let ask = if let Some(e) = &down_err {
            let (_, t) = failure_text(e, Some(since));
            AskGate::Disabled(format!(
                "Asking is off: {t} (last contact {}).",
                fmt_age(since)
            ))
        } else if question_running {
            AskGate::Disabled("A question is running. Asking is off until it finishes.".into())
        } else {
            match wp.state {
                WorkerState::Starting => AskGate::Disabled(
                    "Asking is off: the answering process is starting. It will be available shortly."
                        .into(),
                ),
                WorkerState::Down => AskGate::Disabled(
                    "Asking is off: the answering process is not running. Docket is retrying.".into(),
                ),
                WorkerState::Up if st.busy => AskGate::Disabled(
                    "Asking is off: another question is running. Try again when it finishes.".into(),
                ),
                WorkerState::Up => AskGate::Enabled,
            }
        };
        StatusView {
            overall,
            v2: true,
            chips,
            banners,
            ask,
        }
    }
}

fn capitalise(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

fn failure_text(e: &FetchError, since: Option<u64>) -> (Overall, String) {
    let o = match e {
        FetchError::Unreachable => Overall::Unavailable,
        _ => Overall::Failed,
    };
    let _ = since;
    (o, e.to_string())
}

struct Fresh {
    since: u64,
    forced: bool,
    stale_after: u64,
    refresh_failed: bool,
}

/// Age of a section as of now, and whether it must be treated as stale.
fn freshness<T>(s: &Section<T>, c: &Fresh) -> (Option<u64>, bool) {
    let age = s.age_ms.map(|a| a + c.since);
    let stale = c.forced
        || s.stale != Some(false)
        || c.refresh_failed
        || age.is_none_or(|a| a > c.stale_after.max(1) + 2_000);
    (age, stale)
}

fn missing_chip(kind: ChipKind, name: &'static str) -> Chip {
    Chip {
        kind,
        name,
        value: "unknown".into(),
        tone: Tone::Unknown,
        stale: false,
        age: None,
        tooltip: vec!["Not reported yet.".into()],
    }
}

fn section_tone(s: SectionState) -> Tone {
    match s {
        SectionState::Ok => Tone::Good,
        SectionState::Degraded => Tone::Warn,
        SectionState::Unavailable => Tone::Bad,
        SectionState::Unknown => Tone::Unknown,
    }
}

fn fresh_tip(age: Option<u64>, stale: bool) -> String {
    match (age, stale) {
        (Some(a), false) => format!("Checked {}", fmt_age(a)),
        (Some(a), true) => format!("Stale: last checked {}", fmt_age(a)),
        (None, _) => "Age unknown: treated as stale".into(),
    }
}

fn model_part(label: &str, m: &ModelState, ollama: Tri) -> (String, Tone, Vec<String>) {
    let mut tip = vec![format!(
        "{label}{}: installed {} · reachable {} · resident (loaded now) {}",
        plain_name(&m.name)
            .map(|n| format!(" ({n})"))
            .unwrap_or_default(),
        tri(m.installed),
        tri(m.reachable),
        tri(m.resident)
    )];
    if m.resident == Tri::No {
        tip.push(format!(
            "The {label} model is not loaded right now; it loads on first use."
        ));
    }
    let (v, t) = if m.installed == Tri::No {
        ("not installed", Tone::Bad)
    } else if m.reachable == Tri::No || ollama == Tri::No {
        ("not reachable", Tone::Bad)
    } else if m.installed == Tri::Yes && m.reachable == Tri::Yes {
        if m.resident == Tri::No {
            ("installed, not loaded", Tone::Info)
        } else if m.resident == Tri::Yes {
            ("loaded", Tone::Good)
        } else {
            ("installed, load state unknown", Tone::Good)
        }
    } else {
        ("unknown", Tone::Unknown)
    };
    (format!("{label} {v}"), t, tip)
}

fn tone_rank(t: Tone) -> u8 {
    match t {
        Tone::Bad => 4,
        Tone::Warn => 3,
        Tone::Unknown => 2,
        Tone::Info => 1,
        Tone::Good => 0,
    }
}

fn model_chip(s: Option<&Section<ModelsData>>, c: &Fresh) -> Chip {
    let Some(s) = s else {
        return missing_chip(ChipKind::Model, "Model");
    };
    let (age, stale) = freshness(s, c);
    let (av, at, mut tip) = model_part("answer", &s.data.answer, s.data.ollama);
    let (ev, et, tip2) = model_part("embed", &s.data.embed, s.data.ollama);
    tip.extend(tip2);
    tip.insert(0, format!("Model server answered: {}", tri(s.data.ollama)));
    tip.push(format!(
        "Section: {}",
        match s.state {
            SectionState::Ok => "ok",
            SectionState::Degraded => "degraded",
            SectionState::Unavailable => "unavailable",
            SectionState::Unknown => "unknown",
        }
    ));
    if s.state != SectionState::Ok {
        tip.push(format!("Reason: {}", lookup(MODEL_CODES, &s.code)));
    }
    tip.push(fresh_tip(age, stale));
    let mut tone = if tone_rank(at) >= tone_rank(et) { at } else { et };
    let st = section_tone(s.state);
    if tone_rank(st) > tone_rank(tone) && st != Tone::Good {
        tone = st;
    }
    // Answer and embed each get a say; show both.
    let value = format!("{} · {}", av, ev);
    Chip {
        kind: ChipKind::Model,
        name: "Model",
        value,
        tone,
        stale,
        age: age.map(fmt_age),
        tooltip: tip,
    }
}

fn db_chip(s: Option<&Section<crate::status_view::Db>>, c: &Fresh) -> Chip {
    let Some(s) = s else {
        return missing_chip(ChipKind::Database, "Database");
    };
    let (age, stale) = freshness(s, c);
    let (value, mut tone) = match (s.data.readable, s.data.at_head) {
        (Tri::No, _) => ("unreadable", Tone::Bad),
        (Tri::Unknown, _) => ("unknown", Tone::Unknown),
        (Tri::Yes, Tri::No) => ("readable, needs update", Tone::Warn),
        (Tri::Yes, Tri::Unknown) => ("readable, version unknown", Tone::Unknown),
        (Tri::Yes, Tri::Yes) => ("readable", Tone::Good),
    };
    if matches!(s.state, SectionState::Degraded | SectionState::Unavailable)
        && tone_rank(section_tone(s.state)) > tone_rank(tone)
    {
        tone = section_tone(s.state);
    }
    let mut tip = vec![
        format!(
            "Readable {} · up to date {}",
            tri(s.data.readable),
            tri(s.data.at_head)
        ),
        match s.data.revision.as_deref().and_then(plain_token) {
            Some(r) => format!("Schema revision {r}"),
            None => "Schema revision unknown".into(),
        },
    ];
    if s.state != SectionState::Ok {
        tip.push(format!("Reason: {}", lookup(DB_CODES, &s.code)));
    }
    tip.push(fresh_tip(age, stale));
    Chip {
        kind: ChipKind::Database,
        name: "Database",
        value: value.into(),
        tone,
        stale,
        age: age.map(fmt_age),
        tooltip: tip,
    }
}

fn unk(v: Option<u64>) -> String {
    v.map(|n| n.to_string()).unwrap_or_else(|| "unknown".into())
}

fn index_chip(s: Option<&Section<IndexingData>>, c: &Fresh) -> Chip {
    let Some(s) = s else {
        return missing_chip(ChipKind::Index, "Index");
    };
    let (age, stale) = freshness(s, c);
    let d = &s.data;
    let mut tip = Vec::new();
    let v = d.versions.as_ref().filter(|v| v.known);
    let q = d.queue.as_ref().filter(|q| q.known);
    let queued = q.map(|q| q.queued.unwrap_or(0) + q.running.unwrap_or(0));
    let pending = v.and_then(|v| v.pending);
    let writer_held = d.writer.as_deref() == Some("held");
    let interrupted = d.interrupted_jobs.unwrap_or(0) > 0;
    let dead = d.dead_letters.or(q.and_then(|q| q.dead));
    let (mut value, mut tone);
    if s.state == SectionState::Unavailable {
        value = "unreadable".to_string();
        tone = Tone::Bad;
    } else if v.is_none() {
        value = "unknown".to_string();
        tone = Tone::Unknown;
    } else {
        let v = v.expect("checked");
        let busy = pending.unwrap_or(0) > 0 || queued.unwrap_or(0) > 0 || writer_held;
        if busy {
            let done = match (v.ready, v.keyword_only) {
                (Some(r), Some(k)) => Some(r + k),
                _ => None,
            };
            let eta = d
                .eta_s
                .as_ref()
                .and_then(|e| e.tier1.or(e.tier2))
                .map(|e| format!("ETA ~{}", fmt_dur(e)))
                .unwrap_or_else(|| "ETA unknown".into());
            value = match (done, v.total) {
                (Some(n), Some(m)) => format!("indexing {n} of {m} ({eta})"),
                _ => format!("indexing, progress unknown ({eta})"),
            };
            tone = Tone::Info;
        } else if v.failed.unwrap_or(0) > 0 && v.ready.unwrap_or(0) + v.keyword_only.unwrap_or(0) == 0
        {
            value = format!("failed ({} files)", v.failed.unwrap_or(0));
            tone = Tone::Bad;
        } else {
            value = "idle".into();
            tone = Tone::Good;
        }
    }
    if v.is_some_and(|v| v.empty_ready.unwrap_or(0) > 0) {
        value.push_str(" · inconsistent");
        tone = tone_max(tone, Tone::Warn);
    }
    if interrupted {
        value.push_str(" · interrupted");
        tone = tone_max(tone, Tone::Warn);
    }
    if let Some(n) = dead.filter(|n| *n > 0) {
        value.push_str(&format!(" · {n} dead letters"));
        tone = tone_max(tone, Tone::Warn);
    }
    if s.state == SectionState::Degraded {
        tone = tone_max(tone, Tone::Warn);
    }
    match v {
        Some(v) => tip.push(version_basis_line(v)),
        None => tip.push("Counts unknown".into()),
    }
    tip.push(format!(
        "Queued/running: {} · dead letters: {} · interrupted jobs: {}",
        unk(queued),
        unk(dead),
        unk(d.interrupted_jobs)
    ));
    tip.push(match d.writer.as_deref() {
        Some("held") => "Indexer is working now".into(),
        Some("free") => "Indexer is idle".into(),
        _ => "Indexer state unknown".into(),
    });
    if let Some(h) = d.history.as_ref().filter(|h| h.known) {
        tip.push(format!(
            "Superseded versions kept (history, separate): {}",
            unk(h.superseded)
        ));
    }
    if s.state != SectionState::Ok {
        tip.push(format!("Reason: {}", lookup(INDEX_CODES, &s.code)));
    }
    tip.push(fresh_tip(age, stale));
    Chip {
        kind: ChipKind::Index,
        name: "Index",
        value,
        tone,
        stale,
        age: age.map(fmt_age),
        tooltip: tip,
    }
}

fn tone_max(a: Tone, b: Tone) -> Tone {
    if tone_rank(b) > tone_rank(a) {
        b
    } else {
        a
    }
}

fn version_basis_line(v: &VersionCounts) -> String {
    if v.reconciles {
        "Counts reconcile (same snapshot)".into()
    } else {
        "Counts come from one snapshot but are not claimed to add up".into()
    }
}

// Alias so `db_chip`'s signature stays readable.
pub type Db = docket_proto::status::DatabaseData;

// ---------------------------------------------------------------------------
// per-source view
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceView {
    pub readiness: String,
    pub tone: Tone,
    pub anomalies: Vec<String>,
    /// `ready N · keyword-only N · pending N · skipped N · failed N · empty N`.
    pub counts_line: Option<String>,
    /// Superseded versions: history, shown apart from the active equation.
    pub superseded: Option<String>,
    pub skipped: Vec<(String, u64)>,
    pub failed: Vec<(String, u64)>,
    pub tooltip: Vec<String>,
    /// Old-daemon detail (`N files`) when no counts exist.
    pub legacy_detail: Option<String>,
}

pub fn status_label(status: &str) -> &'static str {
    match status {
        "active" => "Ready",
        "missing" => "Folder missing",
        "revoked" => "Disconnected",
        "tombstoned" => "Removed",
        _ => "Unavailable",
    }
}

fn sum(m: &std::collections::BTreeMap<String, u64>) -> u64 {
    m.values().sum()
}

fn labelled(
    m: &std::collections::BTreeMap<String, u64>,
    f: fn(&str) -> &'static str,
) -> Vec<(String, u64)> {
    // Different raw codes can share the generic label: merge them.
    let mut out: Vec<(String, u64)> = Vec::new();
    for (k, n) in m {
        let l = f(k);
        match out.iter_mut().find(|(x, _)| x == l) {
            Some(e) => e.1 += n,
            None => out.push((l.to_string(), *n)),
        }
    }
    out.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    out
}

pub fn source_view(s: &SourceInfo, v2: bool) -> SourceView {
    let inactive = s.status != "active";
    let plain = SourceView {
        readiness: status_label(&s.status).into(),
        tone: if inactive { Tone::Warn } else { Tone::Good },
        anomalies: vec![],
        counts_line: None,
        superseded: None,
        skipped: vec![],
        failed: vec![],
        tooltip: vec![],
        legacy_detail: Some(format!("{} files", s.files)),
    };
    if !v2 || (s.readiness.is_none() && s.counts.is_none()) {
        if v2 && !inactive {
            // New daemon but no readiness from the worker: not claimed ready.
            return SourceView {
                readiness: "unknown".into(),
                tone: Tone::Unknown,
                ..plain
            };
        }
        return plain;
    }
    let counts = s.counts.as_ref();
    let versions = counts.and_then(|c| c.versions.as_ref()).filter(|v| v.known);
    let mut anomalies: Vec<String> = s
        .anomalies
        .iter()
        .map(|a| anomaly_label(a).to_string())
        .collect();
    anomalies.dedup();
    let has_empty_ready = s.anomalies.iter().any(|a| a == "empty_ready")
        || versions.is_some_and(|v| v.empty_ready.unwrap_or(0) > 0);

    let (readiness, tone) = if inactive {
        (status_label(&s.status).to_string(), Tone::Warn)
    } else {
        match s.readiness.unwrap_or(Readiness::Unknown) {
            Readiness::Ready => {
                let usable_ok = versions.is_some_and(|v| v.ready.unwrap_or(0) > 0);
                let clean = versions.is_some_and(|v| {
                    v.pending.unwrap_or(0) == 0 && v.failed.unwrap_or(0) == 0
                }) && s.anomalies.is_empty();
                if has_empty_ready || (usable_ok && !clean) {
                    ("Partly ready".to_string(), Tone::Warn)
                } else if usable_ok {
                    ("Ready".to_string(), Tone::Good)
                } else {
                    // "ready" without a usable-file count behind it is not shown as ready.
                    ("Not confirmed ready".to_string(), Tone::Unknown)
                }
            }
            Readiness::Indexing => {
                let usable = versions
                    .map(|v| v.ready.unwrap_or(0) + v.keyword_only.unwrap_or(0))
                    .filter(|_| versions.is_some_and(|v| v.ready.is_some()));
                match usable {
                    Some(0) => (
                        "Indexing, no usable files yet".to_string(),
                        Tone::Info,
                    ),
                    _ => ("Indexing".to_string(), Tone::Info),
                }
            }
            Readiness::Partial => ("Partly ready".to_string(), Tone::Warn),
            Readiness::Empty => ("Empty, nothing to index".to_string(), Tone::Warn),
            Readiness::Failed => ("Failed".to_string(), Tone::Bad),
            Readiness::Unavailable => ("Unavailable".to_string(), Tone::Bad),
            Readiness::Unknown => ("unknown".to_string(), Tone::Unknown),
        }
    };

    let counts_line = (!inactive).then(|| counts_line(counts));
    let superseded = counts
        .and_then(|c| c.history.as_ref())
        .filter(|h| h.known)
        .and_then(|h| h.superseded)
        .map(|n| format!("{n} superseded versions kept (history, not counted above)"));
    let job = counts.and_then(|c| c.job.as_ref()).filter(|j| j.known);
    let (mut skipped, mut failed) = (vec![], vec![]);
    if let Some(j) = job {
        let mut all = j.skipped_by_code.clone();
        for (k, n) in &j.counted_only {
            *all.entry(k.clone()).or_insert(0) += n;
        }
        skipped = labelled(&all, skip_label);
        failed = labelled(&j.failed_by_code, failure_label);
    }

    let mut tooltip = Vec::new();
    if let Some(c) = counts {
        tooltip.push("Ready, keyword-only, pending, failed and empty: the current version of each file in one snapshot.".to_string());
        match versions {
            Some(v) if v.reconciles => tooltip.push("Counts reconcile (same snapshot).".into()),
            Some(_) => tooltip.push("Reconciliation between counts is not established.".into()),
            None => tooltip.push("Version counts unknown.".into()),
        }
        tooltip.push(match (job, c.basis.as_ref().and_then(|b| b.skipped_by_code.as_deref())) {
            (Some(_), Some("latest_whole_source_job")) => {
                "Skipped: from the latest whole-folder ingest job; that is a different unit from the counts above.".into()
            }
            (Some(_), _) => "Skipped: from one ingest job (different unit).".into(),
            (None, _) => "Skipped and failed-by-reason: unknown (no whole-folder ingest job on record).".into(),
        });
        if let Some(j) = job {
            if j.interrupted == Some(true) {
                tooltip.push("The last ingest job was interrupted.".into());
            }
        }
    }
    SourceView {
        readiness,
        tone,
        anomalies,
        counts_line,
        superseded,
        skipped,
        failed,
        tooltip,
        legacy_detail: None,
    }
}

fn counts_line(c: Option<&SourceCounts>) -> String {
    let v = c.and_then(|c| c.versions.as_ref()).filter(|v| v.known);
    let g = |f: fn(&VersionCounts) -> Option<u64>| unk(v.and_then(f));
    let job = c.and_then(|c| c.job.as_ref()).filter(|j| j.known);
    let skipped = match job {
        Some(j) => (sum(&j.skipped_by_code) + sum(&j.counted_only)).to_string(),
        None => "unknown".into(),
    };
    let mut line = format!(
        "ready {} · keyword-only {} · pending {} · skipped {} · failed {} · empty {}",
        g(|v| v.ready),
        g(|v| v.keyword_only),
        g(|v| v.pending),
        skipped,
        g(|v| v.failed),
        g(|v| v.empty_ready),
    );
    if let Some(o) = v.and_then(|v| v.other).filter(|n| *n > 0) {
        line.push_str(&format!(" · other {o}"));
    }
    line
}

// ---------------------------------------------------------------------------
// source list freshness
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
pub struct SourcesState {
    pub list: Vec<SourceInfo>,
    received_ms: Option<u64>,
    refresh_failed: bool,
}

impl SourcesState {
    /// A failed refresh keeps the last good list.
    pub fn apply(&mut self, r: Result<Vec<SourceInfo>, ()>, now_ms: u64) {
        match r {
            Ok(l) => {
                self.list = l;
                self.received_ms = Some(now_ms);
                self.refresh_failed = false;
            }
            Err(()) => self.refresh_failed = true,
        }
    }

    pub fn age_ms(&self, now_ms: u64) -> Option<u64> {
        self.received_ms.map(|t| now_ms.saturating_sub(t))
    }

    /// Text under the list: its age while a question runs, when old, or when
    /// the last refresh failed. `None` = current, say nothing.
    pub fn note(&self, now_ms: u64, question_running: bool) -> Option<String> {
        let age = self.age_ms(now_ms)?;
        if self.refresh_failed {
            Some(format!("Could not refresh. Showing the list from {}.", fmt_age(age)))
        } else if age > SOURCES_STALE_MS || question_running {
            Some(format!("As of {}", fmt_age(age)))
        } else {
            None
        }
    }

    pub fn is_old(&self, now_ms: u64) -> bool {
        self.refresh_failed || self.age_ms(now_ms).is_some_and(|a| a > SOURCES_STALE_MS)
    }
}

// ---------------------------------------------------------------------------
// answer metadata
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct AnswerMeta {
    /// Only for an abstained answer: the engine's reason, or "not recorded".
    pub abstention: Option<String>,
    pub warnings: Vec<String>,
    pub coverage: Option<String>,
    pub scope_files: Option<String>,
}

pub fn answer_meta(r: &AskResult) -> AnswerMeta {
    let abstention = r.abstained.then(|| match r.abstention_reason.as_deref() {
        None => "not recorded".to_string(),
        Some(code) => match lookup(ABSTENTION, code) {
            UNRECOGNISED => "recorded, but not a reason this version recognises".to_string(),
            label => format!("{label} ({code})"),
        },
    });
    let mut warnings: Vec<String> = Vec::new();
    for c in &r.warning_codes {
        let l = warning_label(c).to_string();
        if !warnings.contains(&l) {
            warnings.push(l);
        }
    }
    let coverage = r.index_coverage.as_ref().map(|c| {
        let mut s = format!(
            "Index coverage: {} of {} indexed · {} keyword-only",
            c.indexed, c.total, c.keyword_only
        );
        if c.used_provisional {
            s.push_str(" · answer used keyword-only passages");
        }
        s
    });
    AnswerMeta {
        abstention,
        warnings,
        coverage,
        scope_files: r.scope_file_count.map(|n| format!("Files in scope: {n}")),
    }
}

#[cfg(test)]
mod tests;
