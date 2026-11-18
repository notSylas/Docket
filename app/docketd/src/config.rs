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
    /// Serve `/v1/tasks...` and advertise `tasks.v1`. `DOCKET_TASKS=off` hides both.
    pub tasks_enabled: bool,
    /// How long a cancelled worker may take to exit after SIGTERM before the
    /// whole process group is killed.
    pub cancel_grace: Duration,
    /// Events retained per task for `Last-Event-ID` replay.
    pub task_tail: usize,
    /// Finished tasks kept for listing and replay.
    pub retained_tasks: usize,
    /// Tasks allowed to wait behind the running one (`POST /v1/tasks` only).
    pub max_queued: usize,
    /// How often the daemon asks an idle worker for `health`. Zero disables
    /// the refresh (and the `status.v2` capability).
    pub health_interval: Duration,
    /// A health section older than this is reported `stale`.
    pub health_stale: Duration,
    /// Limit for one `health` call. A miss only marks the health sections
    /// stale (`health_timeout`); the worker is NOT replaced for it (real hang
    /// detection is the idle ping and the ask timeout). It is also the longest
    /// a question can wait behind a health call already in flight.
    pub health_timeout: Duration,
}

fn env_ms(name: &str, default: u64) -> Duration {
    Duration::from_millis(
        std::env::var(name)
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(default),
    )
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

impl Config {
    /// Variables: `DOCKET_WORKER_CMD` (default `docket worker`),
    /// `DOCKET_DATA_DIR`, `DOCKET_SOCKET` (override), `XDG_RUNTIME_DIR`,
    /// plus millisecond tuning knobs `DOCKET_ASK_TIMEOUT_MS`,
    /// `DOCKET_CALL_TIMEOUT_MS`, `DOCKET_STARTUP_TIMEOUT_MS`,
    /// `DOCKET_PING_INTERVAL_MS`, `DOCKET_BACKOFF_BASE_MS`, `DOCKET_BACKOFF_MAX_MS`,
    /// `DOCKET_CANCEL_GRACE_MS`, and for tasks `DOCKET_TASKS` (`off` hides the
    /// task routes), `DOCKET_TASK_TAIL`, `DOCKET_RETAINED_TASKS`, `DOCKET_MAX_QUEUED_TASKS`, and for
    /// status `DOCKET_HEALTH_INTERVAL_MS` (15000; 0 = off), `DOCKET_HEALTH_STALE_MS`
    /// (3x the interval) and `DOCKET_HEALTH_TIMEOUT_MS` (2000).
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
        let health_interval = env_ms("DOCKET_HEALTH_INTERVAL_MS", 15_000);
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
            tasks_enabled: !matches!(
                std::env::var("DOCKET_TASKS").ok().as_deref(),
                Some("off" | "0" | "false" | "no")
            ),
            cancel_grace: env_ms("DOCKET_CANCEL_GRACE_MS", 750),
            task_tail: env_usize("DOCKET_TASK_TAIL", 4096),
            retained_tasks: env_usize("DOCKET_RETAINED_TASKS", 16),
            max_queued: env_usize("DOCKET_MAX_QUEUED_TASKS", 1),
            health_interval,
            health_stale: env_ms(
                "DOCKET_HEALTH_STALE_MS",
                (health_interval.as_millis() as u64).saturating_mul(3),
            ),
            health_timeout: env_ms("DOCKET_HEALTH_TIMEOUT_MS", 2_000),
        })
    }
}

pub use docket_proto::client::default_socket_path;
