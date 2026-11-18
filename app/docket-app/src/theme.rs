//! Semantic colors shared by one interface; only these tokens vary by theme.
use eframe::egui::{self, Color32, FontFamily, FontId, Stroke, TextStyle};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Theme {
    #[default]
    Obsidian,
    Porcelain,
}

#[derive(Clone, Copy)]
pub struct Tokens {
    pub canvas: Color32,
    pub nav: Color32,
    pub surface: Color32,
    pub surface_secondary: Color32,
    pub selection: Color32,
    pub hover: Color32,
    pub pressed: Color32,
    pub text: Color32,
    pub secondary: Color32,
    pub disabled: Color32,
    pub border: Color32,
    pub control: Color32,
    pub accent: Color32,
    pub on_accent: Color32,
    pub accent_hover: Color32,
    pub accent_pressed: Color32,
    pub focus: Color32,
    pub success: Color32,
    pub warning: Color32,
    pub danger: Color32,
    pub info: Color32,
    pub evidence: Color32,
}
fn rgb(hex: u32) -> Color32 {
    Color32::from_rgb((hex >> 16) as u8, (hex >> 8) as u8, hex as u8)
}
impl Theme {
    pub fn name(self) -> &'static str {
        match self {
            Self::Obsidian => "Obsidian & Silver",
            Self::Porcelain => "Porcelain & Ink",
        }
    }
    pub fn other(self) -> Self {
        match self {
            Self::Obsidian => Self::Porcelain,
            Self::Porcelain => Self::Obsidian,
        }
    }
    pub fn tokens(self) -> Tokens {
        match self {
            Self::Obsidian => Tokens {
                canvas: rgb(0x14181e),
                nav: rgb(0x0e1218),
                surface: rgb(0x1b2129),
                surface_secondary: rgb(0x202833),
                selection: rgb(0x293b4d),
                hover: rgb(0x252f3b),
                pressed: rgb(0x304257),
                text: rgb(0xe8edf4),
                secondary: rgb(0xa8b4c3),
                disabled: rgb(0x8998aa),
                border: rgb(0x303a47),
                control: rgb(0x7d8fa5),
                accent: rgb(0xadc6e1),
                on_accent: rgb(0x142232),
                accent_hover: rgb(0xc3d6ea),
                accent_pressed: rgb(0x91b1d3),
                focus: rgb(0xa7c9f0),
                success: rgb(0xa4cbb9),
                warning: rgb(0xe0c18d),
                danger: rgb(0xf0aaaa),
                info: rgb(0xadc6e1),
                evidence: rgb(0x263c50),
            },
            Self::Porcelain => Tokens {
                canvas: rgb(0xfafbfc),
                nav: rgb(0xf0f3f5),
                surface: rgb(0xffffff),
                surface_secondary: rgb(0xedf1f4),
                selection: rgb(0xdce9eb),
                hover: rgb(0xe7edf1),
                pressed: rgb(0xd6e2e7),
                text: rgb(0x18232e),
                secondary: rgb(0x516272),
                disabled: rgb(0x586a78),
                border: rgb(0xd7dfe6),
                control: rgb(0x718493),
                accent: rgb(0x245f64),
                on_accent: rgb(0xffffff),
                accent_hover: rgb(0x1c5359),
                accent_pressed: rgb(0x18464c),
                focus: rgb(0x225f8e),
                success: rgb(0x2f6653),
                warning: rgb(0x79551c),
                danger: rgb(0x9d3e45),
                info: rgb(0x365e85),
                evidence: rgb(0xdeecee),
            },
        }
    }
    pub fn apply(self, ctx: &egui::Context) {
        let t = self.tokens();
        let mut v = match self {
            Self::Obsidian => egui::Visuals::dark(),
            Self::Porcelain => egui::Visuals::light(),
        };
        v.override_text_color = Some(t.text);
        v.panel_fill = t.canvas;
        v.window_fill = t.surface;
        v.extreme_bg_color = t.surface;
        v.faint_bg_color = t.surface_secondary;
        v.hyperlink_color = t.accent;
        v.selection.bg_fill = t.selection;
        v.selection.stroke = Stroke::new(1.0, t.text);
        v.window_stroke = Stroke::new(1.0, t.border);
        v.window_corner_radius = egui::CornerRadius::same(10);
        v.widgets.noninteractive.bg_fill = t.surface;
        v.widgets.noninteractive.fg_stroke = Stroke::new(1.0, t.secondary);
        v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, t.border);
        v.widgets.inactive.bg_fill = t.surface_secondary;
        v.widgets.inactive.weak_bg_fill = Color32::TRANSPARENT;
        v.widgets.inactive.fg_stroke = Stroke::new(1.0, t.text);
        v.widgets.inactive.bg_stroke = Stroke::NONE;
        v.widgets.hovered.bg_fill = t.hover;
        v.widgets.hovered.weak_bg_fill = t.hover;
        v.widgets.hovered.fg_stroke = Stroke::new(1.0, t.text);
        v.widgets.hovered.bg_stroke = Stroke::new(1.0, t.control);
        v.widgets.active.bg_fill = t.pressed;
        v.widgets.active.weak_bg_fill = t.pressed;
        v.widgets.active.fg_stroke = Stroke::new(1.0, t.text);
        v.widgets.active.bg_stroke = Stroke::new(2.0, t.focus);
        for w in [
            &mut v.widgets.inactive,
            &mut v.widgets.hovered,
            &mut v.widgets.active,
            &mut v.widgets.noninteractive,
        ] {
            w.corner_radius = egui::CornerRadius::same(6);
        }
        ctx.set_visuals(v);
        ctx.global_style_mut(|s| {
            s.text_styles = [
                (
                    TextStyle::Heading,
                    FontId::new(23.0, FontFamily::Proportional),
                ),
                (TextStyle::Body, FontId::new(15.0, FontFamily::Proportional)),
                (
                    TextStyle::Button,
                    FontId::new(14.0, FontFamily::Proportional),
                ),
                (
                    TextStyle::Small,
                    FontId::new(12.0, FontFamily::Proportional),
                ),
                (
                    TextStyle::Monospace,
                    FontId::new(12.0, FontFamily::Monospace),
                ),
            ]
            .into();
            s.spacing.item_spacing = egui::vec2(8.0, 8.0);
            s.spacing.button_padding = egui::vec2(12.0, 8.0);
            s.spacing.interact_size.y = 36.0;
            s.spacing.window_margin = egui::Margin::same(24);
            s.visuals.interact_cursor = Some(egui::CursorIcon::PointingHand);
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn luminance(c: Color32) -> f64 {
        let channel = |b: u8| {
            let s = f64::from(b) / 255.0;
            if s <= 0.04045 {
                s / 12.92
            } else {
                ((s + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * channel(c.r()) + 0.7152 * channel(c.g()) + 0.0722 * channel(c.b())
    }
    fn ratio(a: Color32, b: Color32) -> f64 {
        let a = luminance(a);
        let b = luminance(b);
        (a.max(b) + 0.05) / (a.min(b) + 0.05)
    }
    #[test]
    fn reading_status_and_focus_tokens_meet_contrast_targets() {
        for theme in [Theme::Obsidian, Theme::Porcelain] {
            let t = theme.tokens();
            for background in [
                t.canvas,
                t.nav,
                t.surface,
                t.surface_secondary,
                t.selection,
                t.evidence,
                t.hover,
                t.pressed,
            ] {
                assert!(ratio(t.text, background) >= 4.5, "{theme:?} reading text");
            }
            for background in [t.canvas, t.nav, t.surface, t.surface_secondary] {
                for fg in [
                    t.secondary,
                    t.disabled,
                    t.accent,
                    t.success,
                    t.warning,
                    t.danger,
                    t.info,
                ] {
                    assert!(
                        ratio(fg, background) >= 4.5,
                        "{theme:?}: {fg:?} on {background:?}"
                    );
                }
                assert!(ratio(t.focus, background) >= 3.0);
                assert!(ratio(t.control, background) >= 3.0);
            }
            for bg in [t.accent, t.accent_hover, t.accent_pressed] {
                assert!(ratio(t.on_accent, bg) >= 4.5);
            }
        }
    }
}
