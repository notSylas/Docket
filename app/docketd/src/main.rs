use docketd::{config::Config, serve};
use tokio::signal::unix::{signal, SignalKind};

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("DOCKET_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_writer(std::io::stderr)
        .init();
    let cfg = match Config::from_env() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("docketd: {e}");
            std::process::exit(2);
        }
    };
    let shutdown = async {
        let (Ok(mut term), Ok(mut int)) = (
            signal(SignalKind::terminate()),
            signal(SignalKind::interrupt()),
        ) else {
            std::future::pending::<()>().await;
            return;
        };
        tokio::select! { _ = term.recv() => {}, _ = int.recv() => {} }
    };
    if let Err(e) = serve(cfg, shutdown).await {
        eprintln!("docketd: {e}");
        std::process::exit(1);
    }
}
