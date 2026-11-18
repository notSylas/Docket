use crate::backend::{Backend, Event};
use crate::model::{self, Connection, Message, Pending, Segment};
use crate::shell::{self, Command, Destination, Shell};
use crate::status_view::{
    self, AskGate, Chip, SourceView, SourcesState, StatusModel, StatusView, Tone,
};
use crate::theme::{Theme, Tokens};
use docket_proto::api::{AskResult, WorkerState};
use eframe::egui::{self, Color32, RichText};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Instant;

const SCOPE_UNAVAILABLE: &str =
    "Selected source unavailable. Choose another source or All sources in Library.";

/// Request context captured before dispatch; never follows later UI selection.
#[derive(Debug, Clone, PartialEq, Eq)]
struct SubmittedScope {
    source_id: Option<String>,
    label: String,
}

pub struct DocketApp {
    shell: Shell,
    backend: Backend,
    conn: Connection,
    src: SourcesState,
    status: StatusModel,
    /// Recomputed every frame from `status`.
    view: StatusView,
    epoch: Instant,
    scope: Option<String>,
    /// A successful snapshot invalidated the selection; explicit reselection required.
    scope_invalidated: bool,
    submitted_scopes: BTreeMap<usize, SubmittedScope>,
    input: String,
    messages: Vec<Message>,
    asking: Option<(Pending, Instant)>,
    last_latency: Option<f64>,
    /// Capability captured for the submitted request, independent of later polls.
    active_tasks: bool,
    stop_error: Option<String>,
    /// (message index, position in that answer's citation numbering)
    open_cite: Option<(usize, usize)>,
    focus_input: bool,
    /// `DOCKET_APP_AUTOASK`: ask once as soon as the service is ready (smoke tests).
    auto_ask: Option<String>,
}

fn dot(ui: &mut egui::Ui, color: Color32) {
    let (rect, _) = ui.allocate_exact_size(egui::vec2(10.0, 10.0), egui::Sense::hover());
    ui.painter().circle_filled(rect.center(), 4.5, color);
}

fn tone_color(tone: Tone, t: Tokens) -> Color32 {
    match tone {
        Tone::Good => t.success,
        Tone::Info => t.info,
        Tone::Warn => t.warning,
        Tone::Bad => t.danger,
        Tone::Unknown => t.secondary,
    }
}
fn draw_chip(ui: &mut egui::Ui, chip: &Chip, t: Tokens) {
    let label = RichText::new(chip.display())
        .size(12.0)
        .color(if chip.stale {
            t.secondary
        } else {
            tone_color(chip.tone, t)
        });
    // Each menu is a direct child of the wrapping row, so it moves as one
    // control instead of squeezing its label into the remaining line width.
    ui.menu_button(if chip.stale { label.italics() } else { label }, |ui| {
        ui.set_max_width(320.0);
        for line in &chip.tooltip {
            ui.add(egui::Label::new(line).wrap());
        }
    });
}

fn source_details(ui: &mut egui::Ui, sv: &SourceView, old: bool, t: Tokens) {
    let label = |t: &str| {
        let r = RichText::new(t).small();
        if old {
            r.weak()
        } else {
            r
        }
    };
    ui.label(label(&sv.readiness).color(tone_color(sv.tone, t)));
    for a in &sv.anomalies {
        ui.add(egui::Label::new(RichText::new(a).small().color(t.warning)).wrap());
    }
    match (&sv.counts_line, &sv.legacy_detail) {
        (Some(c), _) => {
            ui.add(egui::Label::new(label(c).weak()).wrap());
        }
        (None, Some(d)) => {
            ui.label(label(d).weak());
        }
        _ => {}
    }
    if let Some(sup) = &sv.superseded {
        ui.add(egui::Label::new(RichText::new(sup).small().weak().italics()).wrap());
    }
    for (title, rows) in [
        ("Skipped by reason", &sv.skipped),
        ("Failed by reason", &sv.failed),
    ] {
        if !rows.is_empty() {
            egui::CollapsingHeader::new(RichText::new(title).small())
                .id_salt((title, &sv.readiness, rows.len()))
                .show(ui, |ui| {
                    for (l, n) in rows {
                        ui.label(RichText::new(format!("{n} · {l}")).small());
                    }
                });
        }
    }
}

fn answer_meta_lines(ui: &mut egui::Ui, m: &status_view::AnswerMeta, t: Tokens) {
    for w in &m.warnings {
        ui.label(RichText::new(format!("Note: {w}")).small().color(t.warning));
    }
    for t in [&m.coverage, &m.scope_files].into_iter().flatten() {
        ui.label(RichText::new(t).small().weak());
    }
}

impl DocketApp {
    pub fn new(cc: &eframe::CreationContext<'_>, socket: PathBuf) -> Self {
        Theme::Obsidian.apply(&cc.egui_ctx);
        let mut backend = Backend::new(socket, cc.egui_ctx.clone());
        backend.start();
        Self {
            shell: Shell::default(),
            backend,
            conn: Connection::default(),
            src: SourcesState::default(),
            status: StatusModel::default(),
            view: StatusModel::default().view(0, false),
            epoch: Instant::now(),
            scope: None,
            scope_invalidated: false,
            submitted_scopes: BTreeMap::new(),
            input: String::new(),
            messages: Vec::new(),
            asking: None,
            last_latency: None,
            active_tasks: false,
            stop_error: None,
            open_cite: None,
            focus_input: true,
            auto_ask: std::env::var("DOCKET_APP_AUTOASK")
                .ok()
                .filter(|q| !q.trim().is_empty()),
        }
    }

    fn now_ms(&self) -> u64 {
        self.epoch.elapsed().as_millis() as u64
    }

    fn pump(&mut self) {
        let now = self.now_ms();
        while let Ok(ev) = self.backend.rx.try_recv() {
            self.apply_event(ev, now);
        }
        // Process the latest source/status events before any automatic ask.
        if self.auto_ask.is_some() && self.can_ask() {
            self.input = self.auto_ask.take().unwrap_or_default();
            self.submit();
        }
    }

    fn apply_event(&mut self, ev: Event, now: u64) {
        match ev {
            Event::Status(s) => {
                self.conn.apply_status(s.clone().map_err(|e| e.to_string()));
                self.status.apply(s, now);
            }
            Event::Sources(Ok(s)) => {
                // A failed fetch never invalidates selection; only an observed
                // snapshot can do so. A later reappearance needs explicit consent.
                if let Some(id) = &self.scope {
                    if !s
                        .iter()
                        .any(|source| &source.id == id && source.status == "active")
                    {
                        self.scope_invalidated = true;
                    }
                }
                self.src.apply(Ok(s), now);
            }
            Event::Sources(Err(_)) => {
                // Transport errors may contain paths or arbitrary messages.
                // SourcesState retains last-good data and supplies safe age text.
                self.src.apply(Err(()), now);
            }
            Event::Phase(p) => {
                if let Some((cur, _)) = &mut self.asking {
                    cur.set_phase(p);
                }
            }
            Event::Delta(t) => {
                if let Some((cur, _)) = &mut self.asking {
                    cur.push_delta(&t);
                }
            }
            Event::Answer(r, wall) => {
                // A result already accepted before Stop is immutable. If Stop
                // won locally, discard this result without inventing a confirmed
                // server cancellation (the server may already have completed).
                if let Some((pending, _)) = self.asking.take() {
                    self.last_latency = Some(wall);
                    self.messages.push(if pending.stop_requested {
                        Message::Discarded
                    } else {
                        Message::Answer {
                            result: *r,
                            wall_s: wall,
                        }
                    });
                    self.stop_error = None;
                }
            }
            Event::AskFailed(e) => {
                if self.asking.take().is_some() {
                    self.messages.push(Message::Error(e));
                    self.stop_error = None;
                }
            }
            Event::TaskStarted(id) => {
                if let Some((cur, _)) = &mut self.asking {
                    if let Some(target) = cur.set_task(id) {
                        self.backend.cancel(target);
                    }
                }
            }
            Event::Cancelled => {
                if self.asking.take().is_some() {
                    self.messages.push(Message::Cancelled);
                    self.stop_error = None;
                }
            }
            Event::StopFailed { task_id, message } => {
                if let Some((cur, _)) = &mut self.asking {
                    // Cancel replies run on another thread: a late failure from
                    // the previous task must never reset this task's Stop intent.
                    if cur.task_id.as_deref() == Some(task_id.as_str()) && cur.stop_requested {
                        cur.stop_failed();
                        self.stop_error = Some(message);
                    }
                }
            }
            Event::DaemonStarted(Err(e)) => self.conn.note = e,
            Event::DaemonStarted(Ok(())) => {}
        }
    }

    fn begin_question(&mut self) -> Option<(String, Option<String>)> {
        let q = self.input.trim().to_string();
        if q.is_empty() || !self.can_ask() || self.shell.modal_open() {
            return None;
        }
        let submitted = SubmittedScope {
            source_id: self.scope.clone(),
            label: self
                .scope
                .as_ref()
                .and_then(|id| self.src.list.iter().find(|s| &s.id == id))
                .map(|s| s.display_name.clone())
                .unwrap_or_else(|| "All sources".into()),
        };
        let source_id = submitted.source_id.clone();
        self.submitted_scopes.insert(self.messages.len(), submitted);
        self.input.clear();
        self.messages.push(Message::User(q.clone()));
        self.asking = Some((Pending::new(), Instant::now()));
        self.active_tasks = self.conn.tasks_v1;
        self.stop_error = None;
        self.open_cite = None;
        Some((q, source_id))
    }

    fn submit(&mut self) {
        if let Some((question, scope)) = self.begin_question() {
            self.backend.ask(question, scope, self.active_tasks);
        }
    }

    fn select_scope(&mut self, id: Option<String>) {
        self.scope = id;
        self.scope_invalidated = false;
    }

    fn scope_unavailable(&self) -> bool {
        self.scope.as_ref().is_some_and(|id| {
            self.scope_invalidated
                || !self
                    .src
                    .list
                    .iter()
                    .any(|s| &s.id == id && s.status == "active")
        })
    }

    fn service_unavailable(&self) -> bool {
        matches!(
            self.status
                .view(self.now_ms(), self.asking.is_some())
                .overall,
            status_view::Overall::Loading
                | status_view::Overall::Unavailable
                | status_view::Overall::Failed
        )
    }

    fn confirmed_empty_library(&self) -> bool {
        let now = self.now_ms();
        self.src.list.is_empty()
            && self.src.age_ms(now).is_some()
            && !self.src.is_old(now)
            && !self.service_unavailable()
    }

    fn source_message(&self) -> Option<String> {
        let now = self.now_ms();
        if let Some(note) = self.src.note(now, self.asking.is_some()) {
            return Some(note);
        }
        match self.src.age_ms(now) {
            None if self.src.is_old(now) => Some(
                "Could not load sources. No library snapshot is available. Try Refresh.".into(),
            ),
            None if self.service_unavailable() => {
                Some("Sources are unavailable while connecting to the service.".into())
            }
            None => Some("Loading sources. The library has not been reported yet.".into()),
            Some(age) if self.service_unavailable() => Some(format!(
                "Service unavailable. Showing the source list received {}.",
                status_view::fmt_age(age)
            )),
            _ => None,
        }
    }

    fn stop(&mut self) {
        if !self.active_tasks || self.shell.modal_open() {
            return;
        }
        self.stop_error = None;
        if let Some((cur, _)) = &mut self.asking {
            if let Some(target) = cur.request_stop() {
                self.backend.cancel(target);
            }
        }
    }

    fn ask_gate(&self) -> AskGate {
        // Re-evaluate from the R2a model, including locally elapsed freshness;
        // a cached view from the previous frame is not admission authority.
        let view = self.status.view(self.now_ms(), self.asking.is_some());
        if let AskGate::Disabled(reason) = view.ask {
            return AskGate::Disabled(reason);
        }
        if self.asking.is_some() {
            return AskGate::Disabled(
                "A question is running. Asking is off until it finishes.".into(),
            );
        }
        if !view.v2 && (!self.conn.service_up || self.conn.worker != Some(WorkerState::Up)) {
            return AskGate::Disabled(
                match self.conn.worker {
                    Some(WorkerState::Starting) => {
                        "Asking is off: the answering process is starting."
                    }
                    Some(WorkerState::Down) => {
                        "Asking is off: the answering process is not running."
                    }
                    _ => "Asking is off: the answering process is unavailable.",
                }
                .into(),
            );
        }
        if self.scope_unavailable() {
            return AskGate::Disabled(SCOPE_UNAVAILABLE.into());
        }
        // Keep a last-known empty library blocked even after a failed refresh,
        // without treating that old snapshot as first-run onboarding.
        if self.src.list.is_empty() && self.src.age_ms(self.now_ms()).is_some() {
            return AskGate::Disabled(
                if self.confirmed_empty_library() {
                    "Your library is empty. Add a source before asking."
                } else {
                    "The last available source list is empty. Refresh Library before asking."
                }
                .into(),
            );
        }
        AskGate::Enabled
    }

    fn can_ask(&self) -> bool {
        self.ask_gate() == AskGate::Enabled
    }

    fn ask_off_reason(&self) -> Option<String> {
        if self.asking.is_some() {
            return None;
        }
        match self.ask_gate() {
            AskGate::Enabled => None,
            AskGate::Disabled(reason) => Some(reason),
        }
    }

    fn library(&mut self, ui: &mut egui::Ui) {
        let t = self.shell.theme.tokens();
        ui.add_space(4.0);
        ui.horizontal(|ui| {
            ui.heading("Library");
            if ui.small_button("Refresh").clicked() {
                self.backend.refresh_sources();
            }
        });
        let all = self.scope.is_none();
        if ui.selectable_label(all, "All sources").clicked() {
            self.select_scope(None);
        }
        ui.separator();
        let now = self.now_ms();
        let v2 = self.view.v2;
        if self.scope_unavailable() {
            ui.add(egui::Label::new(RichText::new(SCOPE_UNAVAILABLE).color(t.warning)).wrap());
        }
        if let Some(note) = self.source_message() {
            ui.add(egui::Label::new(RichText::new(note).small().color(t.secondary)).wrap());
        }
        if self.confirmed_empty_library() {
            ui.label(RichText::new("Your library is empty").strong());
            ui.add(
                egui::Label::new(
                    "Add a source using the docket command line, then refresh Library.",
                )
                .wrap(),
            );
            return;
        }
        if !v2 {
            ui.label(
                RichText::new("Legacy source details; indexing readiness is not reported.")
                    .small()
                    .weak(),
            );
        }
        let old = self.src.is_old(now) || self.service_unavailable();
        let sources = self.src.list.clone();
        egui::ScrollArea::vertical()
            .id_salt("live-library")
            .show(ui, |ui| {
                for s in &sources {
                    let selected = self.scope.as_deref() == Some(s.id.as_str());
                    let sv = status_view::source_view(s, v2);
                    let ready = if v2 {
                        sv.tone == Tone::Good
                    } else {
                        model::source_ready(s)
                    };
                    let resp = ui
                        .scope(|ui| {
                            ui.horizontal(|ui| {
                                dot(
                                    ui,
                                    if v2 {
                                        tone_color(sv.tone, t)
                                    } else if ready {
                                        t.accent
                                    } else {
                                        t.warning
                                    },
                                );
                                ui.vertical(|ui| {
                                    ui.label(RichText::new(&s.display_name).strong());
                                    if v2 {
                                        source_details(ui, &sv, old, t);
                                    } else {
                                        let detail = format!(
                                            "{} · {} files",
                                            model::status_label(&s.status),
                                            s.files
                                        );
                                        ui.label(RichText::new(detail).small().weak());
                                    }
                                });
                            });
                        })
                        .response
                        .interact(egui::Sense::click());
                    if selected {
                        ui.painter().rect_stroke(
                            resp.rect.expand(3.0),
                            4.0,
                            egui::Stroke::new(1.5, t.accent),
                            egui::StrokeKind::Outside,
                        );
                    }
                    // With status.v2 the tooltip carries the counting basis; an
                    // absolute path never appears in status UI.
                    let resp = if v2 {
                        resp.on_hover_ui(|ui| {
                            for l in &sv.tooltip {
                                ui.label(l);
                            }
                            if sv.tooltip.is_empty() {
                                ui.label("No counts reported.");
                            }
                        })
                    } else {
                        resp.on_hover_text(&s.path)
                    };
                    if resp.clicked() {
                        self.select_scope(if selected && !self.scope_invalidated {
                            None
                        } else {
                            Some(s.id.clone())
                        });
                    }
                    ui.add_space(2.0);
                }
            });
    }

    fn status_line(&self, ui: &mut egui::Ui) {
        let t = self.shell.theme.tokens();
        if self.view.v2 {
            return self.status_bar(ui);
        }
        ui.horizontal_wrapped(|ui| {
            let tone = |ok: bool| if ok { t.accent } else { t.danger };
            dot(ui, tone(self.conn.service_up));
            ui.label(if self.conn.service_up {
                "Service connected"
            } else {
                "Service not connected"
            });
            ui.separator();
            let worker_up = self.conn.worker == Some(WorkerState::Up);
            dot(ui, tone(worker_up));
            ui.label(match self.conn.worker {
                Some(WorkerState::Up) => "Worker up",
                Some(WorkerState::Starting) => "Worker starting",
                Some(WorkerState::Down) => "Worker down",
                None => "Worker unknown",
            });
            ui.separator();
            let model = if self.conn.model.is_empty() {
                "-"
            } else {
                self.conn.model.as_str()
            };
            ui.label(format!("Model: {model}"));
            ui.separator();
            match self.last_latency {
                Some(s) => ui.label(format!("Last answer: {s:.1} s")),
                None => ui.label("Last answer: -"),
            };
            if !self.conn.note.is_empty() {
                ui.separator();
                ui.label(RichText::new(&self.conn.note).color(t.warning));
            }
        });
    }

    /// Chips with tooltips: one per fact, never one constant "ok".
    fn status_bar(&self, ui: &mut egui::Ui) {
        let t = self.shell.theme.tokens();
        ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Extend);
        ui.spacing_mut().interact_size.y = 24.0;
        ui.spacing_mut().button_padding = egui::vec2(6.0, 4.0);
        ui.horizontal_wrapped(|ui| {
            for chip in &self.view.chips {
                draw_chip(ui, chip, t);
                ui.separator();
            }
            match self.last_latency {
                Some(s) => ui.label(format!("Last answer: {s:.1} s")),
                None => ui.label("Last answer: -"),
            };
            if !self.conn.note.is_empty() && self.conn.note.starts_with("could not stop") {
                ui.separator();
                ui.label(RichText::new(&self.conn.note).color(t.warning));
            }
        });
    }

    fn banners(&self, ui: &mut egui::Ui) {
        let t = self.shell.theme.tokens();
        for b in &self.view.banners {
            let c = tone_color(b.tone, t);
            egui::Frame::group(ui.style())
                .fill(c.gamma_multiply(0.10))
                .stroke(egui::Stroke::new(1.0, c))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.label(RichText::new(&b.text).color(c));
                });
        }
    }

    fn scope_name(&self) -> String {
        if self.scope_unavailable() {
            return "Selected source unavailable".into();
        }
        self.scope
            .as_ref()
            .and_then(|id| self.src.list.iter().find(|s| &s.id == id))
            .map(|s| s.display_name.clone())
            .unwrap_or_else(|| "All sources".into())
    }

    fn question_view(&self, ui: &mut egui::Ui, index: usize, question: &str, t: Tokens) {
        let width = (ui.available_width() * 0.92).min(660.0);
        ui.horizontal_top(|ui| {
            ui.add_space((ui.available_width() - width).max(0.0));
            egui::Frame::new()
                .fill(t.surface_secondary)
                .corner_radius(10)
                .inner_margin(16)
                .show(ui, |ui| {
                    ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                        ui.set_width((width - 32.0).max(100.0));
                        ui.horizontal_wrapped(|ui| {
                            ui.label(RichText::new("You").size(13.0).strong());
                            if let Some(scope) = self.submitted_scopes.get(&index) {
                                shell::caption(
                                    ui,
                                    format!("Scope at submission: {}", scope.label),
                                    t,
                                );
                            }
                        });
                        ui.add_space(4.0);
                        ui.add(
                            egui::Label::new(
                                RichText::new(question).size(18.0).line_height(Some(25.0)),
                            )
                            .selectable(true)
                            .wrap(),
                        );
                    });
                });
        });
    }

    fn citation_control(
        &mut self,
        ui: &mut egui::Ui,
        idx: usize,
        n: usize,
        title: &str,
        inline: bool,
        t: Tokens,
    ) {
        let open = self.open_cite == Some((idx, n));
        let response = if inline {
            ui.add(
                egui::Button::new(
                    RichText::new(format!("[{}]", n + 1))
                        .size(13.0)
                        .color(t.accent),
                )
                .fill(t.selection)
                .selected(open),
            )
        } else {
            shell::citation_link(ui, n + 1, title, open, t)
        };
        if response
            .on_hover_text(format!("Show answer-time excerpt: {title}"))
            .clicked()
        {
            self.open_cite = if open { None } else { Some((idx, n)) };
        }
    }

    fn cited_prose(
        &mut self,
        ui: &mut egui::Ui,
        idx: usize,
        result: &AskResult,
        order: &[usize],
        t: Tokens,
    ) {
        for (pi, paragraph) in result.answer.split("\n\n").enumerate() {
            let (segments, paragraph_order) = model::layout(paragraph, &result.citations);
            ui.push_id(("paragraph", idx, pi), |ui| {
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing = egui::vec2(4.0, 2.0);
                    ui.spacing_mut().interact_size.y = 28.0;
                    for (si, segment) in segments.iter().enumerate() {
                        match segment {
                            Segment::Text(text) => {
                                ui.add(
                                    egui::Label::new(
                                        RichText::new(*text).size(16.0).line_height(Some(24.0)),
                                    )
                                    .selectable(true)
                                    .wrap(),
                                );
                            }
                            Segment::Cite(n) => {
                                let ci = paragraph_order[*n];
                                let n = order.iter().position(|i| *i == ci).unwrap_or(*n);
                                ui.push_id(si, |ui| {
                                    self.citation_control(
                                        ui,
                                        idx,
                                        n,
                                        &result.citations[ci].source_display_name,
                                        true,
                                        t,
                                    )
                                });
                            }
                        }
                    }
                });
            });
            ui.add_space(10.0);
        }
    }

    fn copy_answer(result: &AskResult) -> String {
        let references = result
            .citations
            .iter()
            .map(|c| format!("{} {} · {}", c.label, c.source_display_name, c.chunk_id))
            .collect::<Vec<_>>()
            .join("\n");
        if references.is_empty() {
            result.answer.clone()
        } else {
            format!(
                "{}\n\nSources (passage identifiers):\n{references}",
                result.answer
            )
        }
    }

    fn answer_card(&mut self, ui: &mut egui::Ui, idx: usize, result: &AskResult, wall_s: f64) {
        let t = self.shell.theme.tokens();
        shell::response_heading(
            ui,
            if result.abstained {
                "Insufficient evidence"
            } else {
                "Library response"
            },
            t,
        );
        ui.add_space(12.0);
        let (_, order) = model::layout(&result.answer, &result.citations);
        if result.abstained {
            ui.label(
                RichText::new("I don't know based on the available evidence.").color(t.warning),
            );
            shell::prose(ui, &result.answer, t);
        } else {
            self.cited_prose(ui, idx, result, &order, t);
        }
        // Render only closed labels supplied by the existing R2a metadata model.
        if self.view.v2 {
            let meta = status_view::answer_meta(result);
            if let Some(reason) = &meta.abstention {
                shell::caption(ui, format!("Reason: {reason}"), t);
            }
            answer_meta_lines(ui, &meta, t);
        }
        if !order.is_empty() {
            ui.add_space(8.0);
            shell::caption(
                ui,
                format!(
                    "Sources · {} · Select to show the answer-time excerpt",
                    order.len()
                ),
                t,
            );
            for (n, ci) in order.iter().enumerate() {
                ui.push_id(("source", idx, n), |ui| {
                    self.citation_control(
                        ui,
                        idx,
                        n,
                        &result.citations[*ci].source_display_name,
                        false,
                        t,
                    )
                });
            }
        }
        if let Some((mi, n)) = self.open_cite {
            if let Some(&ci) = order.get(n).filter(|_| mi == idx) {
                let c = &result.citations[ci];
                // Keep the existing inline excerpt here; the contextual Inspector
                // layout, resizing and focus restoration belong to C4.
                egui::Frame::new().fill(t.evidence).inner_margin(12).corner_radius(6).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal_wrapped(|ui| {
                        ui.label(RichText::new(&c.source_display_name).strong());
                        if ui.small_button("Close excerpt").clicked() { self.open_cite = None; }
                    });
                    shell::caption(ui, format!("Passage ID: {}", c.chunk_id), t);
                    shell::caption(ui, "Excerpt returned with this answer; up to 400 characters. Current access has not been rechecked.", t);
                    shell::prose(ui, if c.excerpt.is_empty() { "Passage not available in this answer" } else { &c.excerpt }, t);
                });
            }
        }
        ui.add_space(8.0);
        if ui.button("Copy answer with sources").clicked() {
            ui.ctx().copy_text(Self::copy_answer(result));
        }
        egui::CollapsingHeader::new("Answer details")
            .id_salt(("answer-details", idx))
            .show(ui, |ui| {
                shell::caption(ui, format!("Completed in {wall_s:.1} s"), t);
                shell::caption(
                    ui,
                    "Citation resolution at answer time does not independently verify every claim.",
                    t,
                );
                for (phase, seconds) in &result.timings.by_phase {
                    shell::caption(ui, format!("{phase}: {seconds:.2} s"), t);
                }
            });
    }

    fn composer(&mut self, ui: &mut egui::Ui) {
        let t = self.shell.theme.tokens();
        let pending = self.asking.is_some();
        let stopping = self.asking.as_ref().is_some_and(|(p, _)| p.stop_requested);
        let reason = self
            .stop_error
            .clone()
            .or_else(|| self.ask_off_reason())
            .or_else(|| self.scope_unavailable().then(|| SCOPE_UNAVAILABLE.into()));
        egui::Panel::bottom("composer")
            .exact_size(if reason.is_some() { 180.0 } else { 150.0 })
            .resizable(false)
            .frame(
                egui::Frame::new()
                    .fill(t.canvas)
                    .inner_margin(egui::Margin::symmetric(16, 8)),
            )
            .show(ui, |ui| {
                let width = ui.available_width().min(800.0);
                ui.horizontal_top(|ui| {
                    ui.add_space(((ui.available_width() - width) / 2.0).max(0.0));
                    ui.vertical(|ui| {
                        ui.set_width(width);
                        ui.spacing_mut().item_spacing.y = 4.0;
                        ui.spacing_mut().interact_size.y = 28.0;
                        ui.spacing_mut().button_padding.y = 5.0;
                        if let Some(reason) = reason {
                            egui::ScrollArea::vertical()
                                .id_salt("composer-reason")
                                .max_height(28.0)
                                .show(ui, |ui| {
                                    ui.add(
                                        egui::Label::new(
                                            RichText::new(reason).small().color(t.warning),
                                        )
                                        .wrap(),
                                    );
                                });
                        }
                        let focused = ui
                            .ctx()
                            .memory(|m| m.has_focus(egui::Id::unique("question-input")));
                        shell::input_frame(t)
                            .stroke(egui::Stroke::new(
                                if focused { 2.0 } else { 1.0 },
                                if focused { t.focus } else { t.control },
                            ))
                            .corner_radius(12)
                            .inner_margin(10)
                            .show(ui, |ui| {
                                let send_shortcut = !self.shell.modal_open()
                                    && ui.ctx().memory(|m| {
                                        m.has_focus(egui::Id::unique("question-input"))
                                    })
                                    && ui.input_mut(|i| {
                                        i.consume_key(egui::Modifiers::CTRL, egui::Key::Enter)
                                    });
                                let response = egui::ScrollArea::vertical()
                                    .id_salt("composer-draft-scroll")
                                    .max_height(40.0)
                                    .min_scrolled_height(40.0)
                                    .auto_shrink([false, false])
                                    .show(ui, |ui| {
                                        ui.add(
                                            egui::TextEdit::multiline(&mut self.input)
                                                .id(egui::Id::unique("question-input"))
                                                .hint_text(if pending {
                                                    "Draft your next question…"
                                                } else {
                                                    "Ask about your library…"
                                                })
                                                .frame(egui::Frame::NONE)
                                                .desired_rows(2)
                                                .margin(0),
                                        )
                                    })
                                    .inner;
                                if self.focus_input && !self.shell.modal_open() {
                                    response.request_focus();
                                    self.focus_input = false;
                                }
                                ui.horizontal(|ui| {
                                    let scope = format!("Next scope: {}", self.scope_name());
                                    let label = ui.add_sized(
                                        [(ui.available_width() - 80.0).max(40.0), 20.0],
                                        egui::Label::new(
                                            RichText::new(&scope).small().color(t.secondary),
                                        )
                                        .truncate(),
                                    );
                                    label.on_hover_text(scope);
                                    if ui.small_button("Change").clicked() {
                                        self.shell_command(Command::Library, ui.ctx());
                                    }
                                });
                                ui.horizontal(|ui| {
                                    let label = if pending && !self.active_tasks {
                                        "Answering · Stop unavailable on this daemon"
                                    } else if stopping {
                                        "Stopping · partial text discarded"
                                    } else if pending {
                                        "Answering · next draft stays editable"
                                    } else {
                                        "Enter for a new line"
                                    };
                                    ui.add(
                                        egui::Label::new(
                                            RichText::new(label).size(12.0).color(t.secondary),
                                        )
                                        .wrap(),
                                    );
                                    ui.with_layout(
                                        egui::Layout::right_to_left(egui::Align::Center),
                                        |ui| {
                                            if pending && self.active_tasks {
                                                if ui
                                                    .add_enabled(
                                                        !stopping,
                                                        egui::Button::new(
                                                            RichText::new(if stopping {
                                                                "Stopping…"
                                                            } else {
                                                                "Stop · Ctrl Shift X"
                                                            })
                                                            .color(t.danger),
                                                        )
                                                        .fill(t.surface_secondary),
                                                    )
                                                    .clicked()
                                                {
                                                    self.stop();
                                                }
                                            } else if shell::primary(
                                                ui,
                                                "Ask · Ctrl Enter",
                                                self.can_ask() && !self.input.trim().is_empty(),
                                                t,
                                            )
                                            .clicked()
                                            {
                                                self.submit();
                                            }
                                        },
                                    );
                                });
                                if send_shortcut && !self.shell.modal_open() {
                                    self.submit();
                                }
                            });
                    });
                });
            });
    }

    fn chat(&mut self, ui: &mut egui::Ui) {
        let t = self.shell.theme.tokens();
        self.composer(ui);
        egui::CentralPanel::default().frame(egui::Frame::new().fill(t.canvas).inner_margin(16)).show(ui, |ui| {
            egui::ScrollArea::vertical().id_salt("conversation").stick_to_bottom(true).auto_shrink([false, false]).show(ui, |ui| {
                let width = ui.available_width().min(800.0);
                ui.horizontal_top(|ui| {
                    ui.add_space(((ui.available_width() - width) / 2.0).max(0.0));
                    ui.vertical(|ui| {
                        ui.set_width(width);
                        if self.messages.is_empty() && self.asking.is_none() {
                            ui.add_space(24.0);
                            ui.heading("A question is a good place to start.");
                            ui.add_space(8.0);
                            shell::prose(ui, "Ask across your sources, then follow the citations to the excerpts returned with your answer.", t);
                            if ui.button("Browse Library").clicked() { self.shell_command(Command::Library, ui.ctx()); }
                        }
                        let msgs = std::mem::take(&mut self.messages);
                        for (i, message) in msgs.iter().enumerate() {
                            ui.push_id(("message", i), |ui| match message {
                                Message::User(q) => self.question_view(ui, i, q, t),
                                Message::Answer { result, wall_s } => self.answer_card(ui, i, result, *wall_s),
                                Message::Cancelled => { shell::response_heading(ui, "Stopped", t); shell::caption(ui, model::STOPPED_TITLE, t); shell::caption(ui, model::STOPPED_NOTE, t); }
                                Message::Discarded => { shell::response_heading(ui, "Response discarded after Stop", t); shell::caption(ui, "The answer completed before cancellation was confirmed. Its text was discarded. Docket cannot confirm that the language model stopped generating.", t); }
                                Message::Error(error) => { shell::response_heading(ui, "Answer unavailable", t); ui.add(egui::Label::new(RichText::new(error).color(t.danger)).wrap()); shell::caption(ui, "Your next draft is kept. Review the local service or edit your question, then try again.", t); }
                            });
                            ui.add_space(24.0);
                        }
                        self.messages = msgs;
                        if let Some((pending, since)) = &self.asking {
                            shell::response_heading(ui, if pending.stop_requested { "Stopping · partial text discarded" } else if pending.is_validating() { "Draft · checking citations" } else { "Draft · citations not checked" }, t);
                            ui.add_space(8.0);
                            ui.horizontal(|ui| { ui.spinner(); shell::caption(ui, format!("{} · {:.0} s", if pending.stop_requested { "Stopping…" } else { pending.phase.label() }, since.elapsed().as_secs_f32()), t); });
                            if pending.is_draft() && !pending.stop_requested { ui.scope(|ui| { ui.visuals_mut().override_text_color = Some(t.secondary); shell::prose(ui, &pending.text, Tokens { text: t.secondary, ..t }); }); }
                            ui.ctx().request_repaint_after(std::time::Duration::from_millis(500));
                        }
                    });
                });
            });
        });
    }
}

impl DocketApp {
    fn shell_command(&mut self, command: Command, ctx: &egui::Context) {
        self.shell.apply(command, ctx);
        match command {
            Command::FocusQuestion => self.focus_input = true,
            Command::Stop if self.asking.is_some() && self.active_tasks => self.stop(),
            _ => {}
        }
    }
    fn render(&mut self, ui: &mut egui::Ui) {
        let ctx = ui.ctx().clone();
        self.pump();
        let now = self.now_ms();
        self.view = self.status.view(now, self.asking.is_some());
        let can_stop =
            self.asking.as_ref().is_some_and(|(p, _)| !p.stop_requested) && self.active_tasks;
        if let Some(command) = self.shell.shortcuts(&ctx, can_stop) {
            self.shell_command(command, &ctx);
        }
        let t = self.shell.theme.tokens();
        let compact = Shell::compact(ctx.content_rect().width());
        if !compact {
            egui::Panel::left("workspace-navigation")
                .exact_size(shell::NAV_WIDTH)
                .resizable(false)
                .frame(egui::Frame::new().fill(t.nav).inner_margin(16))
                .show(ui, |ui| {
                    if let Some(command) = self.shell.navigation(ui) {
                        self.shell_command(command, &ctx);
                    }
                });
        }
        egui::Panel::top("workspace-header")
            .resizable(false)
            .frame(
                egui::Frame::new()
                    .fill(t.canvas)
                    .inner_margin(egui::Margin::symmetric(24, 10)),
            )
            .show(ui, |ui| {
                if let Some(command) = self.shell.header(ui, compact) {
                    self.shell_command(command, &ctx);
                }
            });
        // Bound auxiliary regions to keep existing Ask/Stop controls reachable.
        egui::Panel::bottom("status")
            .exact_size(if compact { 88.0 } else { 48.0 })
            .resizable(false)
            .frame(
                egui::Frame::new()
                    .fill(t.surface_secondary)
                    .inner_margin(egui::Margin::symmetric(16, 6)),
            )
            .show(ui, |ui| {
                egui::ScrollArea::vertical()
                    .id_salt("status-scroll")
                    .max_height(88.0)
                    .show(ui, |ui| self.status_line(ui));
            });
        if !self.view.banners.is_empty() {
            egui::Panel::top("banners")
                .exact_size(
                    (self.view.banners.len() as f32 * 40.0 + 8.0)
                        .min(ctx.content_rect().height() * 0.24),
                )
                .resizable(false)
                .frame(
                    egui::Frame::new()
                        .fill(t.canvas)
                        .inner_margin(egui::Margin::symmetric(8, 4)),
                )
                .show(ui, |ui| {
                    egui::ScrollArea::vertical()
                        .id_salt("banner-scroll")
                        .max_height(72.0)
                        .show(ui, |ui| self.banners(ui));
                });
        }
        self.backend.set_asking(self.asking.is_some());
        ctx.request_repaint_after(std::time::Duration::from_secs(1));
        match self.shell.destination {
            Destination::Assistant => self.chat(ui),
            Destination::Library => {
                egui::CentralPanel::default()
                    .frame(egui::Frame::new().fill(t.canvas).inner_margin(if compact {
                        16
                    } else {
                        shell::GUTTER
                    }))
                    .show(ui, |ui| self.library(ui));
            }
        }
        if let Some(command) = self.shell.dialogs(&ctx, can_stop) {
            self.shell_command(command, &ctx);
        }
    }
}
impl eframe::App for DocketApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.render(ui);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use docket_proto::api::{SourceInfo, StatusResponse};
    use serde_json::json;

    pub(super) fn app(ctx: &egui::Context) -> DocketApp {
        // Unstarted Backend owns no process and performs no I/O.
        let snapshot: StatusResponse = serde_json::from_value(json!({
            "service":"ok", "version":"0.1.0", "busy":false,
            "worker":{"state":"up", "restarts":0, "model":"fixture", "chunks":3},
            "capabilities":["status.v2","tasks.v1"],
            "health":{"v":1,"worker_process":{"state":"up","restarts":0,"crashes":0,"since_ms":0,"checked_at_ms":0,"responsiveness":"responsive"},
                "models":{"state":"ok","code":"ok","age_ms":0,"stale":false,"ollama":"yes","answer":{"name":"fixture","installed":"yes","reachable":"yes","resident":"yes"},"embed":{"name":"fixture","installed":"yes","reachable":"yes","resident":"no"}},
                "database":{"state":"unavailable","code":"db_busy","age_ms":0,"stale":false,"readable":"no","at_head":"yes"},
                "refresh_interval_ms":15000,"stale_after_ms":45000,"refresh_skipped_busy":0}
        })).unwrap();
        let source: SourceInfo = serde_json::from_value(json!({"id":"test-source", "display_name":"Fixture source", "path":"/fixture", "status":"active", "files":3, "readiness":"partial"})).unwrap();
        let mut status = StatusModel::default();
        status.apply(Ok(snapshot.clone()), 0);
        let mut conn = Connection::default();
        conn.apply_status(Ok(snapshot));
        let mut src = SourcesState::default();
        src.apply(Ok(vec![source]), 0);
        DocketApp {
            shell: Shell::default(),
            backend: Backend::new(PathBuf::from("/nonexistent-c1-test-socket"), ctx.clone()),
            conn,
            src,
            view: status.view(0, false),
            status,
            epoch: Instant::now(),
            scope: Some("test-source".into()),
            scope_invalidated: false,
            submitted_scopes: BTreeMap::new(),
            input: "Keep my draft".into(),
            messages: vec![Message::User("Keep my conversation".into())],
            asking: None,
            last_latency: None,
            active_tasks: false,
            stop_error: None,
            open_cite: None,
            focus_input: false,
            auto_ask: None,
        }
    }
    pub(super) fn draw(
        app: &mut DocketApp,
        ctx: &egui::Context,
        size: [f32; 2],
        events: Vec<egui::Event>,
    ) -> String {
        let mut output = ctx.run_ui(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    egui::vec2(size[0], size[1]),
                )),
                events,
                ..Default::default()
            },
            |ui| app.render(ui),
        );
        fn texts(shape: &egui::epaint::Shape, clip: egui::Rect, out: &mut String) {
            match shape {
                egui::epaint::Shape::Text(t)
                    if clip
                        .expand(0.5)
                        .contains_rect(t.galley.rect.translate(t.pos.to_vec2())) =>
                {
                    out.push_str(&t.galley.job.text);
                    out.push('\n');
                }
                egui::epaint::Shape::Vec(v) => {
                    for shape in v {
                        texts(shape, clip, out);
                    }
                }
                _ => {}
            }
        }
        output.textures_delta.clear();
        let mut text = String::new();
        for shape in &output.shapes {
            texts(&shape.shape, shape.clip_rect, &mut text);
        }
        text
    }
    #[test]
    fn both_themes_and_sizes_keep_real_status_library_and_draft() {
        for theme in [Theme::Obsidian, Theme::Porcelain] {
            for size in [[1440.0, 900.0], [800.0, 600.0], [640.0, 420.0]] {
                let ctx = egui::Context::default();
                theme.apply(&ctx);
                let mut app = app(&ctx);
                app.shell.theme = theme;
                for dest in [
                    Destination::Assistant,
                    Destination::Library,
                    Destination::Assistant,
                ] {
                    app.shell.destination = dest;
                    draw(&mut app, &ctx, size, vec![]);
                    let text = draw(&mut app, &ctx, size, vec![]);
                    for name in ["Service:", "Worker:", "Model:", "Index:", "Database:"] {
                        assert!(text.contains(name), "{size:?}: {name}: {text}");
                    }
                    assert!(
                        text.contains("Database: unreadable."),
                        "warning remains visible: {text}"
                    );
                    assert!(text.contains(if dest == Destination::Library {
                        "Fixture source"
                    } else {
                        "Ask"
                    }));
                    assert_eq!(app.input, "Keep my draft");
                    assert_eq!(app.scope.as_deref(), Some("test-source"));
                    assert_eq!(app.messages.len(), 1);
                }
            }
        }
    }
    #[test]
    fn palette_navigation_theme_and_escape_do_not_submit_or_cancel() {
        let ctx = egui::Context::default();
        Theme::Obsidian.apply(&ctx);
        let mut app = app(&ctx);
        let key = |key, modifiers| egui::Event::Key {
            key,
            physical_key: None,
            pressed: true,
            repeat: false,
            modifiers,
        };
        draw(
            &mut app,
            &ctx,
            [800.0, 600.0],
            vec![key(egui::Key::K, egui::Modifiers::CTRL)],
        );
        assert!(app.shell.modal_open());
        draw(
            &mut app,
            &ctx,
            [800.0, 600.0],
            vec![key(egui::Key::Escape, egui::Modifiers::NONE)],
        );
        assert!(!app.shell.modal_open());
        draw(
            &mut app,
            &ctx,
            [800.0, 600.0],
            vec![key(egui::Key::Num2, egui::Modifiers::CTRL)],
        );
        assert_eq!(app.shell.destination, Destination::Library);
        draw(
            &mut app,
            &ctx,
            [800.0, 600.0],
            vec![key(
                egui::Key::T,
                egui::Modifiers::CTRL | egui::Modifiers::SHIFT,
            )],
        );
        assert_eq!(app.shell.theme, Theme::Porcelain);
        assert_eq!(app.shell.destination, Destination::Library);
        assert_eq!(app.input, "Keep my draft");
        assert!(app.asking.is_none());
        assert_eq!(app.messages.len(), 1);
    }
}

#[cfg(test)]
mod state_bridge_tests;

#[cfg(test)]
mod assistant_tests;
