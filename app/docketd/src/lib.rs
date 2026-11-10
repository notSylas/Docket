//! `docketd`: the always-on local service. It supervises the Python worker and
//! exposes a small JSON API on a Unix domain socket (no TCP listener).

pub mod api;
pub mod config;
pub mod socket;
pub mod worker;

use std::future::Future;
use tokio::sync::watch;
use tracing::info;

/// Bind the socket, run the API and the worker supervisor until `shutdown`
/// completes, then stop the worker and remove the socket.
pub async fn serve(
    cfg: config::Config,
    shutdown: impl Future<Output = ()> + Send + 'static,
) -> std::io::Result<()> {
    let listener = socket::bind(&cfg.socket, cfg.socket_is_default)?;
    let (stop_tx, stop_rx) = watch::channel(false);
    let (handle, sup) = worker::WorkerHandle::spawn(&cfg, stop_rx);
    let app = api::router(api::AppState::new(&cfg, handle));
    info!(target: "docketd", socket = %cfg.socket.display(), "listening");
    let result = axum::serve(listener, app)
        .with_graceful_shutdown(shutdown)
        .await;
    let _ = stop_tx.send(true);
    let _ = sup.await;
    let _ = std::fs::remove_file(&cfg.socket);
    info!(target: "docketd", "stopped");
    result
}
