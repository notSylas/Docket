//! End-to-end tests: real daemon on a real Unix socket, driving a fake worker
//! script that speaks the worker protocol.

use docket_proto::api::{ApiError, AskResult, Phase, SourcesResponse, StatusResponse, WorkerState};
use docket_proto::client;
use docketd::config::Config;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tempfile::TempDir;
use tokio::sync::oneshot;

const T: Duration = Duration::from_secs(20);

struct Daemon {
    sock: PathBuf,
    dir: TempDir,
    stop: Option<oneshot::Sender<()>>,
    task: Option<tokio::task::JoinHandle<std::io::Result<()>>>,
}

fn fake_cmd(dir: &Path, extra: &[&str]) -> Vec<String> {
    let script = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/fake_worker.py");
    let mut v = vec!["python3".to_string(), script.to_string()];
    v.push("--pidfile".into());
    v.push(dir.join("pid").display().to_string());
    v.push("--startlog".into());
    v.push(dir.join("starts").display().to_string());
    v.extend(extra.iter().map(|s| s.to_string()));
    v
}

fn base_config(dir: &Path, cmd: Vec<String>) -> Config {
    Config {
        socket: dir.join("rt").join("docket").join("docketd.sock"),
        socket_is_default: true,
        worker_cmd: cmd,
        data_dir: None,
        ask_timeout: Duration::from_secs(10),
        call_timeout: Duration::from_secs(5),
        startup_timeout: Duration::from_secs(10),
        ping_interval: Duration::from_millis(200),
        backoff_base: Duration::from_millis(100),
        backoff_max: Duration::from_millis(400),
    }
}

impl Daemon {
    async fn start(extra: &[&str]) -> Self {
        Self::start_with(extra, |_| {}).await
    }

    async fn start_with(extra: &[&str], tweak: impl FnOnce(&mut Config)) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg = base_config(dir.path(), fake_cmd(dir.path(), extra));
        tweak(&mut cfg);
        Self::start_cfg(dir, cfg).await
    }

    async fn start_cfg(dir: TempDir, cfg: Config) -> Self {
        let sock = cfg.socket.clone();
        let (stop, rx) = oneshot::channel::<()>();
        let task = tokio::spawn(docketd::serve(cfg, async {
            let _ = rx.await;
        }));
        let t0 = Instant::now();
        while !sock.exists() {
            assert!(t0.elapsed() < T, "socket never appeared");
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let d = Self {
            sock,
            dir,
            stop: Some(stop),
            task: Some(task),
        };
        d.wait_worker(WorkerState::Up).await;
        d
    }

    async fn wait_worker(&self, want: WorkerState) {
        let t0 = Instant::now();
        loop {
            if let Ok((200, s)) = self.try_status().await {
                if s.worker.state == want {
                    return;
                }
            }
            assert!(t0.elapsed() < T, "worker never reached {want:?}");
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    async fn try_status(&self) -> std::io::Result<(u16, StatusResponse)> {
        let s = self.sock.clone();
        tokio::task::spawn_blocking(move || {
            let r = client::get(&s, "/v1/status", T)?;
            let code = r.status;
            let body = r.text()?;
            serde_json::from_str(&body)
                .map(|v| (code, v))
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
        })
        .await
        .unwrap()
    }

    async fn status(&self) -> StatusResponse {
        self.try_status().await.unwrap().1
    }

    async fn get(&self, path: &'static str) -> (u16, String) {
        let s = self.sock.clone();
        tokio::task::spawn_blocking(move || {
            let r = client::get(&s, path, T).unwrap();
            (r.status, r.text().unwrap())
        })
        .await
        .unwrap()
    }

    async fn ask(&self, q: &str) -> (u16, String) {
        self.post(&serde_json::json!({ "question": q }).to_string(), None)
            .await
            .into_status_body()
    }

    async fn post(&self, body: &str, accept: Option<&'static str>) -> Posted {
        let s = self.sock.clone();
        let body = body.to_string();
        tokio::task::spawn_blocking(move || {
            let r = client::post_json(&s, "/v1/ask", &body, accept, T).unwrap();
            let status = r.status;
            let rid = r.header("x-request-id").map(String::from);
            let sse = r
                .header("content-type")
                .is_some_and(|c| c.contains("text/event-stream"));
            if sse {
                let evs: Vec<_> = client::sse_events(r.body).map(Result::unwrap).collect();
                Posted {
                    status,
                    rid,
                    body: String::new(),
                    events: evs,
                }
            } else {
                Posted {
                    status,
                    rid,
                    body: r.text().unwrap(),
                    events: vec![],
                }
            }
        })
        .await
        .unwrap()
    }

    async fn stop(&mut self) {
        if let Some(tx) = self.stop.take() {
            let _ = tx.send(());
        }
        if let Some(t) = self.task.take() {
            t.await.unwrap().unwrap();
        }
    }

    fn pid(&self) -> i32 {
        std::fs::read_to_string(self.dir.path().join("pid"))
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }

    fn starts(&self) -> usize {
        std::fs::read_to_string(self.dir.path().join("starts"))
            .map(|s| s.lines().count())
            .unwrap_or(0)
    }
}

struct Posted {
    status: u16,
    rid: Option<String>,
    body: String,
    events: Vec<client::SseEvent>,
}

impl Posted {
    fn into_status_body(self) -> (u16, String) {
        (self.status, self.body)
    }
}

fn err_code(body: &str) -> String {
    serde_json::from_str::<ApiError>(body).unwrap().error.code
}

fn alive(pid: i32) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}

#[tokio::test(flavor = "multi_thread")]
async fn status_sources_and_ask_with_startup_noise() {
    let mut d = Daemon::start(&["--startup-noise"]).await;
    let st = d.status().await;
    assert_eq!(st.worker.model, "fake-model");
    assert_eq!(st.worker.chunks, Some(5));
    assert!(!st.busy);

    let (code, body) = d.get("/v1/sources").await;
    assert_eq!(code, 200);
    let s: SourcesResponse = serde_json::from_str(&body).unwrap();
    assert_eq!(s.sources[0].display_name, "Docs");

    let (code, body) = d.ask("what is the rate").await;
    assert_eq!(code, 200, "{body}");
    let r: AskResult = serde_json::from_str(&body).unwrap();
    assert!(r.answer.contains("2,650") && !r.abstained);
    assert_eq!(r.citations[0].source_display_name, "rates.txt");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn sse_stream_emits_ordered_phases_then_result_with_request_id() {
    let mut d = Daemon::start(&[]).await;
    let p = d
        .post(r#"{"question":"rate?"}"#, Some("text/event-stream"))
        .await;
    assert_eq!(p.status, 200);
    assert!(p.rid.is_some_and(|r| r.len() >= 10));
    let names: Vec<_> = p.events.iter().map(|e| e.event.as_str()).collect();
    assert_eq!(names, ["phase", "phase", "phase", "phase", "result"]);
    let phases: Vec<Phase> = p.events[..4]
        .iter()
        .map(|e| serde_json::from_str::<serde_json::Value>(&e.data).unwrap()["phase"].clone())
        .map(|v| serde_json::from_value(v).unwrap())
        .collect();
    assert_eq!(
        phases,
        [
            Phase::Retrieving,
            Phase::Reading,
            Phase::Answering,
            Phase::Done
        ]
    );
    let r: AskResult = serde_json::from_str(&p.events[4].data).unwrap();
    assert!(r.answer.contains("2,650"));
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stdout_noise_and_malformed_lines_are_ignored() {
    let mut d = Daemon::start(&[]).await;
    let (code, body) = d.ask("noise").await;
    assert_eq!(code, 200, "{body}");
    assert!(serde_json::from_str::<AskResult>(&body)
        .unwrap()
        .answer
        .contains("2,650"));
    assert_eq!(d.starts(), 1, "noise must not restart the worker");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn crash_is_reported_and_worker_restarts() {
    let mut d = Daemon::start(&[]).await;
    let (code, body) = d.ask("crash").await;
    assert_eq!(code, 502);
    assert_eq!(err_code(&body), "worker_crashed");
    // recovers on its own (backoff 100ms)
    let t0 = Instant::now();
    loop {
        let (code, _) = d.ask("again").await;
        if code == 200 {
            break;
        }
        assert!(t0.elapsed() < T, "never recovered");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(d.starts(), 2);
    assert_eq!(d.status().await.worker.restarts, 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn hung_worker_times_out_is_killed_and_replaced() {
    let mut d = Daemon::start_with(&[], |c| c.ask_timeout = Duration::from_millis(600)).await;
    let old = d.pid();
    let (code, body) = d.ask("hang").await;
    assert_eq!(code, 504);
    assert_eq!(err_code(&body), "timeout");
    let t0 = Instant::now();
    while alive(old) {
        assert!(t0.elapsed() < T, "hung worker was not killed");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    d.wait_worker(WorkerState::Up).await;
    assert_ne!(d.pid(), old);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn only_one_question_in_flight() {
    let mut d = Daemon::start(&[]).await;
    let slow = {
        let s = d.sock.clone();
        tokio::task::spawn_blocking(move || {
            let r = client::post_json(&s, "/v1/ask", r#"{"question":"slow"}"#, None, T).unwrap();
            r.status
        })
    };
    let t0 = Instant::now();
    while !d.status().await.busy {
        assert!(t0.elapsed() < T);
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let (code, body) = d.ask("second").await;
    assert_eq!(code, 429);
    assert_eq!(err_code(&body), "busy");
    assert_eq!(slow.await.unwrap(), 200);
    assert!(!d.status().await.busy);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_errors_and_bad_input_map_to_clean_http_errors() {
    let mut d = Daemon::start(&[]).await;
    let (c, b) = d.ask("rpcerr").await;
    assert_eq!((c, err_code(&b).as_str()), (409, "no_content"));
    let (c, b) = d.ask("badshape").await;
    assert_eq!((c, err_code(&b).as_str()), (502, "worker_error"));
    let (c, b) = d.ask("   ").await;
    assert_eq!((c, err_code(&b).as_str()), (400, "bad_request"));
    let p = d.post("not json", None).await;
    assert_eq!(p.status, 400);
    let big = format!(r#"{{"question":"{}"}}"#, "a".repeat(100_000));
    assert_eq!(d.post(&big, None).await.status, 413);
    assert_eq!(d.get("/nope").await.0, 404);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn socket_and_directory_permissions() {
    let mut d = Daemon::start(&[]).await;
    let sock_mode = std::fs::metadata(&d.sock).unwrap().mode() & 0o777;
    let dir_mode = std::fs::metadata(d.sock.parent().unwrap()).unwrap().mode() & 0o777;
    assert_eq!(sock_mode, 0o600);
    assert_eq!(dir_mode, 0o700);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn clean_shutdown_removes_socket_and_kills_worker() {
    let mut d = Daemon::start(&[]).await;
    let pid = d.pid();
    assert!(alive(pid));
    d.stop().await;
    assert!(!d.sock.exists());
    let t0 = Instant::now();
    while alive(pid) {
        assert!(
            t0.elapsed() < Duration::from_secs(5),
            "worker outlived daemon"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn second_daemon_refused_and_stale_socket_replaced() {
    let mut d = Daemon::start(&[]).await;
    let dir2 = tempfile::tempdir().unwrap();
    let mut cfg2 = base_config(dir2.path(), fake_cmd(dir2.path(), &[]));
    cfg2.socket = d.sock.clone();
    let err = docketd::serve(cfg2, std::future::pending())
        .await
        .unwrap_err();
    assert_eq!(err.kind(), std::io::ErrorKind::AddrInUse);
    d.stop().await;

    // leave a stale socket file behind (listener dropped without cleanup)
    let dir3 = tempfile::tempdir().unwrap();
    let cfg3 = base_config(dir3.path(), fake_cmd(dir3.path(), &[]));
    std::fs::create_dir_all(cfg3.socket.parent().unwrap()).unwrap();
    std::fs::set_permissions(
        cfg3.socket.parent().unwrap(),
        std::fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    drop(std::os::unix::net::UnixListener::bind(&cfg3.socket).unwrap());
    assert!(cfg3.socket.exists());
    let mut d3 = Daemon::start_cfg(dir3, cfg3).await;
    assert_eq!(d3.status().await.service, "ok");
    d3.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn missing_worker_binary_is_unavailable_not_fatal() {
    let dir = tempfile::tempdir().unwrap();
    let cfg = base_config(dir.path(), vec!["/nonexistent/docket-worker".into()]);
    let sock = cfg.socket.clone();
    let (stop, rx) = oneshot::channel::<()>();
    let task = tokio::spawn(docketd::serve(cfg, async {
        let _ = rx.await;
    }));
    while !sock.exists() {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let d = Daemon {
        sock,
        dir,
        stop: Some(stop),
        task: Some(task),
    };
    // may be momentarily Starting while a retry is mid-spawn
    d.wait_worker(WorkerState::Down).await;
    assert_eq!(d.status().await.service, "ok");
    let (code, body) = d.get("/v1/sources").await;
    assert_eq!(code, 503, "{body}");
    let mut d = d;
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_exiting_at_startup_does_not_wedge_the_daemon() {
    let mut d = {
        let dir = tempfile::tempdir().unwrap();
        let cfg = base_config(dir.path(), fake_cmd(dir.path(), &["--exit-immediately"]));
        let sock = cfg.socket.clone();
        let (stop, rx) = oneshot::channel::<()>();
        let task = tokio::spawn(docketd::serve(cfg, async {
            let _ = rx.await;
        }));
        while !sock.exists() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        Daemon {
            sock,
            dir,
            stop: Some(stop),
            task: Some(task),
        }
    };
    let (code, _) = d.ask("anything").await;
    assert_eq!(code, 503);
    assert_eq!(d.status().await.service, "ok");
    d.stop().await;
}
