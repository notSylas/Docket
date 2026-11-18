//! Presentation-only workspace shell. No transport, evidence or simulated state.
use crate::theme::{Theme, Tokens};
use eframe::egui::{self, RichText};

pub const NAV_WIDTH: f32 = 216.0;
pub const GUTTER: i8 = 24;

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum Destination {
    #[default]
    Assistant,
    Library,
}
impl Destination {
    pub fn name(self) -> &'static str {
        match self {
            Self::Assistant => "Assistant",
            Self::Library => "Library",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command {
    Assistant,
    Library,
    FocusQuestion,
    Theme,
    Settings,
    Stop,
}
impl Command {
    fn label(self) -> &'static str {
        match self {
            Self::Assistant => "Go to Assistant",
            Self::Library => "Go to Library",
            Self::FocusQuestion => "Focus question",
            Self::Theme => "Switch theme",
            Self::Settings => "Appearance settings",
            Self::Stop => "Stop current question",
        }
    }
    fn hint(self) -> &'static str {
        match self {
            Self::Assistant => "Ctrl 1",
            Self::Library => "Ctrl 2",
            Self::FocusQuestion => "Assistant",
            Self::Theme => "Ctrl Shift T",
            Self::Settings => "Appearance",
            Self::Stop => "Ctrl Shift X",
        }
    }
}

#[derive(Default)]
pub struct Shell {
    pub theme: Theme,
    pub destination: Destination,
    palette_open: bool,
    settings_open: bool,
    query: String,
    index: usize,
    focus_palette: bool,
    return_focus: Option<egui::Id>,
}
impl Shell {
    pub fn compact(width: f32) -> bool {
        width < 900.0
    }
    pub fn modal_open(&self) -> bool {
        self.palette_open || self.settings_open
    }
    fn close(&mut self, ctx: &egui::Context) {
        self.palette_open = false;
        self.settings_open = false;
        if let Some(id) = self.return_focus.take() {
            ctx.memory_mut(|m| m.request_focus(id));
        }
    }
    fn open(&mut self, ctx: &egui::Context, palette: bool) {
        if !self.modal_open() {
            self.return_focus = ctx.memory(|m| m.focused());
        }
        self.palette_open = palette;
        self.settings_open = !palette;
        if palette {
            self.query.clear();
            self.index = 0;
            self.focus_palette = true;
        }
    }
    pub fn apply(&mut self, cmd: Command, ctx: &egui::Context) {
        self.close(ctx);
        match cmd {
            Command::Assistant | Command::FocusQuestion => {
                self.destination = Destination::Assistant
            }
            Command::Library => self.destination = Destination::Library,
            Command::Theme => {
                self.theme = self.theme.other();
                self.theme.apply(ctx);
            }
            Command::Settings => self.open(ctx, false),
            Command::Stop => {}
        }
    }
    pub fn shortcuts(&mut self, ctx: &egui::Context, can_stop: bool) -> Option<Command> {
        if ctx.input_mut(|i| i.consume_key(egui::Modifiers::CTRL, egui::Key::K)) {
            if self.palette_open {
                self.close(ctx);
            } else {
                self.open(ctx, true);
            }
            return None;
        }
        if self.modal_open() {
            if ctx.input_mut(|i| i.consume_key(egui::Modifiers::NONE, egui::Key::Escape)) {
                self.close(ctx);
            }
            return None;
        }
        for (key, modifiers, cmd) in [
            (egui::Key::Num1, egui::Modifiers::CTRL, Command::Assistant),
            (egui::Key::Num2, egui::Modifiers::CTRL, Command::Library),
            (
                egui::Key::T,
                egui::Modifiers::CTRL | egui::Modifiers::SHIFT,
                Command::Theme,
            ),
            (
                egui::Key::X,
                egui::Modifiers::CTRL | egui::Modifiers::SHIFT,
                Command::Stop,
            ),
        ] {
            if (cmd != Command::Stop || can_stop)
                && ctx.input_mut(|i| i.consume_key(modifiers, key))
            {
                return Some(cmd);
            }
        }
        None
    }
    pub fn navigation(&mut self, ui: &mut egui::Ui) -> Option<Command> {
        let t = self.theme.tokens();
        let mut command = None;
        egui::ScrollArea::vertical().show(ui, |ui| {
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                let (r, _) = ui.allocate_exact_size(egui::vec2(30.0, 32.0), egui::Sense::hover());
                ui.painter()
                    .rect_filled(r.shrink(1.0), 7, t.surface_secondary);
                icon(ui.painter(), r.center(), Icon::Document, t.accent);
                ui.label(RichText::new("Docket").size(22.0).strong());
            });
            ui.add_space(28.0);
            if row_button(ui, "Commands", "Ctrl K", false, Icon::Search, t).clicked() {
                self.open(ui.ctx(), true);
            }
            ui.add_space(16.0);
            for (dest, glyph, cmd) in [
                (Destination::Assistant, Icon::Assistant, Command::Assistant),
                (Destination::Library, Icon::Library, Command::Library),
            ] {
                if row_button(ui, dest.name(), "", self.destination == dest, glyph, t).clicked() {
                    command = Some(cmd);
                }
            }
            ui.add_space(12.0);
            for (name, glyph) in [
                ("Knowledge", Icon::Library),
                ("Investigations", Icon::Search),
                ("Activity", Icon::Document),
            ] {
                planned_row(ui, name, glyph, t);
            }
            ui.add_space(24.0);
            if row_button(ui, "Settings", "", self.settings_open, Icon::Settings, t).clicked() {
                command = Some(Command::Settings);
            }
            caption(ui, "Local workspace", t);
        });
        command
    }
    pub fn header(&mut self, ui: &mut egui::Ui, compact: bool) -> Option<Command> {
        let t = self.theme.tokens();
        let mut command = None;
        ui.horizontal_wrapped(|ui| {
            if compact {
                for (dest, cmd) in [
                    (Destination::Assistant, Command::Assistant),
                    (Destination::Library, Command::Library),
                ] {
                    if ui
                        .selectable_label(self.destination == dest, dest.name())
                        .clicked()
                    {
                        command = Some(cmd);
                    }
                }
                if ui.button("Commands").on_hover_text("Ctrl K").clicked() {
                    self.open(ui.ctx(), true);
                }
                if ui.button("Settings").clicked() {
                    command = Some(Command::Settings);
                }
            } else {
                ui.label(RichText::new(self.destination.name()).strong());
                caption(
                    ui,
                    if self.destination == Destination::Assistant {
                        "/  Current conversation"
                    } else {
                        "/  Sources"
                    },
                    t,
                );
            }
        });
        command
    }
    fn commands(can_stop: bool, query: &str) -> Vec<Command> {
        let mut commands = vec![
            Command::Assistant,
            Command::Library,
            Command::FocusQuestion,
            Command::Theme,
            Command::Settings,
        ];
        if can_stop {
            commands.push(Command::Stop);
        }
        let query = query.trim().to_lowercase();
        commands.retain(|c| c.label().to_lowercase().contains(&query));
        commands
    }
    pub fn dialogs(&mut self, ctx: &egui::Context, can_stop: bool) -> Option<Command> {
        let t = self.theme.tokens();
        let mut execute = None;
        if self.palette_open {
            let modal = egui::Modal::new(egui::Id::unique("command-palette"))
                .frame(modal_frame(t))
                .show(ctx, |ui| {
                    ui.set_width((ctx.content_rect().width() - 96.0).min(540.0));
                    ui.heading("Find a command");
                    let edit = ui.add_sized(
                        [ui.available_width(), 38.0],
                        egui::TextEdit::singleline(&mut self.query)
                            .id(egui::Id::unique("palette-input"))
                            .hint_text("Search commands…")
                            .frame(input_frame(t)),
                    );
                    if self.focus_palette {
                        edit.request_focus();
                        self.focus_palette = false;
                    }
                    if edit.changed() {
                        self.index = 0;
                    }
                    let commands = Self::commands(can_stop, &self.query);
                    if !commands.is_empty() {
                        if ctx.input_mut(|i| {
                            i.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)
                        }) {
                            self.index = (self.index + 1) % commands.len();
                        }
                        if ctx
                            .input_mut(|i| i.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp))
                        {
                            self.index = (self.index + commands.len() - 1) % commands.len();
                        }
                        self.index = self.index.min(commands.len() - 1);
                        if ctx.input_mut(|i| i.consume_key(egui::Modifiers::NONE, egui::Key::Enter))
                        {
                            execute = commands.get(self.index).copied();
                        }
                    }
                    egui::ScrollArea::vertical()
                        .max_height((ctx.content_rect().height() - 230.0).clamp(100.0, 400.0))
                        .show(ui, |ui| {
                            for (index, command) in commands.iter().enumerate() {
                                let response = ui.add_sized(
                                    [ui.available_width(), 38.0],
                                    egui::Button::new(command.label())
                                        .right_text(
                                            RichText::new(command.hint())
                                                .size(12.0)
                                                .color(t.secondary),
                                        )
                                        .selected(self.index == index),
                                );
                                if response.clicked() {
                                    execute = Some(*command);
                                }
                                if index == self.index {
                                    response.scroll_to_me(Some(egui::Align::Center));
                                }
                            }
                            if commands.is_empty() {
                                ui.label("No matching commands.");
                            }
                        });
                    ui.separator();
                    caption(ui, "Up/Down Select    Enter Open    Esc Close", t);
                });
            if modal.should_close() || execute.is_some() {
                self.close(ctx);
            }
        } else if self.settings_open {
            let modal = egui::Modal::new(egui::Id::unique("appearance-settings"))
                .frame(modal_frame(t)).show(ctx, |ui| {
                    ui.set_width((ctx.content_rect().width() - 96.0).min(500.0));
                    ui.heading("Settings");
                    egui::ScrollArea::vertical().max_height((ctx.content_rect().height() - 180.0).max(100.0)).show(ui, |ui| {
                        caption(ui, "Appearance", t);
                        for theme in [Theme::Obsidian, Theme::Porcelain] {
                            if ui.add_sized([ui.available_width(), 48.0], egui::Button::new(theme.name())
                                .right_text(if self.theme == theme { "Selected" } else { "" }).selected(self.theme == theme)).clicked() {
                                self.theme = theme; theme.apply(ctx);
                            }
                        }
                        ui.add_space(12.0);
                        caption(ui, "Theme applies to this window. It is not saved after restart.", t);
                        ui.separator();
                        caption(ui, "Ctrl K · Commands\nCtrl 1 / Ctrl 2 · Assistant / Library\nCtrl Shift T · Switch theme\nCtrl Shift X · Stop (when supported)\nEnter · New line\nCtrl Enter · Ask from composer\nEsc · Close dialog", t);
                    });
                    if ui.button("Done").clicked() { self.close(ctx); }
                });
            if modal.should_close() {
                self.close(ctx);
            }
        }
        execute
    }
}

pub fn caption(ui: &mut egui::Ui, text: impl Into<String>, t: Tokens) {
    ui.label(RichText::new(text.into()).size(13.0).color(t.secondary));
}

pub fn primary(ui: &mut egui::Ui, title: &str, enabled: bool, t: Tokens) -> egui::Response {
    ui.scope(|ui| {
        let v = ui.visuals_mut();
        v.widgets.inactive.weak_bg_fill = t.accent;
        v.widgets.inactive.bg_fill = t.accent;
        v.widgets.hovered.weak_bg_fill = t.accent_hover;
        v.widgets.hovered.bg_fill = t.accent_hover;
        v.widgets.active.weak_bg_fill = t.accent_pressed;
        v.widgets.active.bg_fill = t.accent_pressed;
        ui.add_enabled(
            enabled,
            egui::Button::new(RichText::new(title).color(t.on_accent)),
        )
    })
    .inner
}

#[derive(Clone, Copy)]
pub enum Icon {
    Assistant,
    Library,
    Search,
    Settings,
    Document,
}

/// Small geometric icon set painted in logical points; no platform glyph fallback.
pub fn icon(p: &egui::Painter, center: egui::Pos2, icon: Icon, color: egui::Color32) {
    let r = egui::Rect::from_center_size(center, egui::vec2(16.0, 16.0));
    let stroke = egui::Stroke::new(1.35, color);
    match icon {
        Icon::Search => {
            p.circle_stroke(center - egui::vec2(2.0, 2.0), 5.0, stroke);
            p.line_segment([center + egui::vec2(2.0, 2.0), r.right_bottom()], stroke);
        }
        Icon::Assistant => {
            p.rect_stroke(
                r.shrink2(egui::vec2(0.0, 2.0)),
                3,
                stroke,
                egui::StrokeKind::Inside,
            );
            p.line_segment(
                [r.left_bottom(), r.left_bottom() - egui::vec2(-4.0, 4.0)],
                stroke,
            );
            p.line_segment(
                [
                    center - egui::vec2(4.0, 1.0),
                    center + egui::vec2(4.0, -1.0),
                ],
                stroke,
            );
        }
        Icon::Library => {
            for x in [-5.0, 0.0, 5.0] {
                p.line_segment(
                    [center + egui::vec2(x, -6.0), center + egui::vec2(x, 6.0)],
                    stroke,
                );
            }
            p.line_segment([r.left_bottom(), r.right_bottom()], stroke);
        }
        Icon::Settings => {
            p.circle_stroke(center, 6.0, stroke);
            p.circle_stroke(center, 2.0, stroke);
            for d in [egui::vec2(0.0, 8.0), egui::vec2(8.0, 0.0)] {
                p.line_segment([center - d, center + d], stroke);
            }
        }
        Icon::Document => {
            p.rect_stroke(
                r.shrink2(egui::vec2(2.0, 0.0)),
                2,
                stroke,
                egui::StrokeKind::Inside,
            );
            for y in [-3.0, 1.0, 5.0] {
                p.line_segment(
                    [center + egui::vec2(-3.0, y), center + egui::vec2(3.0, y)],
                    stroke,
                );
            }
        }
    }
}

pub fn row_button(
    ui: &mut egui::Ui,
    label: &str,
    trailing: &str,
    selected: bool,
    glyph: Icon,
    t: Tokens,
) -> egui::Response {
    let response = ui.add_sized(
        [ui.available_width(), 38.0],
        egui::Button::new(format!("      {label}"))
            .right_text(RichText::new(trailing).size(11.0).color(t.secondary))
            .selected(selected),
    );
    icon(
        ui.painter(),
        response.rect.left_center() + egui::vec2(18.0, 0.0),
        glyph,
        if ui.is_enabled() {
            t.secondary
        } else {
            t.disabled
        },
    );
    if selected {
        let r = response.rect;
        ui.painter().line_segment(
            [
                r.left_top() + egui::vec2(1.0, 10.0),
                r.left_bottom() + egui::vec2(1.0, -10.0),
            ],
            egui::Stroke::new(2.0, t.accent),
        );
    }
    response
}

/// Inputs have an essential boundary; quiet action controls do not.
pub fn input_frame(t: Tokens) -> egui::Frame {
    egui::Frame::new()
        .fill(t.surface)
        .stroke(egui::Stroke::new(1.0, t.control))
        .corner_radius(6)
        .inner_margin(egui::Margin::symmetric(12, 8))
}

/// Planned destinations are readable labels, not actionable buttons.
pub fn planned_row(ui: &mut egui::Ui, label: &str, glyph: Icon, t: Tokens) {
    let (rect, response) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 38.0), egui::Sense::hover());
    response.widget_info(|| {
        egui::WidgetInfo::labeled(egui::Role::Label, true, format!("{label} (planned)"))
    });
    icon(
        ui.painter(),
        rect.left_center() + egui::vec2(18.0, 0.0),
        glyph,
        t.disabled,
    );
    ui.painter().text(
        rect.left_center() + egui::vec2(32.0, 0.0),
        egui::Align2::LEFT_CENTER,
        label,
        egui::FontId::proportional(14.0),
        t.disabled,
    );
    ui.painter().text(
        rect.right_center(),
        egui::Align2::RIGHT_CENTER,
        "Planned",
        egui::FontId::proportional(10.0),
        t.disabled,
    );
    response.on_hover_text("Planned destination");
}

pub fn modal_frame(t: Tokens) -> egui::Frame {
    egui::Frame::new()
        .fill(t.surface)
        .stroke(egui::Stroke::new(1.0, t.border))
        .corner_radius(10)
        .inner_margin(24)
}

pub fn prose(ui: &mut egui::Ui, text: &str, t: Tokens) {
    let mut job = egui::text::LayoutJob::default();
    job.append(
        text,
        0.0,
        egui::TextFormat {
            font_id: egui::FontId::proportional(16.0),
            color: t.text,
            line_height: Some(25.0),
            ..Default::default()
        },
    );
    ui.add(egui::Label::new(job).selectable(true).wrap());
}

pub fn citation_link(
    ui: &mut egui::Ui,
    number: usize,
    title: &str,
    selected: bool,
    t: Tokens,
) -> egui::Response {
    ui.scope(|ui| {
        ui.visuals_mut().widgets.inactive.weak_bg_fill = t.surface_secondary;
        ui.add_sized(
            [ui.available_width(), 42.0],
            egui::Button::new(RichText::new(format!("[{number}]  {title}")).color(t.accent))
                .right_text(
                    RichText::new(if selected {
                        "Viewing passage"
                    } else {
                        "Open passage"
                    })
                    .size(12.0)
                    .color(t.accent),
                )
                .selected(selected)
                .wrap(),
        )
    })
    .inner
}

pub fn response_heading(ui: &mut egui::Ui, status: &str, t: Tokens) {
    ui.horizontal(|ui| {
        let (r, _) = ui.allocate_exact_size(egui::vec2(26.0, 26.0), egui::Sense::hover());
        ui.painter().rect_filled(r, 6, t.selection);
        icon(ui.painter(), r.center(), Icon::Assistant, t.accent);
        ui.label(RichText::new("Docket").size(16.0).strong());
        caption(ui, status, t);
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn commands_only_offer_stop_when_supported_and_filter_locally() {
        assert!(!Shell::commands(false, "").contains(&Command::Stop));
        assert_eq!(Shell::commands(true, "stop"), vec![Command::Stop]);
        assert_eq!(Shell::commands(false, " THEME "), vec![Command::Theme]);
        assert!(Shell::commands(true, "demo").is_empty());
        assert!(Shell::compact(640.0) && Shell::compact(800.0));
        assert!(!Shell::compact(900.0) && !Shell::compact(1440.0));
    }
    #[test]
    fn appearance_and_modal_transitions_preserve_destination_and_focus_origin() {
        let ctx = egui::Context::default();
        let mut shell = Shell::default();
        let origin = egui::Id::unique("origin");
        ctx.memory_mut(|m| m.request_focus(origin));
        shell.destination = Destination::Library;
        shell.open(&ctx, true);
        assert_eq!(shell.return_focus, Some(origin));
        shell.open(&ctx, false);
        assert!(!shell.palette_open && shell.settings_open);
        shell.apply(Command::Theme, &ctx);
        assert_eq!(shell.theme, Theme::Porcelain);
        assert_eq!(shell.destination, Destination::Library);
        assert!(!shell.modal_open());
        assert_eq!(ctx.memory(|m| m.focused()), Some(origin));
    }
}
