use super::*;
use docket_proto::api::{AskResult, SourceInfo, StatusResponse};
use serde_json::{json, Value};

const HOSTILE: &str = "/home/alice/payroll-SECRET-9917.xlsx";
const BITS: [&str; 3] = ["SECRET-9917", "/home/alice", "payroll"];

fn sec(state: &str, code: &str, age: u64, stale: bool, extra: Value) -> Value {
    let mut v =
        json!({"state": state, "code": code, "checked_at_ms": 1, "age_ms": age, "stale": stale});
    v.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    v
}

fn vc(ready: u64, kw: u64, empty: u64, pending: u64, failed: u64) -> Value {
    json!({"known": true, "unit": "current_version_per_file", "ready": ready, "keyword_only": kw,
           "empty_ready": empty, "pending": pending, "failed": failed, "other": 0,
           "total": ready + kw + empty + pending + failed, "reconciles": true})
}

fn good_sections() -> Value {
    json!({
        "database": sec("ok", "ok", 1000, false, json!({"readable": "yes", "revision": "0012_x", "at_head": "yes"})),
        "models": sec("ok", "ok", 1000, false, json!({"ollama": "yes",
            "answer": {"name": "qwen3:14b", "installed": "yes", "reachable": "yes", "resident": "yes"},
            "embed": {"name": "bge", "installed": "yes", "reachable": "yes", "resident": "yes"}})),
        "indexing": sec("ok", "ok", 1000, false, json!({"versions": vc(3,0,0,0,0),
            "queue": {"known": true, "queued": 0, "running": 0, "dead": 0}, "dead_letters": 0,
            "eta_s": {"tier1": 0, "tier2": null}, "writer": "free", "interrupted_jobs": 0})),
    })
}

fn status_with(
    worker: &str,
    busy: bool,
    resp: &str,
    sections: Value,
    refresh_err: Option<&str>,
) -> StatusResponse {
    let mut h = json!({"v": 1,
        "worker_process": {"state": worker, "restarts": 0, "crashes": 0, "since_ms": 5000, "responsiveness": resp,
                           "last_ok_age_ms": 500, "checked_at_ms": 9},
        "refresh_interval_ms": 15000, "stale_after_ms": 45000, "refresh_skipped_busy": 0});
    h.as_object_mut()
        .unwrap()
        .extend(sections.as_object().unwrap().clone());
    if let Some(e) = refresh_err {
        h["refresh_error_code"] = json!(e);
    }
    serde_json::from_value(json!({"service": "ok", "version": "0.1.0",
        "worker": {"state": worker, "restarts": 0, "model": "m", "chunks": 3},
        "busy": busy, "capabilities": ["status.v2", "tasks.v1"], "health": h}))
    .unwrap()
}

fn healthy() -> StatusResponse {
    status_with("up", false, "responsive", good_sections(), None)
}

fn model_with(s: StatusResponse) -> StatusModel {
    let mut m = StatusModel::default();
    m.apply(Ok(s), 0);
    m
}

fn chip(v: &StatusView, k: ChipKind) -> &Chip {
    v.chips.iter().find(|c| c.kind == k).unwrap()
}

fn all_text(v: &StatusView) -> String {
    let mut s = String::new();
    for c in &v.chips {
        s += &c.display();
        s += &c.tooltip.join("\n");
    }
    for b in &v.banners {
        s += &b.text;
    }
    if let AskGate::Disabled(t) = &v.ask {
        s += t;
    }
    s
}

fn no_leak(t: &str) {
    for b in BITS {
        assert!(!t.contains(b), "leaked {b}: {t}");
    }
}

#[test]
fn loading_before_first_status() {
    let m = StatusModel::default();
    let v = m.view(0, false);
    assert_eq!(v.overall, Overall::Loading);
    assert!(matches!(v.ask, AskGate::Disabled(ref t) if t.contains("Connecting")));
    assert!(v.chips.is_empty());
}

#[test]
fn connected_healthy_has_separate_chips_not_one_ok() {
    let v = model_with(healthy()).view(1000, false);
    assert_eq!(v.overall, Overall::Healthy);
    assert!(v.v2);
    assert_eq!(v.ask, AskGate::Enabled);
    let names: Vec<_> = v.chips.iter().map(|c| c.name).collect();
    assert_eq!(names, ["Service", "Worker", "Model", "Index", "Database"]);
    let vals: std::collections::HashSet<_> = v.chips.iter().map(|c| c.value.clone()).collect();
    assert!(vals.len() >= 4, "chips carry distinct facts: {vals:?}");
    assert_eq!(chip(&v, ChipKind::Worker).value, "up");
    assert_eq!(chip(&v, ChipKind::Index).value, "idle");
    assert!(v.banners.is_empty());
}

#[test]
fn degraded_when_a_section_is_degraded_or_unavailable() {
    let mut s = good_sections();
    s["database"] = sec(
        "unavailable",
        "db_unreadable",
        100,
        false,
        json!({"readable": "no", "at_head": "unknown"}),
    );
    s["indexing"] = sec("unavailable", "index_unreadable", 100, false, json!({}));
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    assert_eq!(v.overall, Overall::Degraded);
    assert_eq!(chip(&v, ChipKind::Database).value, "unreadable");
    assert_eq!(chip(&v, ChipKind::Index).value, "unreadable");
    assert!(v.banners.iter().any(|b| b.text.contains("Database")));
}

#[test]
fn stale_sections_are_dimmed_with_age_and_make_it_degraded() {
    let mut s = good_sections();
    s["models"]["age_ms"] = json!(120_000);
    s["models"]["stale"] = json!(true);
    let v = model_with(status_with(
        "up",
        false,
        "responsive",
        s,
        Some("health_timeout"),
    ))
    .view(5_000, false);
    assert_eq!(v.overall, Overall::Degraded);
    let m = chip(&v, ChipKind::Model);
    assert!(m.stale);
    // daemon age 120 s + 5 s since the poll arrived
    assert!(m.display().contains("as of 2 min ago"), "{}", m.display());
    let b = v.banners.iter().map(|b| b.text.clone()).collect::<String>();
    assert!(
        b.contains("Out of date") && b.contains("health check timed out"),
        "{b}"
    );
    // Index section was marked fresh by the daemon but the refresh failed: stale too.
    assert!(chip(&v, ChipKind::Index).stale);
}

#[test]
fn a_section_with_no_age_or_stale_flag_is_never_current() {
    let mut s = good_sections();
    s["models"].as_object_mut().unwrap().remove("age_ms");
    s["models"].as_object_mut().unwrap().remove("stale");
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    let m = chip(&v, ChipKind::Model);
    assert!(m.stale);
    assert!(m.display().contains("age unknown"));
}

#[test]
fn section_ages_grow_locally_between_polls() {
    let m = model_with(healthy());
    let a = chip(&m.view(1_000, false), ChipKind::Database)
        .age
        .clone()
        .unwrap();
    assert_eq!(a, "2 s ago");
    // 8 s later with no new poll (not yet "silent"): age is 9 s, not 1 s.
    let a = chip(&m.view(8_000, false), ChipKind::Database)
        .age
        .clone()
        .unwrap();
    assert_eq!(a, "9 s ago");
}

#[test]
fn unreachable_keeps_last_good_forced_stale_with_age() {
    let mut m = model_with(healthy());
    m.apply(Err(FetchError::Unreachable), 7_000);
    let v = m.view(7_000, false);
    assert_eq!(v.overall, Overall::Unavailable);
    assert!(chip(&v, ChipKind::Service).value.contains("not reachable"));
    // last-good data still there, dimmed, with age (7 s since the last good poll + 1 s section age)
    let db = chip(&v, ChipKind::Database);
    assert!(db.stale && db.value == "readable");
    assert_eq!(db.age.as_deref(), Some("8 s ago"));
    assert!(v.banners[0].text.contains("last known state from 7 s ago"));
    match &v.ask {
        AskGate::Disabled(t) => {
            assert!(t.contains("not reachable") && t.contains("7 s ago"), "{t}")
        }
        _ => panic!("ask must be off"),
    }
    // recovery
    m.apply(Ok(healthy()), 9_000);
    assert_eq!(m.view(9_000, false).overall, Overall::Healthy);
}

#[test]
fn no_poll_for_a_long_time_is_unavailable_even_without_an_error() {
    let m = model_with(healthy());
    let v = m.view(STATUS_STALE_LOCAL_MS + 1, false);
    assert_eq!(v.overall, Overall::Unavailable);
    assert!(matches!(v.ask, AskGate::Disabled(_)));
}

#[test]
fn typed_failure_is_failed_and_never_shows_the_message() {
    let mut m = model_with(healthy());
    m.apply(Err(FetchError::Http(500)), 2_000);
    let v = m.view(2_000, false);
    assert_eq!(v.overall, Overall::Failed);
    assert!(chip(&v, ChipKind::Service).value.contains("problem"));
    assert!(v.banners[0].text.contains("HTTP 500"));
    // before any status
    let mut m = StatusModel::default();
    m.apply(Err(FetchError::Protocol), 0);
    let v = m.view(0, false);
    assert_eq!(v.overall, Overall::Failed);
    assert!(matches!(v.ask, AskGate::Disabled(_)));
    let mut m = StatusModel::default();
    m.apply(Err(FetchError::Unreachable), 0);
    assert_eq!(m.view(0, false).overall, Overall::Unavailable);
}

#[test]
fn worker_states_and_why_asking_is_off() {
    let cases = [
        ("starting", false, "unknown", "starting", "starting"),
        ("down", false, "unresponsive", "down", "not running"),
        (
            "up",
            true,
            "busy",
            "busy (answering)",
            "another question is running",
        ),
    ];
    for (w, busy, resp, value, why) in cases {
        let v = model_with(status_with(w, busy, resp, good_sections(), None)).view(0, false);
        assert_eq!(chip(&v, ChipKind::Worker).value, value);
        match &v.ask {
            AskGate::Disabled(t) => assert!(t.contains(why), "{w}: {t}"),
            _ => panic!("{w}: ask must be off"),
        }
    }
    // our own question running
    let v = model_with(healthy()).view(0, true);
    assert!(matches!(v.ask, AskGate::Disabled(ref t) if t.contains("question is running")));
    // busy is not degradation
    let v = model_with(status_with("up", true, "busy", good_sections(), None)).view(0, false);
    assert_eq!(v.overall, Overall::Healthy);
}

#[test]
fn worker_crash_and_restart_display() {
    let mut s = status_with("starting", false, "unknown", good_sections(), None);
    {
        let h = s.health.as_mut().unwrap();
        h.worker_process.restarts = 2;
        h.worker_process.crashes = 2;
        h.worker_process.last_error_code = Some("worker_crashed".into());
    }
    let v = model_with(s).view(0, false);
    let w = chip(&v, ChipKind::Worker);
    assert_eq!(w.value, "restarting");
    let tip = w.tooltip.join("\n");
    assert!(
        tip.contains("Relaunches 2")
            && tip.contains("failures 2")
            && tip.contains("worker crashed"),
        "{tip}"
    );
    assert_eq!(v.overall, Overall::Degraded);
    // unresponsive-but-up
    let v = model_with(status_with(
        "up",
        false,
        "unresponsive",
        good_sections(),
        None,
    ))
    .view(0, false);
    assert_eq!(chip(&v, ChipKind::Worker).tone, Tone::Bad);
    // never says "read-only" about the worker
    assert!(!all_text(&v).to_lowercase().contains("read-only"));
    assert!(!all_text(&v).to_lowercase().contains("read only"));
}

#[test]
fn model_states_installed_reachable_resident_are_separate_and_can_be_unknown() {
    let mut s = good_sections();
    s["models"] = sec(
        "degraded",
        "model_missing",
        100,
        false,
        json!({"ollama": "yes",
        "answer": {"name": "qwen3:14b", "installed": "no", "reachable": "yes", "resident": "no"},
        "embed": {"name": "bge", "installed": "yes", "reachable": "yes", "resident": "unknown"}}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    let m = chip(&v, ChipKind::Model);
    assert_eq!(m.tone, Tone::Bad);
    assert!(m.value.contains("answer not installed"), "{}", m.value);
    let tip = m.tooltip.join("\n");
    assert!(
        tip.contains("installed no · reachable yes · resident (loaded now) no"),
        "{tip}"
    );
    assert!(
        tip.contains("installed yes · reachable yes · resident (loaded now) unknown"),
        "{tip}"
    );
    // asking stays possible (not guessed), but a banner explains
    assert_eq!(v.ask, AskGate::Enabled);
    assert!(v.banners.iter().any(|b| b.text.starts_with("Model")));
    // not resident alone: informational, not degraded
    let mut s = good_sections();
    s["models"]["answer"]["resident"] = json!("no");
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    assert_eq!(chip(&v, ChipKind::Model).tone, Tone::Good.max_info());
    assert_eq!(v.overall, Overall::Healthy);
    // everything undeterminable: unknown, not healthy
    let mut s = good_sections();
    s["models"] = sec(
        "unknown",
        "probe_failed",
        100,
        false,
        json!({"ollama": "unknown",
        "answer": {"installed": "unknown", "reachable": "unknown", "resident": "unknown"}, "embed": {}}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    let m = chip(&v, ChipKind::Model);
    assert_eq!(m.tone, Tone::Unknown);
    assert!(m.value.contains("unknown"));
    assert_eq!(v.overall, Overall::Checking);
}

trait InfoTone {
    fn max_info(self) -> Tone;
}
impl InfoTone for Tone {
    // "installed, not loaded" is informational.
    fn max_info(self) -> Tone {
        Tone::Info
    }
}

#[test]
fn missing_sections_are_unknown_not_healthy() {
    let v = model_with(status_with("up", false, "responsive", json!({}), None)).view(0, false);
    assert_eq!(v.overall, Overall::Checking);
    for k in [ChipKind::Model, ChipKind::Index, ChipKind::Database] {
        assert_eq!(chip(&v, k).value, "unknown");
        assert_eq!(chip(&v, k).tone, Tone::Unknown);
    }
}

#[test]
fn index_zero_result_indexing_shows_progress_not_ready() {
    let mut s = good_sections();
    s["indexing"] = sec(
        "ok",
        "ok",
        100,
        false,
        json!({"versions": vc(0,0,0,12,0),
        "queue": {"known": true, "queued": 12, "running": 0, "dead": 0},
        "eta_s": {"tier1": 30, "tier2": null}, "writer": "held", "dead_letters": 0}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    let i = chip(&v, ChipKind::Index);
    assert_eq!(i.value, "indexing 0 of 12 (ETA ~30 s)");
    assert_ne!(i.tone, Tone::Good);
    // eta unknown stays unknown
    let mut s = good_sections();
    s["indexing"] = sec(
        "ok",
        "ok",
        100,
        false,
        json!({"versions": vc(2,0,0,2,0), "writer": "held",
        "eta_s": {"tier1": null, "tier2": null}}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    assert_eq!(
        chip(&v, ChipKind::Index).value,
        "indexing 2 of 4 (ETA unknown)"
    );
}

#[test]
fn index_dead_letters_interrupted_failed_and_unknown() {
    let mut s = good_sections();
    s["indexing"] = sec(
        "degraded",
        "queue_dead_letters",
        100,
        false,
        json!({"versions": vc(2,0,0,0,0),
        "queue": {"known": true, "queued": 0, "running": 0, "dead": 3}, "dead_letters": 3,
        "writer": "free", "interrupted_jobs": 1}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    let i = chip(&v, ChipKind::Index);
    assert!(
        i.value.contains("3 dead letters") && i.value.contains("interrupted"),
        "{}",
        i.value
    );
    assert_eq!(i.tone, Tone::Warn);
    assert_eq!(v.overall, Overall::Degraded);

    let mut s = good_sections();
    s["indexing"] = sec(
        "degraded",
        "anomalies",
        100,
        false,
        json!({"versions": vc(0,0,0,0,4), "writer": "free"}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    assert_eq!(chip(&v, ChipKind::Index).value, "failed (4 files)");
    assert_eq!(chip(&v, ChipKind::Index).tone, Tone::Bad);

    // unknown counts: never idle, never 0
    let mut s = good_sections();
    s["indexing"] = sec(
        "ok",
        "ok",
        100,
        false,
        json!({"versions": {"known": false, "reason": "read_failed"}}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    assert_eq!(chip(&v, ChipKind::Index).value, "unknown");
}

#[test]
fn index_empty_ready_is_never_idle_fine() {
    let mut s = good_sections();
    s["indexing"] = sec(
        "degraded",
        "anomalies",
        100,
        false,
        json!({"versions": vc(2,0,1,0,0), "writer": "free"}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    let i = chip(&v, ChipKind::Index);
    assert!(i.value.contains("inconsistent"), "{}", i.value);
    assert_eq!(i.tone, Tone::Warn);
    assert!(v.banners.iter().any(|b| b
        .text
        .contains("1 files are marked ready but have no searchable")));
}

#[test]
fn database_states() {
    let mut s = good_sections();
    s["database"] = sec(
        "degraded",
        "schema_behind",
        100,
        false,
        json!({"readable": "yes", "at_head": "no"}),
    );
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    assert_eq!(chip(&v, ChipKind::Database).value, "readable, needs update");
    assert_eq!(chip(&v, ChipKind::Database).tone, Tone::Warn);
    let mut s = good_sections();
    s["database"] = sec("unknown", "unknown", 100, false, json!({}));
    let v = model_with(status_with("up", false, "responsive", s, None)).view(0, false);
    assert_eq!(chip(&v, ChipKind::Database).value, "unknown");
}

#[test]
fn old_daemon_without_status_v2_gets_the_legacy_ui() {
    let s: StatusResponse = serde_json::from_value(json!({"service": "ok", "version": "0.1.0",
        "worker": {"state": "up", "restarts": 0, "model": "m", "chunks": 3}, "busy": false}))
    .unwrap();
    let mut m = model_with(s);
    let v = m.view(0, false);
    assert!(!v.v2 && v.chips.is_empty() && v.banners.is_empty());
    assert_eq!(v.ask, AskGate::Enabled);
    // error handling in legacy mode: still no chips, ask off with reason
    m.apply(Err(FetchError::Unreachable), 1000);
    let v = m.view(1000, false);
    assert!(!v.v2 && v.chips.is_empty());
    assert!(matches!(v.ask, AskGate::Disabled(_)));
    // status.v2 capability without health (or health without the capability) is not v2
    let s: StatusResponse = serde_json::from_value(json!({"service": "ok", "version": "0.1.0",
        "worker": {"state": "up", "restarts": 0}, "busy": false, "capabilities": ["status.v2"]}))
    .unwrap();
    assert!(!model_with(s).view(0, false).v2);
    // legacy sources: today's labels, no readiness claims
    let src: SourceInfo = serde_json::from_value(json!({"id": "s", "display_name": "Docs", "path": "/x/Docs", "status": "active", "files": 3})).unwrap();
    let sv = source_view(&src, false);
    assert_eq!(sv.readiness, "Ready");
    assert_eq!(sv.legacy_detail.as_deref(), Some("3 files"));
    assert!(sv.counts_line.is_none());
}

// ---- sources ----

fn src(readiness: &str, anomalies: Vec<&str>, counts: Value, status: &str) -> SourceInfo {
    serde_json::from_value(
        json!({"id": "s1", "display_name": "Docs", "path": "/x/Docs", "status": status,
        "files": 3, "readiness": readiness, "anomalies": anomalies, "counts": counts}),
    )
    .unwrap()
}

fn counts(versions: Value, job: Value, history: Value) -> Value {
    json!({"snapshot_ms": 1, "versions": versions, "queue": {"known": true, "queued": 0, "running": 0, "dead": 0},
           "job": job, "history": history,
           "basis": {"skipped_by_code": "latest_whole_source_job", "writer_lock": "free"}})
}

fn no_job() -> Value {
    json!({"known": false, "reason": "no_whole_source_job"})
}

#[test]
fn source_ready_line_with_unknown_skipped_and_superseded_apart() {
    let s = src(
        "ready",
        vec![],
        counts(
            vc(3, 0, 0, 0, 0),
            no_job(),
            json!({"known": true, "unit": "superseded_evidence_version", "superseded": 2}),
        ),
        "active",
    );
    let v = source_view(&s, true);
    assert_eq!(v.readiness, "Ready");
    assert_eq!(v.tone, Tone::Good);
    assert_eq!(
        v.counts_line.as_deref(),
        Some("ready 3 · keyword-only 0 · pending 0 · skipped unknown · failed 0 · empty 0")
    );
    assert_eq!(
        v.superseded.as_deref(),
        Some("2 superseded versions kept (history, not counted above)")
    );
    let t = v.tooltip.join("\n");
    assert!(t.contains("Counts reconcile (same snapshot)"), "{t}");
    assert!(t.contains("Skipped and failed-by-reason: unknown"), "{t}");
}

#[test]
fn unknown_counts_are_unknown_never_zero() {
    let s = src(
        "unknown",
        vec![],
        counts(
            json!({"known": false, "reason": "read_failed"}),
            no_job(),
            json!({"known": false}),
        ),
        "active",
    );
    let v = source_view(&s, true);
    assert_eq!(v.readiness, "unknown");
    let line = v.counts_line.unwrap();
    assert_eq!(line, "ready unknown · keyword-only unknown · pending unknown · skipped unknown · failed unknown · empty unknown");
    assert!(v.superseded.is_none(), "unknown history is not shown as 0");
    assert!(v.tooltip.join("\n").contains("Version counts unknown"));
}

#[test]
fn reconciles_false_never_claims_reconciliation() {
    let mut vcn = vc(3, 0, 0, 0, 0);
    vcn["reconciles"] = json!(false);
    let v = source_view(
        &src("ready", vec![], counts(vcn, no_job(), json!({})), "active"),
        true,
    );
    let t = v.tooltip.join("\n").to_lowercase();
    assert!(!t.contains("counts reconcile"), "{t}");
}

#[test]
fn zero_result_indexing_is_not_ready() {
    let s = src(
        "indexing",
        vec![],
        counts(vc(0, 0, 0, 5, 0), no_job(), json!({})),
        "active",
    );
    let v = source_view(&s, true);
    assert_eq!(v.readiness, "Indexing, no usable files yet");
    assert_ne!(v.tone, Tone::Good);
}

#[test]
fn partial_ingestion_and_skipped_by_code_breakdown() {
    let job = json!({"known": true, "unit": "ingestion_file_result_of_one_job", "status": "partial", "files": 9,
        "outcomes": {}, "skipped_by_code": {"unsupported_type": 3, "too_large": 1},
        "counted_only": {"unsupported_other": 4}, "failed_by_code": {"password_protected": 1, "corrupt_file": 2}, "reconciles": true});
    let s = src(
        "partial",
        vec!["job_partial", "failed_versions"],
        counts(vc(4, 0, 0, 0, 3), job, json!({})),
        "active",
    );
    let v = source_view(&s, true);
    assert_eq!(v.readiness, "Partly ready");
    assert_eq!(v.tone, Tone::Warn);
    assert!(v
        .counts_line
        .as_ref()
        .unwrap()
        .contains("skipped 8 · failed 3"));
    assert_eq!(
        v.skipped[0],
        ("other file types Docket does not read".to_string(), 4)
    );
    assert!(v
        .skipped
        .contains(&("unsupported file type".to_string(), 3)));
    assert!(v.failed.contains(&("corrupt file".to_string(), 2)));
    assert!(v.failed.contains(&("password-protected".to_string(), 1)));
    assert_eq!(
        v.anomalies,
        ["the last ingest was partial", "some files failed"]
    );
}

#[test]
fn unsupported_formats_only_source_is_not_ready() {
    // Everything skipped by code, nothing usable: the daemon says failed/empty.
    let job = json!({"known": true, "status": "succeeded", "files": 5, "skipped_by_code": {"unsupported_type": 5}, "reconciles": true});
    let v = source_view(
        &src(
            "empty",
            vec![],
            counts(vc(0, 0, 0, 0, 0), job, json!({})),
            "active",
        ),
        true,
    );
    assert_eq!(v.readiness, "Empty, nothing to index");
    assert!(v.counts_line.unwrap().contains("skipped 5"));
    assert_eq!(v.skipped, [("unsupported file type".to_string(), 5)]);
}

#[test]
fn empty_ready_is_never_shown_as_ready_even_if_the_daemon_says_ready() {
    let s = src(
        "ready",
        vec![],
        counts(vc(2, 0, 1, 0, 0), no_job(), json!({})),
        "active",
    );
    let v = source_view(&s, true);
    assert_eq!(v.readiness, "Partly ready");
    let s = src(
        "partial",
        vec!["empty_ready"],
        counts(vc(2, 0, 1, 0, 0), no_job(), json!({})),
        "active",
    );
    let v = source_view(&s, true);
    assert_eq!(v.readiness, "Partly ready");
    assert!(v.anomalies[0].contains("no searchable passages"));
    assert!(v.counts_line.unwrap().ends_with("empty 1"));
    // "ready" with no usable count behind it is not confirmed
    let s = src(
        "ready",
        vec![],
        counts(json!({"known": false}), no_job(), json!({})),
        "active",
    );
    assert_ne!(source_view(&s, true).readiness, "Ready");
}

#[test]
fn revoked_and_tombstoned_sources_are_not_ready_and_have_no_equation() {
    for (status, label) in [
        ("revoked", "Disconnected"),
        ("tombstoned", "Removed"),
        ("missing", "Folder missing"),
    ] {
        let s = src(
            "unavailable",
            vec![],
            counts(
                json!({"known": false, "reason": "source_inactive"}),
                no_job(),
                json!({}),
            ),
            status,
        );
        let v = source_view(&s, true);
        assert_eq!(v.readiness, label);
        assert!(v.counts_line.is_none());
        assert_eq!(v.tone, Tone::Warn);
    }
}

#[test]
fn source_list_keeps_last_good_and_shows_its_age() {
    let mut l = SourcesState::default();
    assert!(l.note(0, false).is_none());
    l.apply(Ok(vec![src("ready", vec![], json!({}), "active")]), 1_000);
    assert!(l.note(5_000, false).is_none(), "current: say nothing");
    assert_eq!(l.note(5_000, true).as_deref(), Some("As of 4 s ago"));
    assert_eq!(l.note(91_000, false).as_deref(), Some("As of 1 min ago"));
    l.apply(Err(()), 20_000);
    assert_eq!(l.list.len(), 1, "failed refresh does not blank the list");
    assert!(l.note(20_000, false).unwrap().contains("Could not refresh"));
    assert!(l.is_old(20_000));
    l.apply(Ok(vec![]), 21_000);
    assert!(!l.is_old(21_000));
}

// ---- answer metadata ----

fn result(extra: Value) -> AskResult {
    let mut v = json!({"answer": "x", "abstained": false});
    v.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    serde_json::from_value(v).unwrap()
}

#[test]
fn abstention_reason_absent_is_not_recorded() {
    let m = answer_meta(&result(json!({"abstained": true})));
    assert_eq!(m.abstention.as_deref(), Some("not recorded"));
    let m = answer_meta(&result(
        json!({"abstained": true, "abstention_reason": "NO_EVIDENCE"}),
    ));
    assert_eq!(
        m.abstention.as_deref(),
        Some("no evidence found (NO_EVIDENCE)")
    );
    // not abstained: no abstention text at all
    assert!(
        answer_meta(&result(json!({"abstention_reason": "NO_EVIDENCE"})))
            .abstention
            .is_none()
    );
}

#[test]
fn warnings_coverage_and_scope_come_only_from_fields() {
    let none = answer_meta(&result(json!({})));
    assert!(none.warnings.is_empty() && none.coverage.is_none() && none.scope_files.is_none());
    let m = answer_meta(&result(
        json!({"warning_codes": ["no_citations", "keyword_only_evidence", "no_citations"],
        "index_coverage": {"indexed": 3, "total": 5, "keyword_only": 2, "used_provisional": true},
        "scope_file_count": 4}),
    ));
    assert_eq!(
        m.warnings,
        [
            "the answer has no citations",
            "the answer used keyword-only passages"
        ]
    );
    assert_eq!(
        m.coverage.as_deref(),
        Some("Index coverage: 3 of 5 indexed · 2 keyword-only · answer used keyword-only passages")
    );
    assert_eq!(m.scope_files.as_deref(), Some("Files in scope: 4"));
    let m = answer_meta(&result(
        json!({"index_coverage": {"indexed": 5, "total": 5}}),
    ));
    assert_eq!(
        m.coverage.as_deref(),
        Some("Index coverage: 5 of 5 indexed · 0 keyword-only")
    );
}

// ---- privacy ----

#[test]
fn hostile_codes_and_paths_never_reach_rendered_strings() {
    // Every string-ish field carries the hostile value.
    let mut s = good_sections();
    s["models"] = sec(
        "degraded",
        HOSTILE,
        100,
        false,
        json!({"ollama": "yes",
        "answer": {"name": HOSTILE, "installed": "yes", "reachable": "yes", "resident": "no"},
        "embed": {"name": HOSTILE, "installed": "yes", "reachable": "yes", "resident": "no"}}),
    );
    s["database"] = sec(
        "degraded",
        HOSTILE,
        100,
        false,
        json!({"readable": "yes", "revision": HOSTILE, "at_head": "no"}),
    );
    s["indexing"] = sec(
        "degraded",
        HOSTILE,
        100,
        false,
        json!({"versions": vc(1,0,0,0,0), "writer": HOSTILE}),
    );
    let mut st = status_with("down", false, "unresponsive", s, Some(HOSTILE));
    st.version = HOSTILE.into();
    {
        let h = st.health.as_mut().unwrap();
        h.worker_process.last_error_code = Some(HOSTILE.into());
    }
    let v = model_with(st).view(0, false);
    let t = all_text(&v);
    no_leak(&t);
    assert!(
        t.contains(UNRECOGNISED),
        "unknown codes render generically: {t}"
    );
    // errors never carry a message
    for e in [
        FetchError::Unreachable,
        FetchError::Http(502),
        FetchError::Protocol,
    ] {
        no_leak(&e.to_string());
    }

    // sources: hostile anomaly and code keys, hostile name/path fields
    let job = json!({"known": true, "status": "partial", "skipped_by_code": {HOSTILE: 2, "too_large": 1},
        "failed_by_code": {HOSTILE: 1}, "counted_only": {HOSTILE: 1}, "reconciles": true});
    let mut s = src(
        "partial",
        vec![HOSTILE, "job_partial"],
        counts(vc(1, 0, 0, 0, 0), job, json!({})),
        "active",
    );
    s.display_name = "Docs".into();
    s.path = HOSTILE.into();
    let v = source_view(&s, true);
    let mut t = v.readiness.clone() + &v.counts_line.clone().unwrap() + &v.tooltip.join(" ");
    for a in &v.anomalies {
        t += a;
    }
    for (l, n) in v.skipped.iter().chain(v.failed.iter()) {
        t += l;
        t += &n.to_string();
    }
    no_leak(&t);
    assert!(
        v.skipped.iter().any(|(l, n)| l == UNRECOGNISED && *n == 3),
        "unknown skip codes merge generically: {:?}",
        v.skipped
    );
    assert!(v.failed.contains(&(UNRECOGNISED.to_string(), 1)));

    // answer metadata
    let m = answer_meta(&result(
        json!({"abstained": true, "abstention_reason": HOSTILE,
        "warning_codes": [HOSTILE], "scope_label": HOSTILE}),
    ));
    let t = format!("{:?}", m);
    no_leak(&t);
    assert!(m.warnings[0] == UNRECOGNISED);
}

#[test]
fn nothing_claims_read_only_enforcement() {
    let v = model_with(healthy()).view(0, false);
    let t = all_text(&v).to_lowercase();
    assert!(!t.contains("read-only") && !t.contains("sandbox") && !t.contains("enforced"));
}

#[test]
fn fmt_age_boundaries() {
    assert_eq!(fmt_age(0), "0 s ago");
    assert_eq!(fmt_age(89_999), "89 s ago");
    assert_eq!(fmt_age(90_000), "1 min ago");
    assert_eq!(fmt_age(6 * 3_600_000), "6 h ago");
}
