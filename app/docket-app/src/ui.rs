use crate::backend::{Backend, Event};
use crate::model::{self, Connection, Message, Pending, Segment};
use crate::status_view::{
    self, AskGate, Chip, SourceView, SourcesState, StatusModel, StatusView, Tone,
};
use docket_proto::api::{AskResult, WorkerState};
use eframe::egui::{self, Color32, RichText};
use std::path::PathBuf;
use std::time::Instant;

const ACCENT: Color32 = Color32::from_rgb(0x16, 0xA3, 0x4A);
const WARN: Color32 = Color32::from_rgb(0xD9, 0x77, 0x06);
const BAD: Color32 = Color32::from_rgb(0xDC, 0x26, 0x26);
const SPACE: f32 = 16.0;

pub struct DocketApp {
    backend: Backend,
    conn: Connection,
    src: SourcesState,
    sources_note: String,
    status: StatusModel,
    /// Recomputed every frame from `status`.
    view: StatusView,
    epoch: Instant,
    scope: Option<String>,
    input: String,
    messages: Vec<Message>,
    asking: Option<(Pending, Instant)>,
    last_latency: Option<f64>,
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

fn tone_color(t: Tone) -> Color32 {
    match t {
        Tone::Good => ACCENT,
        Tone::Info => Color32::from_rgb(0x25, 0x63, 0xEB),
        Tone::Warn => WARN,
        Tone::Bad => BAD,
        Tone::Unknown => Color32::GRAY,
    }
}

fn draw_chip(ui: &mut egui::Ui, chip: &Chip) {
    let resp = ui
        .scope(|ui| {
            ui.horizontal(|ui| {
                // Old data is dimmed: never drawn like current data.
                let c = tone_color(chip.tone);
                dot(ui, if chip.stale { c.gamma_multiply(0.4) } else { c });
                let text = RichText::new(chip.display());
                ui.label(if chip.stale {
                    text.weak().italics()
                } else {
                    text
                });
            });
        })
        .response;
    resp.on_hover_ui(|ui| {
        for l in &chip.tooltip {
            ui.label(l);
        }
    });
}

fn source_details(ui: &mut egui::Ui, sv: &SourceView, old: bool) {
    let label = |t: &str| {
        let r = RichText::new(t).small();
        if old {
            r.weak()
        } else {
            r
        }
    };
    ui.label(label(&sv.readiness).color(tone_color(sv.tone)));
    for a in &sv.anomalies {
        ui.add(egui::Label::new(RichText::new(a).small().color(WARN)).wrap());
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

fn answer_meta_lines(ui: &mut egui::Ui, m: &status_view::AnswerMeta) {
    for w in &m.warnings {
        ui.label(RichText::new(format!("Note: {w}")).small().color(WARN));
    }
    for t in [&m.coverage, &m.scope_files].into_iter().flatten() {
        ui.label(RichText::new(t).small().weak());
    }
}

fn setup_style(ctx: &egui::Context) {
    use egui::{FontFamily, FontId, TextStyle};
    ctx.global_style_mut(|s| {
        s.text_styles = [
            (
                TextStyle::Heading,
                FontId::new(20.0, FontFamily::Proportional),
            ),
            (TextStyle::Body, FontId::new(15.0, FontFamily::Proportional)),
            (
                TextStyle::Button,
                FontId::new(15.0, FontFamily::Proportional),
            ),
            (
                TextStyle::Small,
                FontId::new(12.5, FontFamily::Proportional),
            ),
            (
                TextStyle::Monospace,
                FontId::new(14.0, FontFamily::Monospace),
            ),
        ]
        .into();
        s.spacing.item_spacing = egui::vec2(8.0, 8.0);
        s.spacing.button_padding = egui::vec2(10.0, 5.0);
        s.visuals.selection.bg_fill = ACCENT.gamma_multiply(0.75);
        s.spacing.window_margin = egui::Margin::same(12);
    });
}

impl DocketApp {
    pub fn new(cc: &eframe::CreationContext<'_>, socket: PathBuf) -> Self {
        setup_style(&cc.egui_ctx);
        let mut backend = Backend::new(socket, cc.egui_ctx.clone());
        backend.start();
        Self {
            backend,
            conn: Connection::default(),
            src: SourcesState::default(),
            sources_note: "Connecting...".into(),
            status: StatusModel::default(),
            view: StatusModel::default().view(0, false),
            epoch: Instant::now(),
            scope: None,
            input: String::new(),
            messages: Vec::new(),
            asking: None,
            last_latency: None,
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
        if self.auto_ask.is_some() && self.can_ask() {
            self.input = self.auto_ask.take().unwrap_or_default();
            self.submit();
        }
        while let Ok(ev) = self.backend.rx.try_recv() {
            match ev {
                Event::Status(s) => {
                    self.conn.apply_status(s.clone().map_err(|e| e.to_string()));
                    self.status.apply(s, now);
                }
                Event::Sources(Ok(s)) => {
                    self.src.apply(Ok(s.clone()), now);
                    self.sources_note = if s.is_empty() {
                        "No folders yet. Add one with the docket command line.".into()
                    } else {
                        String::new()
                    };
                    if let Some(sel) = &self.scope {
                        if !s.iter().any(|x| &x.id == sel) {
                            self.scope = None;
                        }
                    }
                    self.src.list = s;
                }
                Event::Sources(Err(e)) => {
                    self.src.apply(Err(()), now);
                    self.sources_note = e;
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
                    self.asking = None;
                    self.last_latency = Some(wall);
                    self.messages.push(Message::Answer {
                        result: *r,
                        wall_s: wall,
                    });
                    self.focus_input = true;
                }
                Event::AskFailed(e) => {
                    self.asking = None;
                    self.messages.push(Message::Error(e));
                    self.focus_input = true;
                }
                Event::TaskStarted(id) => {
                    if let Some((cur, _)) = &mut self.asking {
                        if let Some(target) = cur.set_task(id) {
                            self.backend.cancel(target);
                        }
                    }
                }
                Event::Cancelled => {
                    self.asking = None;
                    self.messages.push(Message::Cancelled);
                    self.focus_input = true;
                }
                Event::StopFailed(e) => {
                    if let Some((cur, _)) = &mut self.asking {
                        cur.stop_failed();
                    }
                    self.conn.note = e;
                }
                Event::DaemonStarted(Err(e)) => self.conn.note = e,
                Event::DaemonStarted(Ok(())) => {}
            }
        }
    }

    fn submit(&mut self) {
        let q = self.input.trim().to_string();
        if q.is_empty() || self.asking.is_some() {
            return;
        }
        self.input.clear();
        self.messages.push(Message::User(q.clone()));
        self.asking = Some((Pending::new(), Instant::now()));
        self.open_cite = None;
        self.backend.ask(q, self.scope.clone(), self.conn.tasks_v1);
    }

    fn stop(&mut self) {
        if let Some((cur, _)) = &mut self.asking {
            if let Some(target) = cur.request_stop() {
                self.backend.cancel(target);
            }
        }
    }

    fn can_ask(&self) -> bool {
        if self.asking.is_some() {
            return false;
        }
        if self.view.v2 {
            // The status model says why when it is off; see `ask_off_reason`.
            return self.view.ask == AskGate::Enabled;
        }
        self.conn.service_up && self.conn.worker == Some(WorkerState::Up)
    }

    /// Why asking is off, shown instead of a silently disabled input.
    fn ask_off_reason(&self) -> Option<String> {
        if self.asking.is_some() {
            return None;
        }
        if self.view.v2 {
            return match &self.view.ask {
                AskGate::Disabled(t) => Some(t.clone()),
                AskGate::Enabled => None,
            };
        }
        None // old daemon: today's UI, unchanged
    }

    fn library(&mut self, ui: &mut egui::Ui) {
        ui.add_space(4.0);
        ui.horizontal(|ui| {
            ui.heading("Library");
            if ui.small_button("Refresh").clicked() {
                self.backend.refresh_sources();
            }
        });
        let all = self.scope.is_none();
        if ui.selectable_label(all, "All sources").clicked() {
            self.scope = None;
        }
        ui.separator();
        let now = self.now_ms();
        let v2 = self.view.v2;
        if !self.sources_note.is_empty() {
            ui.label(RichText::new(&self.sources_note).small().weak());
        }
        if v2 {
            if let Some(n) = self.src.note(now, self.asking.is_some()) {
                ui.label(RichText::new(n).small().color(WARN));
            }
        }
        let old = v2 && self.src.is_old(now);
        let sources = self.src.list.clone();
        egui::ScrollArea::vertical().show(ui, |ui| {
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
                                    tone_color(sv.tone)
                                } else if ready {
                                    ACCENT
                                } else {
                                    WARN
                                },
                            );
                            ui.vertical(|ui| {
                                ui.label(RichText::new(&s.display_name).strong());
                                if v2 {
                                    source_details(ui, &sv, old);
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
                        egui::Stroke::new(1.5, ACCENT),
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
                    self.scope = if selected { None } else { Some(s.id.clone()) };
                }
                ui.add_space(2.0);
            }
        });
    }

    fn status_line(&self, ui: &mut egui::Ui) {
        if self.view.v2 {
            return self.status_bar(ui);
        }
        ui.horizontal(|ui| {
            let tone = |ok: bool| if ok { ACCENT } else { BAD };
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
                ui.label(RichText::new(&self.conn.note).color(WARN));
            }
        });
    }

    /// Chips with tooltips: one per fact, never one constant "ok".
    fn status_bar(&self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            for chip in &self.view.chips {
                draw_chip(ui, chip);
                ui.separator();
            }
            match self.last_latency {
                Some(s) => ui.label(format!("Last answer: {s:.1} s")),
                None => ui.label("Last answer: -"),
            };
            if !self.conn.note.is_empty() && self.conn.note.starts_with("could not stop") {
                ui.separator();
                ui.label(RichText::new(&self.conn.note).color(WARN));
            }
        });
    }

    fn banners(&self, ui: &mut egui::Ui) {
        for b in &self.view.banners {
            let c = tone_color(b.tone);
            egui::Frame::group(ui.style())
                .fill(c.gamma_multiply(0.10))
                .stroke(egui::Stroke::new(1.0, c))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.label(RichText::new(&b.text).color(c));
                });
        }
    }

    fn answer_card(&mut self, ui: &mut egui::Ui, idx: usize, result: &AskResult, wall_s: f64) {
        let (segs, order) = model::layout(&result.answer, &result.citations);
        // Metadata fields exist only with a status.v2 daemon; nothing is guessed.
        let meta = self.view.v2.then(|| status_view::answer_meta(result));
        if result.abstained {
            egui::Frame::group(ui.style())
                .fill(WARN.gamma_multiply(0.12))
                .stroke(egui::Stroke::new(1.5, WARN))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.label(
                        RichText::new("I don't know based on the available evidence.")
                            .heading()
                            .color(WARN),
                    );
                    let detail = result.answer.trim();
                    if !detail.is_empty() {
                        ui.label(RichText::new(detail).weak());
                    }
                    if let Some(m) = &meta {
                        if let Some(r) = &m.abstention {
                            ui.label(RichText::new(format!("Reason: {r}")).small());
                        }
                        answer_meta_lines(ui, m);
                    }
                });
        } else {
            egui::Frame::group(ui.style()).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing.x = 3.0;
                    for seg in &segs {
                        match seg {
                            Segment::Text(t) => {
                                ui.label(*t);
                            }
                            Segment::Cite(n) => {
                                let c = &result.citations[order[*n]];
                                let open = self.open_cite == Some((idx, *n));
                                let text = format!("{} {}", n + 1, c.source_display_name);
                                let btn =
                                    egui::Button::new(RichText::new(text).small().color(if open {
                                        Color32::WHITE
                                    } else {
                                        ACCENT
                                    }))
                                    .fill(if open {
                                        ACCENT
                                    } else {
                                        ACCENT.gamma_multiply(0.14)
                                    })
                                    .corner_radius(10.0);
                                if ui.add(btn).on_hover_text("Show the passage").clicked() {
                                    self.open_cite = if open { None } else { Some((idx, *n)) };
                                }
                            }
                        }
                    }
                });
            });
        }
        if let (Some(m), false) = (&meta, result.abstained) {
            answer_meta_lines(ui, m);
        }
        if let Some((mi, n)) = self.open_cite {
            if mi == idx {
                if let Some(&ci) = order.get(n) {
                    let c = &result.citations[ci];
                    egui::Frame::group(ui.style())
                        .fill(ACCENT.gamma_multiply(0.07))
                        .show(ui, |ui| {
                            ui.set_width(ui.available_width());
                            ui.label(
                                RichText::new(format!("{} · {}", n + 1, c.source_display_name))
                                    .strong(),
                            );
                            if c.excerpt.is_empty() {
                                ui.label(
                                    RichText::new("The passage is no longer available.").weak(),
                                );
                            } else {
                                ui.label(&c.excerpt);
                            }
                        });
                }
            }
        }
        ui.label(RichText::new(format!("{wall_s:.1} s")).small().weak());
    }

    fn chat(&mut self, ui: &mut egui::Ui) {
        egui::Panel::bottom("composer").show(ui, |ui| {
            ui.add_space(8.0);
            if let Some(why) = self.ask_off_reason() {
                ui.label(RichText::new(why).color(WARN));
            }
            ui.horizontal(|ui| {
                let enabled = self.can_ask();
                let w = ui.available_width() - 90.0;
                let hint = match &self.scope {
                    Some(id) => {
                        let name = self
                            .src
                            .list
                            .iter()
                            .find(|s| &s.id == id)
                            .map(|s| s.display_name.as_str())
                            .unwrap_or("source");
                        format!("Ask about {name}...")
                    }
                    None => "Ask about your documents...".to_string(),
                };
                let edit = egui::TextEdit::singleline(&mut self.input)
                    .hint_text(hint)
                    .desired_width(w)
                    .margin(egui::vec2(10.0, 8.0));
                let resp = ui.add_enabled(enabled, edit);
                if self.focus_input && enabled {
                    resp.request_focus();
                    self.focus_input = false;
                }
                let enter = resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                let stopping = self.asking.as_ref().is_some_and(|(p, _)| p.stop_requested);
                if self.asking.is_some() && self.conn.tasks_v1 {
                    // A question is running: the button becomes Stop.
                    let label = if stopping { "Stopping..." } else { "Stop" };
                    let stop = ui.add_enabled(
                        !stopping,
                        egui::Button::new(label).fill(BAD.gamma_multiply(0.85)),
                    );
                    if stop.clicked() {
                        self.stop();
                    }
                } else {
                    let send = ui.add_enabled(
                        enabled && !self.input.trim().is_empty(),
                        egui::Button::new("Ask").fill(ACCENT),
                    );
                    if send.clicked() || (enter && enabled) {
                        self.submit();
                    }
                }
            });
            ui.add_space(8.0);
        });
        egui::CentralPanel::default().show(ui, |ui| {
            egui::ScrollArea::vertical().stick_to_bottom(true).auto_shrink([false, false]).show(ui, |ui| {
                if self.messages.is_empty() && self.asking.is_none() {
                    ui.add_space(SPACE * 2.0);
                    ui.vertical_centered(|ui| {
                        ui.heading("Ask a question about your documents");
                        ui.label(RichText::new("Answers cite the passages they come from. If the evidence is not there, Docket says so.").weak());
                    });
                }
                let msgs = std::mem::take(&mut self.messages);
                for (i, m) in msgs.iter().enumerate() {
                    match m {
                        Message::User(q) => {
                            ui.with_layout(egui::Layout::right_to_left(egui::Align::Min), |ui| {
                                egui::Frame::group(ui.style()).fill(ui.visuals().faint_bg_color).show(ui, |ui| {
                                    ui.set_max_width(ui.available_width() * 0.8);
                                    ui.label(q);
                                });
                            });
                        }
                        Message::Answer { result, wall_s } => self.answer_card(ui, i, result, *wall_s),
                        Message::Cancelled => {
                            egui::Frame::group(ui.style()).show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                ui.label(RichText::new(model::STOPPED_TITLE).strong());
                                ui.label(RichText::new(model::STOPPED_NOTE).small().weak());
                            });
                        }
                        Message::Error(e) => {
                            egui::Frame::group(ui.style()).stroke(egui::Stroke::new(1.5, BAD)).show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                ui.label(RichText::new(e).color(BAD));
                            });
                        }
                    }
                    ui.add_space(SPACE / 2.0);
                }
                self.messages = msgs;
                if let Some((pending, since)) = &self.asking {
                    if pending.is_draft() && !pending.stop_requested {
                        // Streamed text is a draft: dim, plain (no citation
                        // chips) and labelled until the daemon has validated it.
                        egui::Frame::group(ui.style()).show(ui, |ui| {
                            ui.set_width(ui.available_width());
                            ui.label(RichText::new(pending.text.trim_start()).weak());
                        });
                    }
                    ui.horizontal(|ui| {
                        ui.spinner();
                        let label = if pending.stop_requested {
                            "Stopping... the partial answer is discarded".to_string()
                        } else if pending.is_validating() {
                            "Validating citations... (draft above is not yet verified)".to_string()
                        } else {
                            pending.phase.label().to_string()
                        };
                        ui.label(format!("{label} {:.0} s", since.elapsed().as_secs_f32()));
                    });
                    ui.ctx().request_repaint_after(std::time::Duration::from_millis(500));
                }
            });
        });
    }
}

impl eframe::App for DocketApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.pump();
        let now = self.now_ms();
        self.view = self.status.view(now, self.asking.is_some());
        if !self.view.banners.is_empty() {
            egui::Panel::top("banners").show(ui, |ui| {
                ui.add_space(4.0);
                self.banners(ui);
            });
        }
        egui::Panel::bottom("status").show(ui, |ui| {
            ui.add_space(4.0);
            self.status_line(ui);
            ui.add_space(4.0);
        });
        egui::Panel::left("library")
            .resizable(true)
            .default_size(240.0)
            .show(ui, |ui| self.library(ui));
        self.backend.set_asking(self.asking.is_some());
        // Ages tick on screen even when nothing else repaints.
        ui.ctx()
            .request_repaint_after(std::time::Duration::from_secs(1));
        self.chat(ui);
    }
}
