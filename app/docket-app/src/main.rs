//! Docket desktop app.
//!
//! GUI toolkit choice (spike): egui/eframe. It is pure Rust, permissively
//! licensed, builds on Linux without extra system toolkits, and an
//! immediate-mode UI over a small state struct is the fastest way to prove the
//! service/worker architecture. iced and slint were considered; slint is
//! GPL/commercial licensed, so it is avoided. All UI code is in `ui.rs` and
//! only talks to `backend.rs` and `model.rs`, so the toolkit can be swapped
//! without touching the protocol or daemon.

mod backend;
mod model;
mod ui;

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_inner_size([1040.0, 720.0])
            .with_min_inner_size([640.0, 420.0])
            .with_app_id("docket")
            .with_title("Docket"),
        ..Default::default()
    };
    eframe::run_native(
        "Docket",
        options,
        Box::new(|cc| {
            let socket = match backend::socket_path() {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("docket-app: {e}");
                    std::process::exit(2);
                }
            };
            Ok(Box::new(ui::DocketApp::new(cc, socket)))
        }),
    )
}
