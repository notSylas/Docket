//! `docketd`: the always-on local service. It supervises the Python worker and
//! exposes a small JSON API on a Unix domain socket (no TCP listener).

pub mod api;
pub mod config;
pub mod health;
pub mod socket;
pub mod tasks;
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
    let (handle, sup) = worker::WorkerHandle::spawn(&cfg, stop_rx.clone());
    let state = api::AppState::new(&cfg, handle);
    let health = state.spawn_health(&cfg, stop_rx.clone());
    let registry = state.registry();
    let app = api::router(state);
    info!(target: "docketd", socket = %cfg.socket.display(), "listening");
    // On shutdown, cancel running tasks first (the worker is still supervised,
    // so it is terminated cleanly), wait briefly for them to finish, and only
    // then let the server drain its connections.
    let shutdown = async move {
        shutdown.await;
        registry.cancel_all(docket_proto::task::CancelSource::Shutdown);
        registry
            .wait_all_finished(std::time::Duration::from_secs(10))
            .await;
    };
    let result = axum::serve(listener, app)
        .with_graceful_shutdown(shutdown)
        .await;
    let _ = stop_tx.send(true);
    let _ = health.await;
    let _ = sup.await;
    let _ = std::fs::remove_file(&cfg.socket);
    info!(target: "docketd", "stopped");
    result
}
