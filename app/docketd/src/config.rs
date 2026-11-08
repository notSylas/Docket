use std::path::PathBuf;
use std::time::Duration;

/// Everything the daemon needs to run. `from_env` reads the documented
/// variables; tests build it directly.
#[derive(Debug, Clone)]
pub struct Config {
    /// Unix socket path. Default: `$XDG_RUNTIME_DIR/docket/docketd.sock`.
    pub socket: PathBuf,
    /// True when `socket` came from the default location (the private
    /// directory rules then apply to its parent).
    pub socket_is_default: bool,
    /// Worker command and arguments (split on whitespace).
    pub worker_cmd: Vec<String>,
    /// Passed to the worker as `DOCKET_DATA_DIR` when set.
    pub data_dir: Option<PathBuf>,
    /// Limit for one `query.ask`.
    pub ask_timeout: Duration,
    /// Limit for any other worker call.
    pub call_timeout: Duration,
    /// Limit for the worker to answer its first ping after launch.
    pub startup_timeout: Duration,
    /// Idle health-check period.
    pub ping_interval: Duration,
    /// First restart delay; doubles per consecutive failure up to `backoff_max`.
    pub backoff_base: Duration,
    pub backoff_max: Duration,
}

fn env_ms(name: &str, default: u64) -> Duration {
    Duration::from_millis(
        std::env::var(name)
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(default),
    )
}

impl Config {
    /// Variables: `DOCKET_WORKER_CMD` (default `docket worker`),
    /// `DOCKET_DATA_DIR`, `DOCKET_SOCKET` (override), `XDG_RUNTIME_DIR`,
    /// plus millisecond tuning knobs `DOCKET_ASK_TIMEOUT_MS`,
    /// `DOCKET_CALL_TIMEOUT_MS`, `DOCKET_STARTUP_TIMEOUT_MS`,
    /// `DOCKET_PING_INTERVAL_MS`, `DOCKET_BACKOFF_BASE_MS`, `DOCKET_BACKOFF_MAX_MS`.
    pub fn from_env() -> Result<Self, String> {
        let (socket, socket_is_default) = match std::env::var_os("DOCKET_SOCKET") {
            Some(p) => (PathBuf::from(p), false),
            None => (default_socket_path()?, true),
        };
        let cmd = std::env::var("DOCKET_WORKER_CMD").unwrap_or_else(|_| "docket worker".into());
        let worker_cmd: Vec<String> = cmd.split_whitespace().map(String::from).collect();
        if worker_cmd.is_empty() {
            return Err("DOCKET_WORKER_CMD is empty".into());
        }
        Ok(Self {
            socket,
            socket_is_default,
            worker_cmd,
            data_dir: std::env::var_os("DOCKET_DATA_DIR").map(PathBuf::from),
            ask_timeout: env_ms("DOCKET_ASK_TIMEOUT_MS", 300_000),
            call_timeout: env_ms("DOCKET_CALL_TIMEOUT_MS", 15_000),
            startup_timeout: env_ms("DOCKET_STARTUP_TIMEOUT_MS", 120_000),
            ping_interval: env_ms("DOCKET_PING_INTERVAL_MS", 10_000),
            backoff_base: env_ms("DOCKET_BACKOFF_BASE_MS", 500),
            backoff_max: env_ms("DOCKET_BACKOFF_MAX_MS", 10_000),
        })
    }
}

/// `$XDG_RUNTIME_DIR/docket/docketd.sock`; shared with the GUI.
pub fn default_socket_path() -> Result<PathBuf, String> {
    let dir = std::env::var_os("XDG_RUNTIME_DIR")
        .filter(|v| !v.is_empty())
        .ok_or("XDG_RUNTIME_DIR is not set; set DOCKET_SOCKET to choose a socket path")?;
    Ok(PathBuf::from(dir).join("docket").join("docketd.sock"))
}
