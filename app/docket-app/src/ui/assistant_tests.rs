//! C3 lifecycle and interaction checks against the live UI event boundary.
use super::tests::{app, draw};
use super::*;
use docket_proto::api::{Citation, Phase, StatusResponse};
use serde_json::json;

fn online(a: &mut DocketApp, tasks: bool, modern: bool) {
    let mut caps = vec![];
    if tasks {
        caps.push("tasks.v1");
    }
    if modern {
        caps.push("status.v2");
    }
    let s: StatusResponse = serde_json::from_value(json!({"service":"ok","version":"fixture","busy":false,"capabilities":caps,"worker":{"state":"up","restarts":0,"model":"fixture","chunks":3},"health":{"v":1,"worker_process":{"state":"up","restarts":0,"crashes":0,"since_ms":0,"checked_at_ms":0,"responsiveness":"responsive"},"refresh_interval_ms":15000,"stale_after_ms":45000,"refresh_skipped_busy":0}})).unwrap();
    a.apply_event(Event::Status(Ok(s)), a.now_ms());
}
fn result() -> AskResult {
    AskResult {
        answer: "The synthetic rate is 2,650 [rates#1].\n\nA second paragraph.".into(),
        citations: vec![Citation {
            label: "[rates#1]".into(),
            source_display_name: "Synthetic rates".into(),
            chunk_id: "synthetic-1".into(),
            excerpt: "Synthetic rate: 2,650".into(),
        }],
        ..Default::default()
    }
}
fn key(key: egui::Key, modifiers: egui::Modifiers) -> egui::Event {
    egui::Event::Key {
        key,
        physical_key: None,
        pressed: true,
        repeat: false,
        modifiers,
    }
}
fn start(a: &mut DocketApp) {
    online(a, true, true);
    assert!(a.begin_question().is_some());
}

#[test]
fn early_stop_discards_late_result_without_claiming_worker_termination() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    start(&mut a);
    a.input = "My next draft".into();
    a.stop();
    a.stop();
    assert!(a.asking.as_ref().unwrap().0.stop_requested);
    a.apply_event(Event::Delta("late draft".into()), 0);
    assert!(a.asking.as_ref().unwrap().0.text.is_empty());
    a.apply_event(Event::Answer(Box::new(result()), 0.2), 0);
    assert!(matches!(a.messages.last(), Some(Message::Discarded)));
    assert_eq!(a.input, "My next draft");
    a.apply_event(Event::Cancelled, 0);
    a.apply_event(
        Event::StopFailed {
            task_id: "old".into(),
            message: "late failure".into(),
        },
        0,
    );
    assert!(matches!(a.messages.last(), Some(Message::Discarded)));
    assert!(a.stop_error.is_none());
    draw(&mut a, &ctx, [800.0, 600.0], vec![]);
    let text = draw(&mut a, &ctx, [800.0, 600.0], vec![]);
    assert!(text.contains("before cancellation was confirmed"), "{text}");
    assert!(!text.contains("Docket ended its answering process"));
    assert!(!text.contains("The synthetic rate"));
}

#[test]
fn completed_answer_is_not_relabelled_by_later_stop_or_terminal_events() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    start(&mut a);
    a.apply_event(Event::Answer(Box::new(result()), 0.2), 0);
    a.stop();
    let len = a.messages.len();
    a.apply_event(Event::Cancelled, 0);
    a.apply_event(Event::AskFailed("late".into()), 0);
    a.apply_event(Event::Answer(Box::new(result()), 0.2), 0);
    assert_eq!(a.messages.len(), len);
    assert!(matches!(a.messages.last(), Some(Message::Answer { .. })));
}

#[test]
fn stop_failed_keeps_task_retry_and_legitimate_result_path() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    start(&mut a);
    a.input = "Preserved".into();
    a.stop();
    a.asking.as_mut().unwrap().0.set_task("task-1".into());
    a.apply_event(
        Event::StopFailed {
            task_id: "task-1".into(),
            message: "could not stop: retry".into(),
        },
        0,
    );
    assert!(!a.asking.as_ref().unwrap().0.stop_requested);
    assert!(a.stop_error.as_deref().unwrap().contains("retry"));
    online(&mut a, true, true); // ordinary polling must not erase cancel feedback
    assert!(a.stop_error.is_some());
    a.apply_event(Event::Answer(Box::new(result()), 0.2), 0);
    assert!(matches!(a.messages.last(), Some(Message::Answer { .. })));
    assert_eq!(a.input, "Preserved");
    assert!(a.stop_error.is_none());
    assert!(a.begin_question().is_some());
    a.stop();
    a.asking.as_mut().unwrap().0.set_task("task-2".into());
    a.apply_event(
        Event::StopFailed {
            task_id: "task-2".into(),
            message: "retry".into(),
        },
        0,
    );
    a.stop();
    assert!(a.asking.as_ref().unwrap().0.stop_requested);
    assert!(a.stop_error.is_none());
}

#[test]
fn confirmed_cancel_timeout_and_recovery_preserve_next_draft_and_scope() {
    for fail in [false, true] {
        let ctx = egui::Context::default();
        let mut a = app(&ctx);
        start(&mut a);
        let scope = a.submitted_scopes.values().next().unwrap().clone();
        a.input = "Next question\nwith a newline".into();
        a.select_scope(None);
        if fail {
            a.apply_event(Event::AskFailed("Question timed out".into()), 0);
        } else {
            a.stop();
            a.apply_event(Event::Cancelled, 0);
        }
        assert!(a.asking.is_none());
        assert_eq!(a.scope, None);
        assert_eq!(a.submitted_scopes.values().next().unwrap(), &scope);
        let (q, id) = a.begin_question().unwrap();
        assert!(q.contains('\n'));
        assert_eq!(id, None);
    }
}

#[test]
fn request_transport_capability_does_not_follow_later_status_polls() {
    for tasks in [false, true] {
        let ctx = egui::Context::default();
        let mut a = app(&ctx);
        online(&mut a, tasks, false);
        a.begin_question().unwrap();
        assert_eq!(a.active_tasks, tasks);
        online(&mut a, !tasks, true);
        assert_eq!(a.active_tasks, tasks);
        a.stop();
        assert_eq!(a.asking.as_ref().unwrap().0.stop_requested, tasks);
    }
}

#[test]
fn provisional_output_has_no_citation_actions_and_final_result_preserves_metadata() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    start(&mut a);
    a.apply_event(Event::Phase(Phase::Validating), 0);
    a.apply_event(Event::Delta("Provisional [rates#1]".into()), 0);
    draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    let text = draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    assert!(text.contains("Draft · checking citations"));
    assert!(!text.contains("Copy answer with sources"));
    assert!(!text.contains("Open passage"));
    let r = result();
    a.apply_event(Event::Answer(Box::new(r.clone()), 0.5), 0);
    assert!(matches!(a.messages.last(),Some(Message::Answer{result,..}) if result==&r));
    draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    let text = draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    assert!(text.contains("Copy answer with sources"));
    assert!(text.contains("Synthetic rates"));
    assert!(!text.contains("Provisional"));
    let copy = DocketApp::copy_answer(&r);
    for actual in [&r.answer, &r.citations[0].label, &r.citations[0].chunk_id] {
        assert!(copy.contains(actual));
    }
}

#[test]
fn empty_cached_excerpt_never_diagnoses_deletion_or_claims_rechecked_access() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    a.messages.clear();
    let mut r = result();
    r.citations[0].excerpt.clear();
    a.messages.push(Message::Answer {
        result: r,
        wall_s: 0.1,
    });
    a.open_cite = Some((0, 0));
    draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    let text = draw(&mut a, &ctx, [1440.0, 900.0], vec![]);
    assert!(text.contains("Passage not available in this answer"));
    assert!(text.contains("Current access has not been rechecked"));
    assert!(!text.contains("no longer available"));
}

#[test]
fn composer_stays_editable_during_streaming_in_both_themes_and_all_sizes() {
    for theme in [Theme::Obsidian, Theme::Porcelain] {
        for size in [[1440.0, 900.0], [800.0, 600.0], [640.0, 420.0]] {
            let ctx = egui::Context::default();
            theme.apply(&ctx);
            let mut a = app(&ctx);
            a.shell.theme = theme;
            start(&mut a);
            a.focus_input = true;
            draw(&mut a, &ctx, size, vec![]);
            draw(&mut a, &ctx, size, vec![]);
            draw(
                &mut a,
                &ctx,
                size,
                vec![egui::Event::Text("Next editable draft".into())],
            );
            assert_eq!(a.input, "Next editable draft", "{theme:?} {size:?}");
            let text = draw(&mut a, &ctx, size, vec![]);
            assert!(text.contains("Stop · Ctrl Shift X"), "{size:?}: {text}");
            draw(
                &mut a,
                &ctx,
                size,
                vec![key(egui::Key::Enter, egui::Modifiers::NONE)],
            );
            assert!(a.input.contains('\n'));
            let count = a.messages.len();
            draw(
                &mut a,
                &ctx,
                size,
                vec![key(egui::Key::Enter, egui::Modifiers::CTRL)],
            );
            assert_eq!(
                a.messages.len(),
                count,
                "Ctrl Enter cannot send a second task"
            );
        }
    }
}

#[test]
fn shortcuts_do_not_submit_from_noncomposer_or_through_modal_and_escape_never_stops() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    online(&mut a, true, true);
    let count = a.messages.len();
    draw(
        &mut a,
        &ctx,
        [800.0, 600.0],
        vec![key(egui::Key::Enter, egui::Modifiers::CTRL)],
    );
    assert_eq!(count, a.messages.len());
    a.shell_command(Command::Settings, &ctx);
    a.focus_input = true;
    draw(
        &mut a,
        &ctx,
        [800.0, 600.0],
        vec![key(egui::Key::Enter, egui::Modifiers::CTRL)],
    );
    assert!(a.asking.is_none());
    draw(
        &mut a,
        &ctx,
        [800.0, 600.0],
        vec![key(egui::Key::Escape, egui::Modifiers::NONE)],
    );
    start(&mut a);
    draw(
        &mut a,
        &ctx,
        [800.0, 600.0],
        vec![key(egui::Key::Escape, egui::Modifiers::NONE)],
    );
    assert!(!a.asking.as_ref().unwrap().0.stop_requested);
}

#[test]
fn late_cancel_failure_cannot_reset_a_new_tasks_stop_intent() {
    let ctx = egui::Context::default();
    let mut a = app(&ctx);
    start(&mut a);
    a.apply_event(Event::Answer(Box::new(result()), 0.1), 0);
    a.input = "Next".into();
    a.begin_question().unwrap();
    let pending = &mut a.asking.as_mut().unwrap().0;
    pending.set_task("new-task".into());
    pending.request_stop();
    a.apply_event(
        Event::StopFailed {
            task_id: "old-task".into(),
            message: "old failure".into(),
        },
        0,
    );
    assert!(a.asking.as_ref().unwrap().0.stop_requested);
    assert!(a.stop_error.is_none());
}

#[test]
fn long_next_draft_scrolls_without_hiding_stop_or_losing_text() {
    for size in [[1440.0, 900.0], [640.0, 420.0]] {
        let ctx = egui::Context::default();
        let mut a = app(&ctx);
        start(&mut a);
        let draft = (0..40)
            .map(|n| format!("Next-question line {n}\n"))
            .collect::<String>();
        a.input = draft.clone();
        draw(&mut a, &ctx, size, vec![]);
        let text = draw(&mut a, &ctx, size, vec![]);
        assert!(text.contains("Stop · Ctrl Shift X"), "{size:?}: {text}");
        assert!(text.contains("Service:"), "{size:?}: {text}");
        assert_eq!(a.input, draft);
        a.apply_event(Event::Cancelled, 0);
        assert_eq!(a.input, draft);
    }
}
