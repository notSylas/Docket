use crate::backend::{Backend, Event};
use crate::model::{self, Connection, Message, Segment};
use docket_proto::api::{AskResult, Phase, SourceInfo, WorkerState};
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
    sources: Vec<SourceInfo>,
    sources_note: String,
    scope: Option<String>,
    input: String,
    messages: Vec<Message>,
    asking: Option<(Phase, Instant)>,
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
            sources: Vec::new(),
            sources_note: "Connecting...".into(),
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

    fn pump(&mut self) {
        if self.auto_ask.is_some() && self.can_ask() {
            self.input = self.auto_ask.take().unwrap_or_default();
            self.submit();
        }
        while let Ok(ev) = self.backend.rx.try_recv() {
            match ev {
                Event::Status(s) => self.conn.apply_status(s),
                Event::Sources(Ok(s)) => {
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
                    self.sources = s;
                }
                Event::Sources(Err(e)) => self.sources_note = e,
                Event::Phase(p) => {
                    if let Some((cur, _)) = &mut self.asking {
                        *cur = p;
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
        self.asking = Some((Phase::Retrieving, Instant::now()));
        self.open_cite = None;
        self.backend.ask(q, self.scope.clone());
    }

    fn can_ask(&self) -> bool {
        self.conn.service_up && self.conn.worker == Some(WorkerState::Up) && self.asking.is_none()
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
        if !self.sources_note.is_empty() {
            ui.label(RichText::new(&self.sources_note).small().weak());
        }
        egui::ScrollArea::vertical().show(ui, |ui| {
            for s in &self.sources {
                let selected = self.scope.as_deref() == Some(s.id.as_str());
                let ready = model::source_ready(s);
                let resp = ui
                    .scope(|ui| {
                        ui.horizontal(|ui| {
                            dot(ui, if ready { ACCENT } else { WARN });
                            ui.vertical(|ui| {
                                ui.label(RichText::new(&s.display_name).strong());
                                let detail = format!(
                                    "{} · {} files",
                                    model::status_label(&s.status),
                                    s.files
                                );
                                ui.label(RichText::new(detail).small().weak());
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
                if resp.on_hover_text(&s.path).clicked() {
                    self.scope = if selected { None } else { Some(s.id.clone()) };
                }
                ui.add_space(2.0);
            }
        });
    }

    fn status_line(&self, ui: &mut egui::Ui) {
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

    fn answer_card(&mut self, ui: &mut egui::Ui, idx: usize, result: &AskResult, wall_s: f64) {
        let (segs, order) = model::layout(&result.answer, &result.citations);
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
            ui.horizontal(|ui| {
                let enabled = self.can_ask();
                let w = ui.available_width() - 90.0;
                let hint = match &self.scope {
                    Some(id) => {
                        let name = self
                            .sources
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
                let send = ui.add_enabled(
                    enabled && !self.input.trim().is_empty(),
                    egui::Button::new("Ask").fill(ACCENT),
                );
                if send.clicked() || (enter && enabled) {
                    self.submit();
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
                if let Some((phase, since)) = &self.asking {
                    ui.horizontal(|ui| {
                        ui.spinner();
                        ui.label(format!("{} {:.0} s", phase.label(), since.elapsed().as_secs_f32()));
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
        egui::Panel::bottom("status").show(ui, |ui| {
            ui.add_space(4.0);
            self.status_line(ui);
            ui.add_space(4.0);
        });
        egui::Panel::left("library")
            .resizable(true)
            .default_size(240.0)
            .show(ui, |ui| self.library(ui));
        self.chat(ui);
    }
}
