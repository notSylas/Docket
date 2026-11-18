//! C2: inject real typed transport events; no model, data library or worker launch.
use super::tests::{app, draw};
use super::*;
use crate::status_view::FetchError;
use docket_proto::api::{SourceInfo, StatusResponse};
use serde_json::json;
use std::time::Duration;

fn source(id: &str) -> SourceInfo {
    serde_json::from_value(json!({"id":id,"display_name":format!("Source {id}"),"path":"/fixture-only","status":"active","files":0})).unwrap()
}
fn status(v2: bool, worker: WorkerState, busy: bool) -> StatusResponse {
    let mut value = json!({"service":"ok","version":"fixture","busy":busy,"worker":{"state":worker,"restarts":0,"model":"fixture","chunks":3}});
    if v2 {
        value["capabilities"] = json!(["tasks.v1", "status.v2"]);
        value["health"] = json!({"v":1,"worker_process":{"state":worker,"restarts":0,"crashes":0,"since_ms":0,"checked_at_ms":0,"responsiveness":"responsive"},"refresh_interval_ms":15000,"stale_after_ms":45000,"refresh_skipped_busy":0});
    }
    serde_json::from_value(value).unwrap()
}
fn online(app: &mut DocketApp, v2: bool) {
    app.apply_event(
        Event::Status(Ok(status(v2, WorkerState::Up, false))),
        app.now_ms(),
    );
}
fn library_text(app: &mut DocketApp, ctx: &egui::Context) -> String {
    app.shell.destination = Destination::Library;
    draw(app, ctx, [1440.0, 900.0], vec![]);
    draw(app, ctx, [1440.0, 900.0], vec![])
}

#[test]
fn disappeared_source_never_widens_and_reappearance_requires_explicit_selection() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, true);
    a.apply_event(Event::Sources(Ok(vec![source("a"), source("b")])), 0);
    a.select_scope(Some("a".into()));
    a.apply_event(Event::Sources(Ok(vec![source("b")])), 0);
    assert_eq!(a.scope.as_deref(), Some("a"));
    assert_eq!(a.ask_off_reason().as_deref(), Some(SCOPE_UNAVAILABLE));
    assert!(!a.can_ask());
    let draft = a.input.clone();
    assert!(a.begin_question().is_none());
    assert_eq!(a.input, draft);
    assert!(library_text(&mut a, &ctx).contains("Selected source unavailable"));
    a.apply_event(Event::Sources(Ok(vec![source("a"), source("b")])), 0);
    assert!(
        !a.can_ask(),
        "a reappeared ID does not silently clear invalidation"
    );
    a.select_scope(Some("b".into()));
    assert!(a.can_ask());
    a.select_scope(None);
    assert!(a.can_ask());
}

#[test]
fn inactive_lifecycle_is_unavailable_but_unknown_or_partial_readiness_is_allowed() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, true);
    for lifecycle in ["revoked", "missing", "tombstoned"] {
        let mut s = source("a");
        s.status = lifecycle.into();
        a.select_scope(Some("a".into()));
        a.apply_event(Event::Sources(Ok(vec![s])), 0);
        assert!(!a.can_ask());
        assert!(a.scope_unavailable());
    }
    for readiness in [
        None,
        Some(docket_proto::status::Readiness::Unknown),
        Some(docket_proto::status::Readiness::Partial),
        Some(docket_proto::status::Readiness::Indexing),
    ] {
        let mut s = source("a");
        s.readiness = readiness;
        a.apply_event(Event::Sources(Ok(vec![s.clone()])), 0);
        a.select_scope(Some("a".into()));
        assert!(
            a.can_ask(),
            "lifecycle active is usable for routing; readiness is not a fabricated admission rule"
        );
        if readiness.is_none() {
            assert_eq!(status_view::source_view(&s, true).readiness, "unknown");
        }
    }
}

#[test]
fn submitted_scope_is_captured_for_dispatch_and_conversation_not_next_selection() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, true);
    a.apply_event(Event::Sources(Ok(vec![source("a"), source("b")])), 0);
    a.select_scope(Some("a".into()));
    let index = a.messages.len();
    let (question, dispatched_scope) = a.begin_question().unwrap();
    assert_eq!(question, "Keep my draft");
    assert_eq!(dispatched_scope.as_deref(), Some("a"));
    a.select_scope(Some("b".into()));
    a.apply_event(Event::Sources(Ok(vec![source("b")])), 0);
    assert_eq!(
        a.submitted_scopes[&index],
        SubmittedScope {
            source_id: Some("a".into()),
            label: "Source a".into()
        }
    );
    assert!(a.asking.is_some());
    a.apply_event(Event::Answer(Box::default(), 0.1), 0);
    assert_eq!(a.scope.as_deref(), Some("b"));
    assert_eq!(a.submitted_scopes[&index].source_id, dispatched_scope);
    a.shell.destination = Destination::Assistant;
    draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    assert!(draw(&mut a, &ctx, [1440.0, 900.0], vec![]).contains("Scope at submission: Source a"));
}

#[test]
fn confirmed_empty_loading_failure_and_offline_are_distinct() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, true);
    a.select_scope(None);
    a.src = SourcesState::default();
    assert!(!a.confirmed_empty_library());
    a.apply_event(Event::Sources(Err("/home/private/SECRET".into())), 0);
    let text = library_text(&mut a, &ctx);
    assert!(text.contains("Could not load sources"));
    assert!(!text.contains("Your library is empty"));
    assert!(!text.contains("SECRET"));
    a.apply_event(Event::Sources(Ok(vec![])), 0);
    assert!(a.confirmed_empty_library());
    assert!(!a.can_ask());
    assert!(library_text(&mut a, &ctx).contains("Your library is empty"));
    a.apply_event(Event::Sources(Err("refresh failed SECRET".into())), 0);
    assert!(!a.confirmed_empty_library());
    assert!(!a.can_ask());
    assert!(a
        .ask_off_reason()
        .unwrap()
        .contains("last available source list is empty"));
    let text = library_text(&mut a, &ctx);
    assert!(text.contains("Could not refresh"));
    assert!(!text.contains("Your library is empty"));
    a.apply_event(Event::Sources(Ok(vec![])), 0);
    a.apply_event(Event::Status(Err(FetchError::Unreachable)), 0);
    assert!(!a.confirmed_empty_library());
    assert!(!a.can_ask());
    assert!(a.ask_off_reason().unwrap().contains("not reachable"));
    assert!(!library_text(&mut a, &ctx).contains("Your library is empty"));
}

#[test]
fn failed_refresh_keeps_last_good_source_data_and_receipt_age_even_on_legacy() {
    for v2 in [true, false] {
        let ctx = egui::Context::default();
        let mut a = app(&ctx);
        a.epoch = Instant::now() - Duration::from_secs(60);
        online(&mut a, v2);
        a.select_scope(None);
        a.apply_event(Event::Sources(Ok(vec![source("retained")])), 0);
        a.apply_event(
            Event::Sources(Err("untrusted source path SECRET".into())),
            a.now_ms(),
        );
        assert_eq!(a.src.list.len(), 1);
        assert_eq!(a.src.list[0].id, "retained");
        assert!(a.src.age_ms(a.now_ms()).unwrap() >= 60_000);
        assert!(a.src.is_old(a.now_ms()));
        assert!(
            a.can_ask(),
            "failed source refresh does not invent a global answering veto"
        );
        let text = library_text(&mut a, &ctx);
        assert!(text.contains("Source retained"));
        assert!(text.contains("Could not refresh"));
        assert!(text.contains("60 s ago"));
        assert!(!text.contains("SECRET"));
    }
}

#[test]
fn r2a_ask_reasons_and_locally_elapsed_status_staleness_remain_authoritative() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    a.select_scope(None);
    for (worker, busy) in [
        (WorkerState::Starting, false),
        (WorkerState::Down, false),
        (WorkerState::Up, true),
    ] {
        a.apply_event(Event::Status(Ok(status(true, worker, busy))), a.now_ms());
        let expected = a.status.view(a.now_ms(), false).ask;
        assert_eq!(a.ask_gate(), expected);
        assert!(!a.can_ask());
    }
    online(&mut a, true);
    assert!(a.can_ask());
    a.epoch = Instant::now() - Duration::from_secs(20);
    assert!(
        !a.can_ask(),
        "admission recomputes local status age without waiting for a repaint"
    );
    assert!(a.ask_off_reason().unwrap().contains("not reachable"));
    online(&mut a, true);
    assert!(a.can_ask());
    a.apply_event(Event::Status(Err(FetchError::Protocol)), a.now_ms());
    assert_eq!(a.ask_gate(), a.status.view(a.now_ms(), false).ask);
}

#[test]
fn legacy_daemons_keep_fallback_and_apply_only_local_scope_and_empty_guards() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, false);
    assert!(!a.conn.tasks_v1);
    a.select_scope(Some("test-source".into()));
    assert!(a.can_ask());
    a.apply_event(Event::Sources(Ok(vec![source("other")])), 0);
    assert!(!a.can_ask());
    a.select_scope(None);
    assert!(a.can_ask());
    a.apply_event(Event::Sources(Ok(vec![])), 0);
    assert!(!a.can_ask());
    a.apply_event(
        Event::Status(Ok(status(false, WorkerState::Starting, false))),
        0,
    );
    assert!(a.ask_off_reason().unwrap().contains("starting"));
}

#[test]
fn hostile_source_diagnostics_and_abstention_codes_use_existing_closed_label_models() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, true);
    a.select_scope(None);
    let mut s = source("safe");
    s.anomalies = vec!["/private/SECRET".into()];
    a.apply_event(Event::Sources(Ok(vec![s])), 0);
    a.apply_event(Event::Sources(Err("/private/SECRET".into())), 0);
    assert!(!library_text(&mut a, &ctx).contains("SECRET"));
    let result = AskResult {
        answer: "No supported answer".into(),
        abstained: true,
        abstention_reason: Some("/private/SECRET".into()),
        warning_codes: vec!["/private/SECRET".into()],
        ..Default::default()
    };
    assert!(!format!("{:?}", status_view::answer_meta(&result)).contains("SECRET"));
    a.messages = vec![Message::Answer {
        result,
        wall_s: 0.1,
    }];
    a.shell.destination = Destination::Assistant;
    draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    let text = draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    assert!(!text.contains("SECRET"));
    assert!(text.contains("not a reason this version recognises"));
}

#[test]
fn queued_autoask_does_not_consume_input_after_an_empty_snapshot() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, true);
    a.select_scope(None);
    a.auto_ask = Some("Pending automatic question".into());
    a.apply_event(Event::Sources(Ok(vec![])), 0);
    a.pump();
    assert!(a.auto_ask.is_some());
    assert!(a.asking.is_none());
    assert_eq!(a.input, "Keep my draft");
}

#[test]
fn navigation_and_theme_rendering_do_not_open_backend_connections() {
    use std::os::unix::net::UnixListener;
    let dir = std::env::temp_dir().join(format!(
        "docket-c2-noio-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir(&dir).unwrap();
    let socket = dir.join("socket");
    let listener = UnixListener::bind(&socket).unwrap();
    listener.set_nonblocking(true).unwrap();
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    a.backend = Backend::new(socket.clone(), ctx.clone()); // no poller: detect view-triggered I/O only
    for _ in 0..4 {
        for command in [
            Command::Library,
            Command::Theme,
            Command::Assistant,
            Command::Theme,
        ] {
            a.shell_command(command, &ctx);
            draw(&mut a, &ctx, [800.0, 600.0], vec![]);
        }
    }
    std::thread::sleep(Duration::from_millis(200));
    assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
    drop(listener);
    std::fs::remove_file(socket).unwrap();
    std::fs::remove_dir(dir).unwrap();
}
