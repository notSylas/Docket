//! Additive status types (R2a): health sections, source readiness and counts,
//! and the optional metadata of an answer.
//!
//! Everything here is codes and counts. There is deliberately no field for a
//! path, a file name, document text or an error message: the daemon decodes the
//! worker's JSON into these types and anything else is dropped, so such text
//! cannot reach `/v1/status`, `/v1/sources` or any event.
//!
//! ACCOUNTING UNITS. Each count block names its unit and is never claimed to
//! reconcile with another block:
//! * [`VersionCounts`] (`unit: current_version_per_file`): non-superseded
//!   evidence versions of ACTIVE sources, one read snapshot. `reconciles` is true
//!   only when the categories are complete, mutually exclusive and sum to
//!   `total`. `empty_ready` (READY with zero chunks) is anomalous and is NOT in
//!   `ready`.
//! * [`QueueCounts`] (`unit: ingest_queue_row`): tiered-indexing rows.
//! * [`JobCounts`] (`unit: ingestion_file_result_of_one_job`): rows of ONE named
//!   whole-source job. Tiered runs and path-level jobs leave no rows, so their
//!   skipped/failed history is unknown (`known: false`), never zero.
//! * [`HistoryCounts`] (`unit: superseded_evidence_version`): kept apart from
//!   the active equation.
//!
//! Unknown is `known: false` with a reason code (or an absent value), never 0.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Sections carry codes of this shape only; anything else becomes `unknown`.
pub fn sanitize_code(s: &str) -> String {
    let ok = !s.is_empty()
        && s.len() <= 40
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if ok {
        s.to_string()
    } else {
        "unknown".to_string()
    }
}

fn sanitize_map(m: &mut BTreeMap<String, u64>) {
    *m = std::mem::take(m)
        .into_iter()
        .map(|(k, v)| (sanitize_code(&k), v))
        .collect();
}

/// Health of one section.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SectionState {
    Ok,
    Degraded,
    Unavailable,
    #[default]
    #[serde(other)]
    Unknown,
}

/// yes / no / unknown, for facts that may not be determinable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Tri {
    Yes,
    No,
    #[default]
    #[serde(other)]
    Unknown,
}

/// One status section: producer state and code, the producer's timestamp, and
/// (filled in by the daemon at response time) the age and a stale flag.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Section<T> {
    #[serde(default)]
    pub state: SectionState,
    #[serde(default)]
    pub code: String,
    /// When the producer measured this (ms since the Unix epoch).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub checked_at_ms: Option<u64>,
    /// Daemon-computed at response time: now - `checked_at_ms`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub age_ms: Option<u64>,
    /// Daemon-computed: older than the stale threshold, or the producing
    /// worker is gone. Stale data is never presented as current.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stale: Option<bool>,
    #[serde(flatten)]
    pub data: T,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessData {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uptime_s: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub protocol: Option<u32>,
    #[serde(default)]
    pub features: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DatabaseData {
    #[serde(default)]
    pub readable: Tri,
    /// Migration revision id (an opaque code such as `0012_...`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<String>,
    #[serde(default)]
    pub at_head: Tri,
}

/// One model: installed (listed by Ollama), reachable (Ollama answered),
/// resident (loaded now, from `/api/ps`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelState {
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub installed: Tri,
    #[serde(default)]
    pub reachable: Tri,
    #[serde(default)]
    pub resident: Tri,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size_vram: Option<u64>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelsData {
    /// Whether the local Ollama server answered.
    #[serde(default)]
    pub ollama: Tri,
    #[serde(default)]
    pub answer: ModelState,
    #[serde(default)]
    pub embed: ModelState,
}

/// Version counts. Counts are `None` when `known` is false.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct VersionCounts {
    #[serde(default)]
    pub known: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total: Option<u64>,
    /// READY with chunks: usable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ready: Option<u64>,
    /// TEXT_READY with chunks: searchable by keyword only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keyword_only: Option<u64>,
    /// READY/TEXT_READY with zero chunks: anomalous, not usable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub empty_ready: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pending: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failed: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub other: Option<u64>,
    #[serde(default)]
    pub reconciles: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueueCounts {
    #[serde(default)]
    pub known: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queued: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub running: Option<u64>,
    /// Dead-letter COUNT only; never paths or error text.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dead: Option<u64>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct HistoryCounts {
    #[serde(default)]
    pub known: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub superseded: Option<u64>,
}

/// The latest whole-source ingest job and its file-result rows.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobCounts {
    #[serde(default)]
    pub known: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_id: Option<String>,
    /// PENDING | RUNNING | SUCCEEDED | FAILED | PARTIAL (closed set).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    /// A RUNNING job whose writer lock is free: `Some(true)`. `None` = unknown.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interrupted: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub files: Option<u64>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub outcomes: BTreeMap<String, u64>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub skipped_by_code: BTreeMap<String, u64>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub failed_by_code: BTreeMap<String, u64>,
    /// Skip reasons the job only counted (not rows), e.g. `unsupported_other`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub counted_only: BTreeMap<String, u64>,
    #[serde(default)]
    pub reconciles: bool,
}

/// Which snapshot / job the counts of a source are based on.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CountsBasis {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub versions: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_id: Option<String>,
    /// `latest_whole_source_job` or `unknown`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skipped_by_code: Option<String>,
    /// `free` | `held` | `unknown`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub writer_lock: Option<String>,
}

/// Per-source counts. Blocks are separate accounting units; none reconciles
/// with another.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceCounts {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshot_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub versions: Option<VersionCounts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue: Option<QueueCounts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job: Option<JobCounts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub history: Option<HistoryCounts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub basis: Option<CountsBasis>,
}

impl SourceCounts {
    /// Keep only closed-set codes (drops anything that is not `[a-z0-9_]{1,40}`).
    pub fn sanitize(&mut self) {
        if let Some(j) = &mut self.job {
            for m in [
                &mut j.outcomes,
                &mut j.skipped_by_code,
                &mut j.failed_by_code,
                &mut j.counted_only,
            ] {
                sanitize_map(m);
            }
            j.status = j.status.take().map(|s| sanitize_code(&s.to_lowercase()));
            j.reason = j.reason.take().map(|s| sanitize_code(&s));
            j.unit = j.unit.take().map(|s| sanitize_code(&s));
        }
        for r in [
            self.versions.as_mut().map(|v| &mut v.reason),
            self.queue.as_mut().map(|v| &mut v.reason),
            self.history.as_mut().map(|v| &mut v.reason),
        ]
        .into_iter()
        .flatten()
        {
            *r = r.take().map(|s| sanitize_code(&s));
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TierEta {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tier1: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tier2: Option<u64>,
}

/// Library-level indexing view.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexingData {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshot_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub versions: Option<VersionCounts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue: Option<QueueCounts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub history: Option<HistoryCounts>,
    /// Dead-letter count only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dead_letters: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub eta_s: Option<TierEta>,
    /// `free` | `held` | `unknown`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub writer: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interrupted_jobs: Option<u64>,
}

/// The worker's `health` result.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerHealth {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub process: Option<Section<ProcessData>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub database: Option<Section<DatabaseData>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub models: Option<Section<ModelsData>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub indexing: Option<Section<IndexingData>>,
}

impl WorkerHealth {
    /// Drop anything that is not a closed-set code.
    pub fn sanitize(&mut self) {
        fn code<T>(s: &mut Section<T>) {
            s.code = sanitize_code(&s.code);
        }
        if let Some(s) = &mut self.process {
            code(s);
            s.data.features = std::mem::take(&mut s.data.features)
                .into_iter()
                .filter(|f| {
                    f.len() <= 40
                        && f.bytes().all(|b| {
                            b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.' || b == b'_'
                        })
                })
                .collect();
        }
        if let Some(s) = &mut self.database {
            code(s);
            s.data.revision = s.data.revision.take().filter(|r| {
                r.len() <= 64 && r.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
            });
        }
        if let Some(s) = &mut self.models {
            code(s);
            // Model names are configuration ids (e.g. `qwen3:14b`), kept short and plain.
            for m in [&mut s.data.answer, &mut s.data.embed] {
                let plain = m.name.len() <= 80
                    && !m.name.starts_with('/')
                    && m.name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._:/-".contains(&b));
                if !plain {
                    m.name = String::new();
                }
            }
        }
        if let Some(s) = &mut self.indexing {
            code(s);
            s.data.writer = s
                .data
                .writer
                .take()
                .filter(|w| matches!(w.as_str(), "free" | "held" | "unknown"));
        }
    }
}

/// Readiness of one source (closed set; anything else reads as `unknown`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Readiness {
    /// Usable files, nothing outstanding or anomalous.
    Ready,
    /// Work outstanding. Zero usable files while indexing is never `ready`.
    Indexing,
    /// Some usable files, but failures, dead letters, empty-ready versions,
    /// an interrupted job or keyword-only files with no work left.
    Partial,
    /// Nothing to index.
    Empty,
    /// Nothing usable and failures explain it.
    Failed,
    /// Source not active (revoked, tombstoned, missing) or counts unreadable.
    Unavailable,
    #[default]
    #[serde(other)]
    Unknown,
}

/// Daemon-computed view of the worker process it supervises. Always fresh: it
/// is read at response time, so it stays accurate while the worker is busy or
/// gone.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Responsiveness {
    /// Answered recently.
    Responsive,
    /// A question is running; the worker is not expected to answer meanwhile.
    Busy,
    /// Should have answered by now (or is down).
    Unresponsive,
    #[default]
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerProcess {
    pub state: crate::api::WorkerState,
    /// Relaunches since the daemon started (a crash, a hung call or a cancel
    /// each cost one relaunch).
    pub restarts: u64,
    /// Unexpected worker failures only (crash, timeout, failed start); a user
    /// cancel is not a crash.
    pub crashes: u64,
    /// Milliseconds since the worker entered `state`.
    pub since_ms: u64,
    /// Closed code of the last failure, if any.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error_code: Option<String>,
    pub responsiveness: Responsiveness,
    /// Milliseconds since the worker last answered anything.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_ok_age_ms: Option<u64>,
    pub checked_at_ms: u64,
}

/// `health` of `GET /v1/status` (present only with the `status.v2` capability).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StatusHealth {
    pub v: u32,
    pub worker_process: WorkerProcess,
    pub refresh_interval_ms: u64,
    /// Sections older than this are marked `stale`.
    pub stale_after_ms: u64,
    /// Refreshes skipped because a question was running.
    pub refresh_skipped_busy: u64,
    /// Closed code of the last failed refresh, cleared by a success.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refresh_error_code: Option<String>,
    #[serde(flatten)]
    pub sections: WorkerHealth,
}

/// Answer index coverage: numbers only.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexCoverage {
    pub indexed: u64,
    pub total: u64,
    #[serde(default)]
    pub keyword_only: u64,
    #[serde(default)]
    pub used_provisional: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sanitize_code_is_a_closed_shape() {
        assert_eq!(sanitize_code("db_busy"), "db_busy");
        assert_eq!(sanitize_code("/home/a/b.txt"), "unknown");
        assert_eq!(sanitize_code("Has Space"), "unknown");
        assert_eq!(sanitize_code(""), "unknown");
    }

    #[test]
    fn section_round_trips_and_flattens() {
        let raw = json!({"state":"ok","code":"ok","checked_at_ms":5,"readable":"yes","revision":"0012_x","at_head":"no","path":"/leak"});
        let s: Section<DatabaseData> = serde_json::from_value(raw).unwrap();
        assert_eq!(s.data.readable, Tri::Yes);
        let out = serde_json::to_value(&s).unwrap();
        assert!(out.get("path").is_none(), "unknown fields are dropped");
        assert!(out.get("age_ms").is_none() && out.get("stale").is_none());
        assert_eq!(out["at_head"], "no");
    }

    #[test]
    fn unknown_enum_values_degrade_to_unknown() {
        let s: Section<DatabaseData> =
            serde_json::from_value(json!({"state":"weird","readable":"maybe"})).unwrap();
        assert_eq!(s.state, SectionState::Unknown);
        assert_eq!(s.data.readable, Tri::Unknown);
    }

    #[test]
    fn counts_sanitize_drops_non_codes() {
        let mut c: SourceCounts = serde_json::from_value(json!({
            "job": {"known": true, "skipped_by_code": {"too_large": 1, "/etc/passwd": 2},
                    "status": "PARTIAL", "reason": "No Way"}
        }))
        .unwrap();
        c.sanitize();
        let j = c.job.unwrap();
        assert_eq!(j.skipped_by_code.get("unknown"), Some(&2));
        assert_eq!(j.status.as_deref(), Some("partial"));
        assert_eq!(j.reason.as_deref(), Some("unknown"));
    }

    #[test]
    fn unknown_counts_are_absent_not_zero() {
        let v = VersionCounts {
            known: false,
            reason: Some("source_inactive".into()),
            ..Default::default()
        };
        assert_eq!(
            serde_json::to_value(&v).unwrap(),
            json!({"known": false, "reason": "source_inactive", "reconciles": false})
        );
    }
}
