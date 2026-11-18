//! End-to-end tests: real daemon on a real Unix socket, driving a fake worker
//! script that speaks the worker protocol.

use docket_proto::api::{ApiError, AskResult, Phase, SourcesResponse, StatusResponse, WorkerState};
use docket_proto::client;
use docket_proto::task::{
    parse_task_event, CancelResponse, CancelSource, CreateTaskResponse, InferenceStatus, TaskEvent,
    TaskEventBody, TaskInfo, TaskState, TasksResponse, WorkerExit,
};
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
        tasks_enabled: true,
        cancel_grace: Duration::from_millis(500),
        task_tail: 1024,
        retained_tasks: 16,
        max_queued: 1,
        // Off by default so existing tests see no extra worker traffic.
        health_interval: Duration::ZERO,
        health_stale: Duration::from_millis(600),
        health_timeout: Duration::from_secs(2),
        evidence_timeout: Duration::from_secs(3),
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
async fn sse_stream_true_forwards_deltas_between_phases_and_result() {
    let mut d = Daemon::start(&[]).await;
    let p = d
        .post(
            r#"{"question":"rate?","stream":true}"#,
            Some("text/event-stream"),
        )
        .await;
    assert_eq!(p.status, 200);
    let names: Vec<_> = p.events.iter().map(|e| e.event.as_str()).collect();
    assert_eq!(
        names,
        ["phase", "phase", "phase", "delta", "delta", "delta", "phase", "phase", "result"]
    );
    let text: String = p
        .events
        .iter()
        .filter(|e| e.event == "delta")
        .map(|e| {
            serde_json::from_str::<serde_json::Value>(&e.data).unwrap()["text"]
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect();
    assert_eq!(text, "The rate is 2,650 [rates.txt#1].");
    let phases: Vec<String> = p
        .events
        .iter()
        .filter(|e| e.event == "phase")
        .map(|e| {
            serde_json::from_str::<serde_json::Value>(&e.data).unwrap()["phase"]
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect();
    assert_eq!(
        phases,
        ["retrieving", "reading", "answering", "validating", "done"]
    );
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stream_flag_is_ignored_for_plain_json_and_absent_on_old_clients() {
    let mut d = Daemon::start(&[]).await;
    let (c, body) = d
        .post(r#"{"question":"rate?","stream":true}"#, None)
        .await
        .into_status_body();
    assert_eq!(c, 200);
    assert!(body.contains("2,650"));
    // SSE without `stream` keeps the old shape: no delta events.
    let p = d
        .post(r#"{"question":"rate?"}"#, Some("text/event-stream"))
        .await;
    assert!(p.events.iter().all(|e| e.event != "delta"));
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

// ---------------------------------------------------------------------------
// R1: tasks, cancellation, stale-event safety
// ---------------------------------------------------------------------------

impl Daemon {
    async fn http(
        &self,
        method: &'static str,
        path: String,
        body: Option<String>,
        headers: Vec<(&'static str, String)>,
    ) -> (u16, String) {
        let s = self.sock.clone();
        tokio::task::spawn_blocking(move || {
            let h: Vec<(&str, &str)> = headers.iter().map(|(k, v)| (*k, v.as_str())).collect();
            let r = client::request(
                &s,
                method,
                &path,
                &h,
                body.as_deref().map(str::as_bytes),
                Some(T),
            )
            .unwrap();
            (r.status, r.text().unwrap())
        })
        .await
        .unwrap()
    }

    async fn create_task(
        &self,
        question: &str,
        stream: bool,
        deadline_ms: Option<u64>,
    ) -> (u16, String) {
        let mut b = serde_json::json!({"kind":"ask","question":question,"stream":stream});
        if let Some(d) = deadline_ms {
            b["deadline_ms"] = d.into();
        }
        self.http("POST", "/v1/tasks".into(), Some(b.to_string()), vec![])
            .await
    }

    async fn new_task(&self, question: &str, stream: bool) -> String {
        let (c, b) = self.create_task(question, stream, None).await;
        assert_eq!(c, 202, "{b}");
        serde_json::from_str::<CreateTaskResponse>(&b)
            .unwrap()
            .task_id
            .0
    }

    async fn cancel_task(&self, id: &str) -> (u16, String) {
        self.http("POST", format!("/v1/tasks/{id}/cancel"), None, vec![])
            .await
    }

    async fn tasks(&self) -> Vec<TaskInfo> {
        let (c, b) = self.http("GET", "/v1/tasks".into(), None, vec![]).await;
        assert_eq!(c, 200, "{b}");
        serde_json::from_str::<TasksResponse>(&b).unwrap().tasks
    }

    async fn task(&self, id: &str) -> TaskInfo {
        self.tasks()
            .await
            .into_iter()
            .find(|t| t.task_id.0 == id)
            .unwrap()
    }

    async fn wait_task(&self, id: &str, what: &str, pred: impl Fn(&TaskInfo) -> bool) -> TaskInfo {
        let t0 = Instant::now();
        loop {
            let t = self.task(id).await;
            if pred(&t) {
                return t;
            }
            assert!(
                t0.elapsed() < T,
                "task never reached: {what} (at {:?})",
                t.state
            );
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }

    /// Follow a task's event stream to its end on a background thread.
    fn collect_events(
        &self,
        id: &str,
        last_event_id: Option<u64>,
    ) -> tokio::task::JoinHandle<Vec<TaskEvent>> {
        let s = self.sock.clone();
        let path = format!("/v1/tasks/{id}/events");
        tokio::task::spawn_blocking(move || {
            let lid = last_event_id.map(|n| n.to_string());
            let mut h: Vec<(&str, &str)> = vec![];
            if let Some(l) = &lid {
                h.push(("Last-Event-ID", l));
            }
            let r = client::request(&s, "GET", &path, &h, None, Some(T)).unwrap();
            assert_eq!(r.status, 200);
            client::sse_events(r.body)
                .map(Result::unwrap)
                .filter_map(|e| parse_task_event(&e.data))
                .collect()
        })
    }

    async fn events(&self, id: &str) -> Vec<TaskEvent> {
        self.collect_events(id, None).await.unwrap()
    }
}

fn states(evs: &[TaskEvent]) -> Vec<TaskState> {
    evs.iter()
        .filter_map(|e| match e.body {
            TaskEventBody::State { state } => Some(state),
            _ => None,
        })
        .collect()
}

fn assert_contiguous(evs: &[TaskEvent]) {
    for (i, e) in evs.iter().enumerate() {
        assert_eq!(e.seq, i as u64 + 1, "seq must be 1,2,3...");
    }
}

fn pos(evs: &[TaskEvent], f: impl Fn(&TaskEventBody) -> bool) -> Option<usize> {
    evs.iter().position(|e| f(&e.body))
}

async fn wait_dead(pid: i32, what: &str) {
    let t0 = Instant::now();
    while alive(pid) {
        assert!(
            t0.elapsed() < Duration::from_secs(10),
            "{what}: pid {pid} still alive"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

/// A restart backoff this long would make "next question works" fail if a
/// user cancel were (wrongly) counted as a failure.
fn long_backoff(c: &mut Config) {
    c.backoff_base = Duration::from_secs(30);
    c.backoff_max = Duration::from_secs(30);
}

fn assert_cancel_semantics(evs: &[TaskEvent], by: CancelSource) {
    assert_contiguous(evs);
    let req = pos(
        evs,
        |b| matches!(b, TaskEventBody::CancelRequested { by: x } if *x == by),
    )
    .expect("cancel_requested");
    let term = pos(evs, |b| matches!(b, TaskEventBody::WorkerTerminated { .. }))
        .expect("worker_terminated");
    let inf = pos(evs, |b| {
        matches!(
            b,
            TaskEventBody::InferenceStopped {
                status: InferenceStatus::Unverified
            }
        )
    })
    .expect("inference_stopped is always unverified");
    assert!(
        req < term && term < inf,
        "cancel_requested, worker_terminated, inference_stopped in order"
    );
    assert_eq!(states(evs).last(), Some(&TaskState::Cancelled));
    assert!(states(evs).contains(&TaskState::Cancelling));
    // The three facts are distinct events; there is no other claim about inference.
    assert_eq!(
        evs.iter()
            .filter(|e| matches!(e.body, TaskEventBody::InferenceStopped { .. }))
            .count(),
        1
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn cancel_stalled_worker_is_bounded_kills_worker_and_next_question_works() {
    // Long backoff: a cancel counted as a failure would block the next question.
    let mut d = Daemon::start_with(&[], long_backoff).await;
    let old = d.pid();
    let id = d.new_task("hang", false).await;
    d.wait_task(&id, "running", |t| {
        t.state == TaskState::Running && t.last_seq >= 3
    })
    .await;
    let collector = d.collect_events(&id, None);
    let t0 = Instant::now();
    let (code, body) = d.cancel_task(&id).await;
    assert_eq!(code, 200, "{body}");
    let c: CancelResponse = serde_json::from_str(&body).unwrap();
    assert!(
        c.accepted && c.state == TaskState::Cancelling,
        "cancelling is not cancelled"
    );
    let evs = collector.await.unwrap();
    assert!(
        t0.elapsed() < Duration::from_secs(3),
        "cancel took {:?}",
        t0.elapsed()
    );
    assert_cancel_semantics(&evs, CancelSource::User);
    assert!(matches!(
        evs.iter().find_map(|e| match &e.body {
            TaskEventBody::WorkerTerminated { how } => Some(*how),
            _ => None,
        }),
        Some(WorkerExit::Exited | WorkerExit::Killed)
    ));
    wait_dead(old, "cancelled worker").await;
    // Restart without penalty: the next question is served right away.
    let t1 = Instant::now();
    let (code, body) = d.ask("again").await;
    assert_eq!(code, 200, "{body}");
    assert!(
        t1.elapsed() < Duration::from_secs(10),
        "no backoff after a user cancel"
    );
    assert!(serde_json::from_str::<AskResult>(&body)
        .unwrap()
        .answer
        .contains("2,650"));
    assert_eq!(d.starts(), 2);
    assert_eq!(d.status().await.worker.restarts, 1);
    assert!(!d.status().await.busy, "permit released");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn cancel_mid_stream_discards_later_output_and_leaves_no_result() {
    let mut d = Daemon::start_with(&[], long_backoff).await;
    let id = d.new_task("stream_stall", true).await;
    d.wait_task(&id, "first delta", |t| t.last_seq >= 6).await;
    let collector = d.collect_events(&id, None);
    assert_eq!(d.cancel_task(&id).await.0, 200);
    let evs = collector.await.unwrap();
    assert_cancel_semantics(&evs, CancelSource::User);
    assert!(evs
        .iter()
        .any(|e| matches!(&e.body, TaskEventBody::Delta { text } if text == "partial ")));
    assert!(pos(&evs, |b| matches!(b, TaskEventBody::Result { .. })).is_none());
    let (code, _) = d.ask("after stop").await;
    assert_eq!(code, 200);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_late_result_after_cancel_is_never_published() {
    // The fake worker traps SIGTERM, then 0.3 s later prints a delta and a
    // result for the task it was asked to abandon, and only then exits. The
    // grace period is long enough for that to happen.
    let mut d = Daemon::start_with(&[], |c| {
        long_backoff(c);
        c.cancel_grace = Duration::from_millis(2000);
    })
    .await;
    let id = d.new_task("late", true).await;
    d.wait_task(&id, "first delta", |t| t.last_seq >= 6).await;
    let collector = d.collect_events(&id, None);
    d.cancel_task(&id).await;
    let live = collector.await.unwrap();
    assert!(
        d.dir.path().join("late-marker").exists(),
        "the fake worker really emitted its late output"
    );
    assert_cancel_semantics(&live, CancelSource::User);
    assert!(matches!(
        live.iter().find_map(|e| match &e.body {
            TaskEventBody::WorkerTerminated { how } => Some(*how),
            _ => None,
        }),
        Some(WorkerExit::Exited)
    ));
    // Neither the live stream nor a later replay of the retained tail holds it.
    let replay = d.events(&id).await;
    assert_eq!(replay, live, "replay equals what was streamed");
    for evs in [&live, &replay] {
        assert!(pos(evs, |b| matches!(b, TaskEventBody::Result { .. })).is_none());
        let text: String = evs
            .iter()
            .filter_map(|e| match &e.body {
                TaskEventBody::Delta { text } => Some(text.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(text, "partial ", "no delta after the cancel request");
        assert!(!serde_json::to_string(evs).unwrap().contains("LATE"));
    }
    // And the next question gets its own, correct answer.
    let (code, body) = d.ask("next").await;
    assert_eq!(code, 200, "{body}");
    assert!(!body.contains("LATE"));
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_that_ignores_sigterm_is_killed_after_the_grace_period() {
    let mut d = Daemon::start_with(&[], |c| {
        long_backoff(c);
        c.cancel_grace = Duration::from_millis(300);
    })
    .await;
    let old = d.pid();
    let id = d.new_task("stubborn", false).await;
    d.wait_task(&id, "stalled", |t| t.last_seq >= 5).await;
    let collector = d.collect_events(&id, None);
    let t0 = Instant::now();
    d.cancel_task(&id).await;
    let evs = collector.await.unwrap();
    assert!(t0.elapsed() < Duration::from_secs(3));
    assert_cancel_semantics(&evs, CancelSource::User);
    assert!(pos(&evs, |b| matches!(
        b,
        TaskEventBody::WorkerTerminated {
            how: WorkerExit::Killed
        }
    ))
    .is_some());
    wait_dead(old, "stubborn worker").await;
    d.stop().await;
}

fn child_pid(d: &Daemon) -> i32 {
    let t0 = Instant::now();
    loop {
        if let Ok(s) = std::fs::read_to_string(d.dir.path().join("childpid")) {
            if let Ok(p) = s.trim().parse() {
                return p;
            }
        }
        assert!(t0.elapsed() < T, "child pid never written");
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn cancel_and_timeout_kill_the_whole_process_group_no_orphans() {
    let mut d = Daemon::start_with(&[], |c| {
        c.cancel_grace = Duration::from_millis(300);
    })
    .await;
    // Cancel.
    let id = d.new_task("child", false).await;
    d.wait_task(&id, "stalled", |t| t.last_seq >= 5).await;
    let kid = child_pid(&d);
    assert!(alive(kid));
    let collector = d.collect_events(&id, None);
    d.cancel_task(&id).await;
    collector.await.unwrap();
    wait_dead(kid, "grandchild after cancel").await;
    // Deadline.
    std::fs::remove_file(d.dir.path().join("childpid")).unwrap();
    d.wait_worker(WorkerState::Up).await;
    let id = d.create_task("child", false, Some(700)).await;
    assert_eq!(id.0, 202);
    let id = serde_json::from_str::<CreateTaskResponse>(&id.1)
        .unwrap()
        .task_id
        .0;
    let kid = child_pid(&d);
    let evs = d.events(&id).await;
    assert_eq!(states(&evs).last(), Some(&TaskState::TimedOut));
    wait_dead(kid, "grandchild after timeout").await;
    d.stop().await;
}

impl Daemon {
    /// The id of the newest task (tasks created through `/v1/ask` included).
    async fn newest_task(&self) -> String {
        let t0 = Instant::now();
        loop {
            if let Some(t) = self.tasks().await.first() {
                return t.task_id.0.clone();
            }
            assert!(t0.elapsed() < T, "no task appeared");
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn client_disconnect_mid_stream_cancels_with_client_gone_and_frees_the_worker() {
    let mut d = Daemon::start_with(&[], long_backoff).await;
    let old = d.pid();
    // SSE /v1/ask: read the first delta, then hang up.
    let s = d.sock.clone();
    tokio::task::spawn_blocking(move || {
        let r = client::post_json(
            &s,
            "/v1/ask",
            r#"{"question":"stream_stall","stream":true}"#,
            Some("text/event-stream"),
            T,
        )
        .unwrap();
        let mut it = client::sse_events(r.body);
        loop {
            if it.next().unwrap().unwrap().event == "delta" {
                break;
            }
        }
    })
    .await
    .unwrap();
    let id = d.newest_task().await;
    let t = d
        .wait_task(&id, "cancelled", |t| t.state == TaskState::Cancelled)
        .await;
    assert!(t.last_seq > 6);
    let evs = d.events(&id).await;
    assert_cancel_semantics(&evs, CancelSource::ClientGone);
    wait_dead(old, "worker after disconnect").await;
    // The permit is free: the next question is accepted and served at once.
    let (code, body) = d.ask("after hangup").await;
    assert_eq!(code, 200, "{body}");

    // Same for a plain JSON /v1/ask whose client gives up (read timeout).
    let s = d.sock.clone();
    tokio::task::spawn_blocking(move || {
        let r = client::post_json(
            &s,
            "/v1/ask",
            r#"{"question":"hang"}"#,
            None,
            Duration::from_millis(400),
        );
        assert!(r.is_err() || r.unwrap().text().is_err());
    })
    .await
    .unwrap();
    let t0 = Instant::now();
    loop {
        let tasks = d.tasks().await;
        if tasks[0].state == TaskState::Cancelled && tasks[0].task_id.0 != id {
            break;
        }
        assert!(t0.elapsed() < T, "plain-JSON disconnect did not cancel");
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    let (code, _) = d.ask("and again").await;
    assert_eq!(code, 200);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn deadline_gives_timed_out_not_failed() {
    let mut d = Daemon::start_with(&[], |c| c.ask_timeout = Duration::from_millis(600)).await;
    let old = d.pid();
    let id = d.new_task("hang", false).await;
    let evs = d.events(&id).await;
    assert_contiguous(&evs);
    assert_eq!(states(&evs).last(), Some(&TaskState::TimedOut));
    assert!(!states(&evs).contains(&TaskState::Failed));
    assert!(evs
        .iter()
        .any(|e| matches!(&e.body, TaskEventBody::Error { code, .. } if code == "timeout")));
    assert!(pos(&evs, |b| matches!(b, TaskEventBody::CancelRequested { .. })).is_none());
    wait_dead(old, "hung worker").await;
    d.wait_worker(WorkerState::Up).await;
    assert_eq!(d.ask("after timeout").await.0, 200);

    // A per-task deadline lowers the limit; it cannot raise it above the daemon's.
    let (c, b) = d.create_task("hang", false, Some(200)).await;
    assert_eq!(c, 202);
    let short = serde_json::from_str::<CreateTaskResponse>(&b)
        .unwrap()
        .task_id
        .0;
    assert_eq!(d.task(&short).await.deadline_ms, 200);
    let (c, b) = d.create_task("slow", false, Some(3_600_000)).await;
    assert_eq!(c, 202);
    let capped = serde_json::from_str::<CreateTaskResponse>(&b)
        .unwrap()
        .task_id
        .0;
    assert_eq!(d.task(&capped).await.deadline_ms, 600);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_crash_fails_the_task_with_a_typed_reason_and_the_worker_restarts() {
    let mut d = Daemon::start(&[]).await;
    let id = d.new_task("crash", false).await;
    let evs = d.events(&id).await;
    assert_contiguous(&evs);
    assert_eq!(states(&evs).last(), Some(&TaskState::Failed));
    assert!(evs
        .iter()
        .any(|e| matches!(&e.body, TaskEventBody::Error { code, .. } if code == "worker_crashed")));
    assert!(pos(&evs, |b| matches!(
        b,
        TaskEventBody::InferenceStopped { .. }
    ))
    .is_none());
    let t0 = Instant::now();
    loop {
        if d.ask("again").await.0 == 200 {
            break;
        }
        assert!(t0.elapsed() < T, "never recovered");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(d.starts(), 2);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn normal_task_streams_in_order_with_replay_and_capability() {
    let mut d = Daemon::start(&[]).await;
    assert!(d
        .status()
        .await
        .capabilities
        .contains(&"tasks.v1".to_string()));
    let id = d.new_task("rate?", true).await;
    let evs = d.events(&id).await;
    assert_contiguous(&evs);
    assert_eq!(
        states(&evs),
        [TaskState::Queued, TaskState::Running, TaskState::Succeeded]
    );
    let text: String = evs
        .iter()
        .filter_map(|e| match &e.body {
            TaskEventBody::Delta { text } => Some(text.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(text, "The rate is 2,650 [rates.txt#1].");
    let result_at = pos(&evs, |b| matches!(b, TaskEventBody::Result { .. })).unwrap();
    assert!(
        matches!(evs[result_at].body, TaskEventBody::Result { ref result } if !result.abstained && result.citations.len() == 1)
    );
    assert!(evs.iter().all(|e| e.task_id.0 == id));
    // Last-Event-ID: exactly the missed tail, in order, no duplicates.
    let tail = d.collect_events(&id, Some(4)).await.unwrap();
    assert_eq!(tail, evs[4..].to_vec());
    assert!(d
        .collect_events(&id, Some(evs.len() as u64))
        .await
        .unwrap()
        .is_empty());
    // The raw SSE carries the seq as the event id.
    let (c, raw) = d
        .http(
            "GET",
            format!("/v1/tasks/{id}/events"),
            None,
            vec![("Last-Event-ID", "2".into())],
        )
        .await;
    assert_eq!(c, 200);
    assert!(raw.contains("id: 3\n") && !raw.contains("id: 2\n") && raw.contains("event: state"));
    // Listing.
    let t = d.task(&id).await;
    assert_eq!(t.state, TaskState::Succeeded);
    assert_eq!(t.last_seq, evs.len() as u64);
    // Stdout noise from the worker never reaches an event.
    let id = d.new_task("noise", false).await;
    let evs = d.events(&id).await;
    assert_eq!(states(&evs).last(), Some(&TaskState::Succeeded));
    assert!(!serde_json::to_string(&evs).unwrap().contains("chatter"));
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn old_ask_clients_see_exactly_the_old_vocabulary() {
    let mut d = Daemon::start(&[]).await;
    let p = d
        .post(
            r#"{"question":"rate?","stream":true}"#,
            Some("text/event-stream"),
        )
        .await;
    for e in &p.events {
        assert!(
            ["phase", "delta", "result", "error"].contains(&e.event.as_str()),
            "{}",
            e.event
        );
        assert!(
            !e.data.contains("task_id") && !e.data.contains("\"seq\""),
            "{}",
            e.data
        );
    }
    assert_eq!(p.events[0].data, r#"{"phase":"retrieving"}"#);
    assert_eq!(p.events[3].event, "delta");
    assert_eq!(p.events[3].data, r#"{"text":"The rate "}"#);
    // The old decoder and a newer event name: unknown events are skipped.
    assert!(docket_proto::api::parse_ask_event("state", r#"{"state":"running"}"#).is_none());
    assert!(docket_proto::api::parse_ask_event("cancel_requested", "{}").is_none());
    // Errors keep the old envelope with the request id.
    let p = d
        .post(r#"{"question":"crash"}"#, Some("text/event-stream"))
        .await;
    let last = p.events.last().unwrap();
    assert_eq!(last.event, "error");
    let e: ApiError = serde_json::from_str(&last.data).unwrap();
    assert_eq!(e.error.code, "worker_crashed");
    assert_eq!(Some(e.error.request_id), p.rid);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn cancel_of_finished_unknown_and_double_cancel_is_idempotent() {
    let mut d = Daemon::start_with(&[], long_backoff).await;
    // Finished task.
    let done = d.new_task("rate?", false).await;
    d.events(&done).await;
    let (c, b) = d.cancel_task(&done).await;
    assert_eq!(c, 200);
    let r: CancelResponse = serde_json::from_str(&b).unwrap();
    assert!(!r.accepted && r.state == TaskState::Succeeded);
    // Unknown task.
    assert_eq!(d.cancel_task("t-nope").await.0, 404);
    assert_eq!(
        d.http("GET", "/v1/tasks/t-nope/events".into(), None, vec![])
            .await
            .0,
        404
    );
    // Double cancel of a running one.
    let id = d.new_task("hang", false).await;
    d.wait_task(&id, "running", |t| t.last_seq >= 3).await;
    let collector = d.collect_events(&id, None);
    let first: CancelResponse = serde_json::from_str(&d.cancel_task(&id).await.1).unwrap();
    let second = d.cancel_task(&id).await;
    assert_eq!(second.0, 200);
    let second: CancelResponse = serde_json::from_str(&second.1).unwrap();
    assert!(first.accepted && !second.accepted);
    let evs = collector.await.unwrap();
    assert_cancel_semantics(&evs, CancelSource::User);
    assert_eq!(
        evs.iter()
            .filter(|e| matches!(e.body, TaskEventBody::CancelRequested { .. }))
            .count(),
        1
    );
    // After it finished: still 200, not accepted, state cancelled.
    let third: CancelResponse = serde_json::from_str(&d.cancel_task(&id).await.1).unwrap();
    assert!(!third.accepted && third.state == TaskState::Cancelled);
    let t0 = Instant::now();
    while d.starts() < 2 {
        assert!(t0.elapsed() < T, "worker was not restarted");
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    d.wait_worker(WorkerState::Up).await;
    assert_eq!(d.starts(), 2, "double cancel restarts the worker once");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn queued_task_cancel_never_touches_the_worker_and_the_queue_is_bounded() {
    let mut d = Daemon::start(&[]).await;
    let pid = d.pid();
    let running = d.new_task("slow", false).await;
    d.wait_task(&running, "running", |t| t.state == TaskState::Running)
        .await;
    let queued = d.new_task("rate?", false).await;
    assert_eq!(d.task(&queued).await.state, TaskState::Queued);
    // The wait queue holds one; a third is refused like /v1/ask refuses.
    let (c, b) = d.create_task("third", false, None).await;
    assert_eq!((c, err_code(&b).as_str()), (429, "busy"));
    assert_eq!(d.ask("via ask").await.0, 429);
    let collector = d.collect_events(&queued, None);
    let (c, b) = d.cancel_task(&queued).await;
    assert_eq!(c, 200);
    assert!(serde_json::from_str::<CancelResponse>(&b).unwrap().accepted);
    let evs = collector.await.unwrap();
    assert_eq!(
        states(&evs),
        [
            TaskState::Queued,
            TaskState::Cancelling,
            TaskState::Cancelled
        ]
    );
    assert!(pos(&evs, |b| matches!(
        b,
        TaskEventBody::WorkerTerminated { .. } | TaskEventBody::InferenceStopped { .. }
    ))
    .is_none());
    // The running task is unaffected and finishes; the worker was never restarted.
    let evs = d.events(&running).await;
    assert_eq!(states(&evs).last(), Some(&TaskState::Succeeded));
    assert_eq!((d.pid(), d.starts()), (pid, 1));
    // A queued task that is NOT cancelled runs after the running one.
    let a = d.new_task("slow", false).await;
    d.wait_task(&a, "running", |t| t.state == TaskState::Running)
        .await;
    let b = d.new_task("rate?", false).await;
    assert_eq!(
        states(&d.events(&b).await).last(),
        Some(&TaskState::Succeeded)
    );
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn shutdown_during_a_task_cancels_cleanly_and_leaves_no_orphan() {
    let mut d = Daemon::start(&[]).await;
    let pid = d.pid();
    let id = d.new_task("child", true).await;
    d.wait_task(&id, "stalled", |t| t.last_seq >= 6).await;
    let kid = child_pid(&d);
    let collector = d.collect_events(&id, None);
    d.stop().await; // returns only once the daemon has stopped
    let evs = collector.await.unwrap();
    assert_cancel_semantics(&evs, CancelSource::Shutdown);
    assert!(!d.sock.exists());
    wait_dead(pid, "worker").await;
    wait_dead(kid, "worker's child").await;
    assert_eq!(
        d.starts(),
        1,
        "no replacement worker is started while shutting down"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn tasks_off_hides_routes_and_capability_but_ask_still_works() {
    let mut d = Daemon::start_with(&[], |c| c.tasks_enabled = false).await;
    let (_, status) = d.get("/v1/status").await;
    assert!(
        !status.contains("capabilities") && !status.contains("tasks.v1"),
        "{status}"
    );
    assert_eq!(d.get("/v1/tasks").await.0, 404);
    assert_eq!(d.create_task("q", false, None).await.0, 404);
    assert_eq!(d.cancel_task("t-1").await.0, 404);
    let (code, body) = d.ask("rate?").await;
    assert_eq!(code, 200, "{body}");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn task_routes_enforce_the_same_input_limits() {
    let mut d = Daemon::start(&[]).await;
    let big = format!(r#"{{"kind":"ask","question":"{}"}}"#, "a".repeat(100_000));
    assert_eq!(
        d.http("POST", "/v1/tasks".into(), Some(big.clone()), vec![])
            .await
            .0,
        413
    );
    assert_eq!(
        d.http("POST", "/v1/tasks/t-1/cancel".into(), Some(big), vec![])
            .await
            .0,
        413
    );
    let (c, b) = d
        .http(
            "POST",
            "/v1/tasks".into(),
            Some(r#"{"kind":"ingest","question":"q"}"#.into()),
            vec![],
        )
        .await;
    assert_eq!((c, err_code(&b).as_str()), (400, "bad_request"));
    assert_eq!(d.create_task("   ", false, None).await.0, 400);
    let long = "a".repeat(9_000);
    assert_eq!(d.create_task(&long, false, None).await.0, 400);
    assert_eq!(
        d.http("POST", "/v1/tasks".into(), Some("nope".into()), vec![])
            .await
            .0,
        400
    );
    assert_eq!(
        d.http(
            "POST",
            "/v1/tasks/t-1/cancel".into(),
            Some("nope".into()),
            vec![]
        )
        .await
        .0,
        400
    );
    // Error bodies never echo the question.
    let (_, b) = d.create_task("   ", false, None).await;
    assert!(!b.contains("aaaa"));
    d.stop().await;
}

// ---------------------------------------------------------------------------
// R2a: status, health freshness, source readiness, answer metadata
// ---------------------------------------------------------------------------

const HOSTILE_BITS: [&str; 3] = ["SECRET-9917", "/home/alice", "payroll"];

fn assert_no_leak(what: &str, body: &str) {
    for bit in HOSTILE_BITS {
        assert!(!body.contains(bit), "{what} leaked {bit:?}: {body}");
    }
}

fn health_cfg(c: &mut Config) {
    c.health_interval = Duration::from_millis(100);
    c.health_stale = Duration::from_millis(600);
    c.health_timeout = Duration::from_secs(2);
}

impl Daemon {
    fn healthlog_path(&self) -> PathBuf {
        self.dir.path().join("healthlog")
    }

    fn health_calls(&self) -> usize {
        std::fs::read_to_string(self.healthlog_path())
            .map(|s| s.lines().count())
            .unwrap_or(0)
    }

    async fn wait_status(
        &self,
        what: &str,
        pred: impl Fn(&StatusResponse) -> bool,
    ) -> StatusResponse {
        let t0 = Instant::now();
        loop {
            let s = self.status().await;
            if pred(&s) {
                return s;
            }
            assert!(t0.elapsed() < T, "status never reached: {what}");
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    async fn healthy(&self) -> StatusResponse {
        self.wait_status("models section present", |s| {
            s.health
                .as_ref()
                .is_some_and(|h| h.sections.models.is_some())
        })
        .await
    }
}

/// Start with the health-capable fake worker; `extra` adds scenario flags.
async fn start_health(extra: &[&str], tweak: impl FnOnce(&mut Config)) -> Daemon {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("healthlog").display().to_string();
    let mut args: Vec<&str> = vec!["--health", "--healthlog", &log];
    args.extend_from_slice(extra);
    let mut cfg = base_config(dir.path(), fake_cmd(dir.path(), &args));
    health_cfg(&mut cfg);
    tweak(&mut cfg);
    Daemon::start_cfg(dir, cfg).await
}

#[tokio::test(flavor = "multi_thread")]
async fn old_worker_gets_no_status_v2_no_health_calls_and_the_old_status_document() {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("healthlog").display().to_string();
    let mut cfg = base_config(dir.path(), fake_cmd(dir.path(), &["--healthlog", &log]));
    health_cfg(&mut cfg);
    let mut d = Daemon::start_cfg(dir, cfg).await;
    tokio::time::sleep(Duration::from_millis(700)).await;
    let (_, body) = d.get("/v1/status").await;
    assert!(
        !body.contains("status.v2") && !body.contains("health"),
        "{body}"
    );
    assert!(body.contains("tasks.v1"));
    assert_eq!(d.health_calls(), 0, "an old worker is never sent `health`");
    let (_, src) = d.get("/v1/sources").await;
    assert!(
        !src.contains("readiness") && !src.contains("counts"),
        "{src}"
    );
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn capable_worker_advertises_status_v2_and_serves_fresh_sections() {
    let mut d = start_health(&[], |_| {}).await;
    let s = d.healthy().await;
    assert!(s.has_capability("status.v2") && s.has_capability("tasks.v1"));
    let h = s.health.unwrap();
    let models = h.sections.models.unwrap();
    assert_eq!(models.data.answer.name, "fake-model");
    assert_eq!(models.data.answer.resident, docket_proto::status::Tri::Yes);
    assert_eq!(models.data.embed.resident, docket_proto::status::Tri::No);
    assert_eq!(models.stale, Some(false));
    assert!(models.checked_at_ms.is_some() && models.age_ms.is_some());
    let p = h.sections.process.unwrap();
    assert!(p.data.features.iter().any(|f| f == "health.v1"));
    assert_eq!(h.worker_process.state, WorkerState::Up);
    assert_eq!(
        h.worker_process.responsiveness,
        docket_proto::status::Responsiveness::Responsive
    );
    assert_eq!(h.worker_process.crashes, 0);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn scenarios_are_reported_with_states_codes_and_source_readiness() {
    use docket_proto::status::{Readiness, SectionState, Tri};
    for (scenario, ready, check) in [
        ("db_unavailable", Readiness::Ready, 0),
        ("model_missing", Readiness::Ready, 1),
        ("indexing", Readiness::Indexing, 2),
        ("partial", Readiness::Partial, 3),
        ("empty_ready", Readiness::Partial, 3),
    ] {
        let mut d = start_health(&["--scenario", scenario], |_| {}).await;
        let h = d.healthy().await.health.unwrap();
        match check {
            0 => {
                let db = h.sections.database.unwrap();
                assert_eq!(db.state, SectionState::Unavailable);
                assert_eq!(db.code, "db_unreadable");
                assert_eq!(db.data.readable, Tri::No);
            }
            1 => {
                let m = h.sections.models.unwrap();
                assert_eq!(
                    (m.state, m.code.as_str()),
                    (SectionState::Degraded, "model_missing")
                );
                assert_eq!(m.data.answer.installed, Tri::No);
                assert_eq!(m.data.embed.resident, Tri::Unknown);
            }
            2 => {
                let i = h.sections.indexing.unwrap();
                let v = i.data.versions.unwrap();
                assert_eq!((v.ready, v.pending, v.reconciles), (Some(1), Some(2), true));
            }
            _ => {
                let v = h.sections.indexing.unwrap().data.versions.unwrap();
                assert_eq!((v.ready, v.empty_ready), (Some(2), Some(1)));
            }
        }
        let (_, body) = d.get("/v1/sources").await;
        let s: SourcesResponse = serde_json::from_str(&body).unwrap();
        assert_eq!(s.sources[0].readiness, Some(ready), "{scenario}");
        assert_eq!(s.sources[0].files, 3, "old `files` is untouched");
        let c = s.sources[0].counts.as_ref().unwrap();
        // Tiered/path-only history: unknown, never zero.
        let job = c.job.as_ref().unwrap();
        assert!(!job.known && job.skipped_by_code.is_empty() && job.files.is_none());
        assert_eq!(c.history.as_ref().unwrap().superseded, Some(2));
        d.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn status_never_blocks_during_a_slow_question_and_refresh_pauses_then_resumes() {
    let mut d = start_health(&[], |_| {}).await;
    let _ = d.healthy().await;
    let before = d.health_calls();
    assert!(before >= 1);
    let sock = d.sock.clone();
    let asker = tokio::task::spawn_blocking(move || {
        let body = serde_json::json!({"question": "slow"}).to_string();
        let r = client::post_json(&sock, "/v1/ask", &body, None, T).unwrap();
        r.status
    });
    d.wait_status("busy", |s| s.busy).await;
    let calls_at_start = d.health_calls();
    let t0 = Instant::now();
    let mut worst = Duration::ZERO;
    while t0.elapsed() < Duration::from_millis(900) {
        let t = Instant::now();
        let s = d.status().await;
        worst = worst.max(t.elapsed());
        let h = s.health.unwrap();
        assert!(s.busy);
        assert_eq!(
            h.worker_process.responsiveness,
            docket_proto::status::Responsiveness::Busy,
            "busy, not stale-unresponsive"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(
        worst < Duration::from_millis(250),
        "status blocked: {worst:?}"
    );
    assert_eq!(
        d.health_calls(),
        calls_at_start,
        "no health call while a question runs"
    );
    assert_eq!(asker.await.unwrap(), 200);
    let s = d
        .wait_status("refresh resumed", |s| {
            !s.busy
                && s.health
                    .as_ref()
                    .is_some_and(|h| h.refresh_skipped_busy > 0)
        })
        .await;
    assert!(s.health.unwrap().refresh_skipped_busy > 0);
    let t0 = Instant::now();
    while d.health_calls() <= calls_at_start {
        assert!(t0.elapsed() < T, "refresh never resumed");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_worker_that_stops_answering_health_leaves_stale_sections_with_age() {
    let mut d = start_health(&["--health-never-after", "2"], |c| {
        c.health_timeout = Duration::from_millis(400);
        c.backoff_base = Duration::from_millis(100);
        c.backoff_max = Duration::from_millis(200);
    })
    .await;
    let _ = d.healthy().await;
    let s = d
        .wait_status("stale after health went silent", |s| {
            s.health.as_ref().is_some_and(|h| {
                h.refresh_error_code.is_some()
                    && h.sections
                        .models
                        .as_ref()
                        .is_some_and(|m| m.stale == Some(true))
            })
        })
        .await;
    let h = s.health.unwrap();
    let m = h.sections.models.unwrap();
    assert!(m.age_ms.unwrap() > 0, "age is reported, section not erased");
    // R2a-3: the health timeout has its own code and does not by itself
    // replace the worker. The (existing) idle ping hang detection still does,
    // once the ping times out.
    assert_eq!(h.refresh_error_code.as_deref(), Some("health_timeout"));
    let s = d
        .wait_status("ping hang detection replaced the hung worker", |s| {
            s.health
                .as_ref()
                .is_some_and(|h| h.worker_process.crashes >= 1)
        })
        .await;
    assert_eq!(
        s.health.unwrap().worker_process.last_error_code.as_deref(),
        Some("worker_timeout")
    );
    d.stop().await;
}

fn start_count(d: &Daemon) -> usize {
    std::fs::read_to_string(d.dir.path().join("starts"))
        .map(|s| s.lines().count())
        .unwrap_or(0)
}

#[tokio::test(flavor = "multi_thread")]
async fn slow_health_marks_stale_but_never_replaces_the_worker() {
    // Health takes 900 ms, the limit is 300 ms: every refresh times out, yet
    // the worker still answers pings, so it must stay.
    let mut d = start_health(&["--health-delay", "0.9"], |c| {
        c.health_timeout = Duration::from_millis(300);
        c.call_timeout = Duration::from_secs(5);
    })
    .await;
    let s = d
        .wait_status("health_timeout reported", |s| {
            s.health
                .as_ref()
                .is_some_and(|h| h.refresh_error_code.as_deref() == Some("health_timeout"))
        })
        .await;
    assert_eq!(s.worker.state, WorkerState::Up);
    // Let several refresh cycles pass.
    tokio::time::sleep(Duration::from_millis(3500)).await;
    let s = d.status().await;
    let h = s.health.unwrap();
    assert_eq!(
        h.worker_process.crashes, 0,
        "a health timeout is not a crash"
    );
    assert_eq!(h.worker_process.restarts, 0);
    assert_eq!(start_count(&d), 1, "worker was never relaunched");
    assert_eq!(
        h.worker_process.responsiveness,
        docket_proto::status::Responsiveness::Responsive,
        "pings keep succeeding, so it is not unresponsive"
    );
    // The next question is served by the same worker.
    let (code, _) = d.ask("hello").await;
    assert_eq!(code, 200);
    assert_eq!(start_count(&d), 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn question_during_a_slow_health_call_is_bounded_and_the_worker_survives() {
    let mut d = start_health(&["--health-delay", "1.0"], |c| {
        c.health_timeout = Duration::from_millis(300);
        c.health_interval = Duration::from_millis(100);
    })
    .await;
    // Wait until a health call is in flight.
    let t0 = Instant::now();
    while d.health_calls() == 0 {
        assert!(t0.elapsed() < T);
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let t = Instant::now();
    let (code, _) = d.ask("hello").await;
    let waited = t.elapsed();
    assert_eq!(code, 200);
    // The daemon dispatches the question after at most the health timeout
    // (300 ms); this single-threaded fake worker then finishes its 1.0 s health
    // sleep before reading it, so the end-to-end time is bounded by that
    // sleep, not by anything the daemon adds.
    assert!(
        waited < Duration::from_millis(2500),
        "question took {waited:?}"
    );
    assert_eq!(start_count(&d), 1, "worker not replaced");
    // And the next one is served promptly.
    let t = Instant::now();
    let (code, _) = d.ask("hello again").await;
    assert_eq!(code, 200);
    assert!(
        t.elapsed() < Duration::from_millis(2500),
        "next question served promptly"
    );
    assert_eq!(start_count(&d), 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn never_answering_health_is_not_a_kill_reason_by_itself() {
    let mut d = start_health(&["--health-never-after", "1"], |c| {
        c.health_timeout = Duration::from_millis(300);
        c.call_timeout = Duration::from_secs(8);
    })
    .await;
    let _ = d.healthy().await;
    let s = d
        .wait_status("health_timeout and stale", |s| {
            s.health.as_ref().is_some_and(|h| {
                h.refresh_error_code.as_deref() == Some("health_timeout")
                    && h.sections
                        .models
                        .as_ref()
                        .is_some_and(|m| m.stale == Some(true))
            })
        })
        .await;
    // Right after the timeout the worker has not been touched (the ping
    // limit is 8 s, far away).
    assert_eq!(s.health.unwrap().worker_process.crashes, 0);
    assert_eq!(start_count(&d), 1);
    // The hung worker is eventually replaced by ping hang detection, not by
    // the health call, and then serves a question.
    d.wait_status("hang detection", |s| {
        s.health
            .as_ref()
            .is_some_and(|h| h.worker_process.crashes >= 1)
    })
    .await;
    d.wait_status("relaunched", |s| {
        s.worker.state == WorkerState::Up && s.worker.restarts >= 1
    })
    .await;
    let (code, _) = d.ask("hello").await;
    assert_eq!(code, 200);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_crash_marks_process_down_counts_restarts_and_stales_the_rest() {
    let mut d = start_health(&[], |c| {
        c.backoff_base = Duration::from_millis(1500);
        c.backoff_max = Duration::from_millis(1500);
    })
    .await;
    let _ = d.healthy().await;
    let (code, _) = d.ask("crash").await;
    assert_eq!(code, 502);
    let s = d
        .wait_status("down", |s| s.worker.state == WorkerState::Down)
        .await;
    let h = s.health.unwrap();
    assert_eq!(h.worker_process.state, WorkerState::Down);
    assert_eq!(
        h.worker_process.last_error_code.as_deref(),
        Some("worker_crashed")
    );
    assert_eq!(h.worker_process.crashes, 1);
    assert_eq!(h.worker_process.restarts, 0);
    assert_eq!(
        h.worker_process.responsiveness,
        docket_proto::status::Responsiveness::Unresponsive
    );
    let m = h.sections.models.unwrap();
    assert_eq!(m.stale, Some(true), "worker gone: previous data is stale");
    assert!(m.age_ms.is_some());
    // It comes back; restarts counts the relaunch, crashes stays 1.
    let s = d
        .wait_status("restarted", |s| {
            s.worker.state == WorkerState::Up && s.worker.restarts == 1
        })
        .await;
    assert_eq!(s.health.unwrap().worker_process.crashes, 1);
    let h = d
        .wait_status("fresh again", |s| {
            s.health
                .as_ref()
                .and_then(|h| h.sections.models.as_ref())
                .is_some_and(|m| m.stale == Some(false))
        })
        .await
        .health
        .unwrap();
    assert_eq!(h.worker_process.state, WorkerState::Up);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn user_cancel_is_a_relaunch_but_not_a_crash() {
    let mut d = start_health(&[], |_| {}).await;
    let _ = d.healthy().await;
    let id = d.new_task("stream_stall", true).await;
    d.wait_task(&id, "running", |t| t.state == TaskState::Running)
        .await;
    let _ = d.cancel_task(&id).await;
    let s = d
        .wait_status("relaunched", |s| {
            s.worker.restarts == 1 && s.worker.state == WorkerState::Up
        })
        .await;
    assert_eq!(s.health.unwrap().worker_process.crashes, 0);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn answer_metadata_passes_through_and_old_documents_stay_identical() {
    let mut d = start_health(&[], |_| {}).await;
    let (code, body) = d.ask("meta").await;
    assert_eq!(code, 200, "{body}");
    let r: AskResult = serde_json::from_str(&body).unwrap();
    assert!(r.abstained);
    assert_eq!(r.abstention_reason.as_deref(), Some("NO_EVIDENCE"));
    assert_eq!(r.scope_file_count, Some(4));
    assert_eq!(r.index_coverage.unwrap().keyword_only, 2);
    assert_eq!(r.warning_codes, vec!["no_citations".to_string()]);
    assert_no_leak("/v1/ask", &body);
    assert!(!body.contains("scope_label"));
    // A plain answer keeps exactly the old keys.
    let (_, body) = d.ask("rate?").await;
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    let mut keys: Vec<_> = v.as_object().unwrap().keys().cloned().collect();
    keys.sort();
    assert_eq!(keys, ["abstained", "answer", "citations", "timings"]);
    // The same fields travel in task events.
    let id = d.new_task("meta", false).await;
    d.wait_task(&id, "done", |t| t.state == TaskState::Succeeded)
        .await;
    let evs = d.events(&id).await;
    let text = serde_json::to_string(&evs.iter().map(|e| &e.body).collect::<Vec<_>>()).unwrap();
    assert!(
        text.contains("NO_EVIDENCE") && text.contains("warning_codes"),
        "{text}"
    );
    assert_no_leak("task events", &text);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn hostile_worker_text_never_reaches_status_sources_or_events() {
    let mut d = start_health(&["--scenario", "hostile"], |_| {}).await;
    let s = d.healthy().await;
    let (_, status) = d.get("/v1/status").await;
    assert_no_leak("/v1/status", &status);
    let (_, sources) = d.get("/v1/sources").await;
    assert_no_leak("/v1/sources", &sources);
    let src: SourcesResponse = serde_json::from_str(&sources).unwrap();
    let c = src.sources[0].counts.as_ref().unwrap();
    let job = c.job.as_ref().unwrap();
    assert_eq!(job.failed_by_code.get("unknown"), Some(&2));
    assert_eq!(job.failed_by_code.get("timeout"), Some(&1));
    assert_eq!(src.sources[0].anomalies, vec!["unknown", "job_partial"]);
    // Out-of-set code became "unknown"; unknown fields were dropped.
    let h = s.health.unwrap();
    assert_eq!(h.sections.models.unwrap().code, "unknown");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn tasks_off_still_serves_status_v2_alone() {
    let mut d = start_health(&[], |c| c.tasks_enabled = false).await;
    let s = d.healthy().await;
    assert_eq!(s.capabilities, vec!["status.v2".to_string()]);
    assert_eq!(d.get("/v1/tasks").await.0, 404);
    let (code, _) = d.ask("rate?").await;
    assert_eq!(code, 200);
    d.stop().await;
}

// ---------------------------------------------------------------------------
// R2b-1b-iii: POST /v1/evidence/get
// ---------------------------------------------------------------------------

const NOT_CITED: &str = r#"{"outcome":"not_cited"}"#;
const EV: &[&str] = &["--evidence", "--grants"];

fn ev_body(task: &str, chunk: &str) -> String {
    serde_json::json!({"task_id": task, "chunk_id": chunk}).to_string()
}

fn outcome(body: &str) -> String {
    serde_json::from_str::<serde_json::Value>(body).unwrap()["outcome"]
        .as_str()
        .unwrap_or("<none>")
        .to_string()
}

impl Daemon {
    async fn evidence_raw(&self, body: &str) -> (u16, String) {
        self.http(
            "POST",
            "/v1/evidence/get".into(),
            Some(body.to_string()),
            vec![("Content-Type", "application/json".into())],
        )
        .await
    }

    /// 200 + the typed body.
    async fn ev(&self, task: &str, chunk: &str) -> String {
        let (c, b) = self.evidence_raw(&ev_body(task, chunk)).await;
        assert_eq!(c, 200, "{b}");
        b
    }

    async fn wait_idle(&self) {
        let t0 = Instant::now();
        while self.status().await.busy {
            assert!(t0.elapsed() < T, "slot never freed");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    /// Run `question` to success and return its task id (slot free again).
    async fn done_task(&self, question: &str) -> String {
        let id = self.new_task(question, false).await;
        self.wait_task(&id, "succeeded", |t| t.state == TaskState::Succeeded)
            .await;
        self.wait_idle().await;
        id
    }

    /// Params of every `evidence.get` the fake worker received.
    fn evcalls(&self) -> Vec<serde_json::Value> {
        std::fs::read_to_string(self.dir.path().join("evcalls"))
            .map(|s| {
                s.lines()
                    .map(|l| serde_json::from_str(l).unwrap())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Poll until a read of `chunk` is `ok` (the worker may be restarting).
    async fn ev_until_ok(&self, task: &str, chunk: &str) -> String {
        let t0 = Instant::now();
        loop {
            let b = self.ev(task, chunk).await;
            if outcome(&b) == "ok" {
                return b;
            }
            assert!(t0.elapsed() < T, "never ok, last: {}", outcome(&b));
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    /// Start a read on a background thread; join to get (status, body).
    fn ev_bg(&self, task: &str, chunk: &str) -> tokio::task::JoinHandle<(u16, String)> {
        let s = self.sock.clone();
        let body = ev_body(task, chunk);
        tokio::task::spawn_blocking(move || {
            let r = client::post_json(&s, "/v1/evidence/get", &body, None, T).unwrap();
            (r.status, r.text().unwrap())
        })
    }
}

fn passage_text(body: &str) -> String {
    serde_json::from_str::<serde_json::Value>(body).unwrap()["passage"]["text"]
        .as_str()
        .unwrap()
        .to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn evidence_ok_sends_exactly_the_granted_version_and_resolved_chunk() {
    let mut d = Daemon::start(EV).await;
    assert!(d
        .status()
        .await
        .capabilities
        .contains(&"evidence.v1".to_string()));
    let t = d.done_task("cite c1 c3 old=new").await;
    let b = d.ev(&t, "c1").await;
    assert_eq!(outcome(&b), "ok");
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["untrusted_content"], true);
    let text = passage_text(&b);
    assert!(
        text.contains("chunk=c1 versions=v1 before=1 after=1"),
        "{text}"
    );
    assert_eq!(v["context_before"].as_array().unwrap().len(), 1);
    // Exactly these params: no client ids, no before/after the client omitted.
    assert_eq!(
        d.evcalls().last().unwrap(),
        &serde_json::json!({"chunk_id":"c1","allowed_version_ids":["v1"],
                            "answer_excerpt":"excerpt of c1"})
    );
    // The client's window is forwarded; 0 is not dropped.
    let (c, b) = d
        .evidence_raw(
            r#"{"task_id":"X","chunk_id":"c3","before":2,"after":0}"#
                .replace('X', &t)
                .as_str(),
        )
        .await;
    assert_eq!(c, 200);
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["context_before"].as_array().unwrap().len(), 2);
    assert_eq!(v["context_after"].as_array().unwrap().len(), 0);
    assert_eq!(
        d.evcalls().last().unwrap(),
        &serde_json::json!({"chunk_id":"c3","allowed_version_ids":["v1"],
                            "before":2,"after":0,"answer_excerpt":"excerpt of c3"})
    );
    // An alias remap sends the resolved id (never the citation's old id).
    let b = d.ev(&t, "old").await;
    assert_eq!(outcome(&b), "ok");
    assert_eq!(d.evcalls().last().unwrap()["chunk_id"], "new");
    assert_eq!(
        d.evcalls().last().unwrap()["allowed_version_ids"],
        serde_json::json!(["v1"])
    );
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn every_not_cited_variant_is_byte_identical_and_never_reaches_the_worker() {
    let mut d = Daemon::start(EV).await;
    // Several citations of the same version: c2 is NOT thereby authorised.
    let a = d.done_task("cite c1 c3").await;
    let b = d.done_task("cite:v2:s2 d1").await;
    let abstained = d.done_task("abstain c1").await;
    assert_eq!(outcome(&d.ev(&a, "c1").await), "ok");
    assert_eq!(outcome(&d.ev(&b, "d1").await), "ok");
    let calls = d.evcalls().len();
    let mut bodies = vec![
        d.ev(&a, "c2").await,             // uncited chunk, same version as c1/c3
        d.ev(&a, "d1").await,             // another task's grant
        d.ev(&b, "c1").await,             // ... and the other way round
        d.ev("t-nope", "c1").await,       // unknown task
        d.ev(&abstained, "c1").await,     // abstained
        d.ev(&a, &"z".repeat(200)).await, // unknown chunk
    ];
    // Not succeeded: running, then cancelled.
    let run = d.new_task("citeslow c1", false).await;
    bodies.push(d.ev(&run, "c1").await);
    d.wait_task(&run, "succeeded", |t| t.state == TaskState::Succeeded)
        .await;
    let st = d.new_task("stream_stall", false).await;
    d.wait_task(&st, "running", |t| t.state == TaskState::Running)
        .await;
    // While another task holds the slot: authorisation still comes first.
    bodies.push(d.ev(&st, "c1").await);
    bodies.push(d.ev(&a, "c2").await);
    d.cancel_task(&st).await;
    d.wait_task(&st, "cancelled", |t| t.state == TaskState::Cancelled)
        .await;
    bodies.push(d.ev(&st, "c1").await);
    for (n, b) in bodies.iter().enumerate() {
        assert_eq!(b, NOT_CITED, "variant {n}");
    }
    assert_eq!(
        d.evcalls().len(),
        calls,
        "worker was called for a not_cited"
    );
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn route_level_race_running_task_is_not_cited_then_ok_after_completion() {
    let mut d = Daemon::start(EV).await;
    let id = d.new_task("citeslow c1", false).await;
    assert_eq!(d.ev(&id, "c1").await, NOT_CITED);
    d.wait_task(&id, "succeeded", |t| t.state == TaskState::Succeeded)
        .await;
    d.wait_idle().await;
    assert_eq!(outcome(&d.ev(&id, "c1").await), "ok");
    assert_eq!(d.evcalls().len(), 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn busy_right_after_completion_is_transient_and_leaves_the_task_intact() {
    let mut d = Daemon::start(EV).await;
    let id = d.new_task("cite c1", false).await;
    d.wait_task(&id, "succeeded", |t| t.state == TaskState::Succeeded)
        .await;
    // Deliberately no wait_idle: the ask slot may still be held for a moment.
    let t0 = Instant::now();
    let mut seen = Vec::new();
    loop {
        let o = outcome(&d.ev(&id, "c1").await);
        let done = o != "busy";
        seen.push(o);
        if done {
            break;
        }
        assert!(t0.elapsed() < T, "busy never cleared");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(seen.pop().as_deref(), Some("ok"));
    assert!(seen.iter().all(|o| o == "busy"), "{seen:?}");
    // The task is untouched and its grant still works.
    assert_eq!(d.task(&id).await.state, TaskState::Succeeded);
    assert_eq!(outcome(&d.ev(&id, "c1").await), "ok");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn unsupported_for_metadata_less_answers_and_for_workers_without_the_feature() {
    // Worker has the feature but its answers carry no metadata.
    let mut d = Daemon::start(&["--evidence"]).await;
    let t = d.done_task("cite c1").await;
    assert_eq!(d.ev(&t, "c1").await, r#"{"outcome":"unsupported"}"#);
    assert_eq!(d.ev(&t, "anything").await, r#"{"outcome":"unsupported"}"#);
    assert!(d.evcalls().is_empty());
    d.stop().await;

    // Old worker: metadata but no `evidence.v1`: no capability, no call.
    let mut d = Daemon::start(&["--grants"]).await;
    assert!(!d
        .status()
        .await
        .capabilities
        .contains(&"evidence.v1".to_string()));
    let t = d.done_task("cite c1").await;
    assert_eq!(d.ev(&t, "c1").await, r#"{"outcome":"unsupported"}"#);
    assert!(d.evcalls().is_empty());
    // The ask path is unchanged for it.
    let (c, _) = d.ask("rate?").await;
    assert_eq!(c, 200);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn tasks_off_has_no_route_no_capability_and_no_evidence_access() {
    let mut d = Daemon::start_with(EV, |c| c.tasks_enabled = false).await;
    let (_, status) = d.get("/v1/status").await;
    assert!(
        !status.contains("evidence.v1") && !status.contains("capabilities"),
        "{status}"
    );
    let (c, _) = d.evidence_raw(&ev_body("t-1", "c1")).await;
    assert_eq!(c, 404);
    assert!(d.evcalls().is_empty());
    // Legacy ask has no task id, so it cannot use the route; it still works.
    let (c, b) = d.ask("cite c1").await;
    assert_eq!(c, 200, "{b}");
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn busy_while_a_question_runs_without_calling_the_worker() {
    let mut d = Daemon::start(EV).await;
    let t = d.done_task("cite c1").await;
    let running = d.new_task("slow", false).await;
    d.wait_task(&running, "running", |x| x.state == TaskState::Running)
        .await;
    let t0 = Instant::now();
    assert_eq!(d.ev(&t, "c1").await, r#"{"outcome":"busy"}"#);
    assert!(
        t0.elapsed() < Duration::from_millis(1000),
        "busy must be immediate"
    );
    assert!(d.evcalls().is_empty(), "worker called while busy");
    d.wait_task(&running, "succeeded", |x| x.state == TaskState::Succeeded)
        .await;
    d.wait_idle().await;
    assert_eq!(outcome(&d.ev(&t, "c1").await), "ok");
    assert_eq!(d.evcalls().len(), 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn read_in_flight_before_an_ask_does_not_block_the_ask_and_second_read_is_busy() {
    let mut d = Daemon::start(&["--evidence", "--grants", "--evidence-delay", "0.8"]).await;
    let t = d.done_task("cite slow1 c1").await;
    let read = d.ev_bg(&t, "slow1");
    tokio::time::sleep(Duration::from_millis(250)).await;
    // The read is mid-flight; the slot is free, so the Ask is accepted and
    // queues in the worker behind the read.
    let ask = d.new_task("rate?", false).await;
    // Slot now taken: a second read is `busy` straight away.
    assert_eq!(d.ev(&t, "c1").await, r#"{"outcome":"busy"}"#);
    let (c, b) = read.await.unwrap();
    assert_eq!((c, outcome(&b).as_str()), (200, "ok"), "{b}");
    d.wait_task(&ask, "succeeded", |x| x.state == TaskState::Succeeded)
        .await;
    assert_eq!(
        d.evcalls().len(),
        1,
        "only the first read reached the worker"
    );
    assert_eq!(d.starts(), 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn soft_timeout_keeps_the_worker_and_a_late_reply_never_confuses_the_next_read() {
    let mut d = Daemon::start_with(
        &["--evidence", "--grants", "--evidence-delay", "0.6"],
        |c| c.evidence_timeout = Duration::from_millis(400),
    )
    .await;
    let t = d.done_task("cite slow1 c1 c2").await;
    let pid = d.pid();
    assert_eq!(d.ev(&t, "slow1").await, r#"{"outcome":"timeout"}"#);
    // The worker is still busy with the slow read; its late reply (old id)
    // lands while this next read is in flight and must be ignored by id.
    let b = d.ev(&t, "c1").await;
    assert_eq!(outcome(&b), "ok", "{b}");
    let text = passage_text(&b);
    assert!(text.contains("chunk=c1 "), "{text}");
    assert!(!text.contains("slow1"));
    assert_eq!(d.pid(), pid, "soft timeout must not replace the worker");
    assert!(alive(pid));
    assert_eq!(d.starts(), 1);
    let st = d.status().await;
    assert_eq!((st.worker.restarts, st.worker.state), (0, WorkerState::Up));
    // And a question afterwards still works on the same worker.
    let (c, _) = d.ask("rate?").await;
    assert_eq!(c, 200);
    assert_eq!(d.starts(), 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_crash_during_reads_is_worker_unavailable_and_recovers() {
    let mut d = Daemon::start(&["--evidence", "--grants", "--evidence-delay", "0.5"]).await;
    let t = d.done_task("cite slowcrash1 c1 crash1").await;
    let pid = d.pid();
    // A read queued behind a crashing one also ends as worker_unavailable
    // (the worker is in restart backoff when it gets its turn).
    let first = d.ev_bg(&t, "slowcrash1");
    tokio::time::sleep(Duration::from_millis(150)).await;
    let second = d.ev(&t, "c1").await;
    let (_, first) = first.await.unwrap();
    assert_eq!(first, r#"{"outcome":"worker_unavailable"}"#);
    assert_eq!(second, r#"{"outcome":"worker_unavailable"}"#);
    // After the restart the next read succeeds.
    d.ev_until_ok(&t, "c1").await;
    assert_ne!(d.pid(), pid);
    assert_eq!(d.starts(), 2);
    assert_eq!(
        d.ev(&t, "crash1").await,
        r#"{"outcome":"worker_unavailable"}"#
    );
    d.ev_until_ok(&t, "c1").await;
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn ask_cancelled_around_reads_never_strands_them() {
    let mut d = Daemon::start(EV).await;
    let t = d.done_task("cite c1").await;
    let st = d.new_task("stream_stall", false).await;
    d.wait_task(&st, "running", |x| x.state == TaskState::Running)
        .await;
    assert_eq!(d.ev(&t, "c1").await, r#"{"outcome":"busy"}"#);
    let pid = d.pid();
    d.cancel_task(&st).await;
    d.wait_task(&st, "cancelled", |x| x.state == TaskState::Cancelled)
        .await;
    // While the worker is replaced a read is busy / worker_unavailable, never
    // a hang or a stale answer; once it is back, reads succeed.
    let t0 = Instant::now();
    loop {
        let b = d.ev(&t, "c1").await;
        match outcome(&b).as_str() {
            "ok" => break,
            "busy" | "worker_unavailable" => {}
            o => panic!("unexpected {o}"),
        }
        assert!(t0.elapsed() < T);
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_ne!(d.pid(), pid);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worker_data_from_another_version_source_or_chunk_is_never_returned() {
    let mut d = Daemon::start(EV).await;
    let ids = [
        "wrongver1",
        "wrongsrc1",
        "wrongchunk1",
        "wrongctxver1",
        "wrongctxsrc1",
        "replaced1",
        "nf1",
        "su1",
        "sp1",
        "wnotcited1",
        "wbusy1",
        "wfuture1",
        "badshape1",
        "rpcerr1",
    ];
    let t = d.done_task(&format!("cite {}", ids.join(" "))).await;
    for id in &ids[..5] {
        let b = d.ev(&t, id).await;
        assert_eq!(b, r#"{"outcome":"not_found"}"#, "{id}");
        assert!(!b.contains("SECRET"));
    }
    let b = d.ev(&t, "replaced1").await;
    assert_eq!(outcome(&b), "ok");
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&b).unwrap()["passage"]["chunk_id"],
        "new-replaced1"
    );
    for (id, want) in [
        ("nf1", "not_found"),
        ("su1", "source_unavailable"),
        ("sp1", "superseded"),
        // Daemon-only / unknown outcomes from the worker are never passed on.
        ("wnotcited1", "unsupported"),
        ("wbusy1", "unsupported"),
        ("wfuture1", "unsupported"),
        ("badshape1", "unsupported"),
        ("rpcerr1", "worker_unavailable"),
    ] {
        assert_eq!(
            d.ev(&t, id).await,
            format!(r#"{{"outcome":"{want}"}}"#),
            "{id}"
        );
    }
    // A worker RPC error is not a crash.
    assert_eq!(d.starts(), 1);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn malformed_requests_are_400_with_one_generic_message_and_echo_nothing() {
    let mut d = Daemon::start(EV).await;
    let t = d.done_task("cite c1").await;
    let calls = d.evcalls().len();
    let long = "LEAKME".repeat(40);
    let bodies: Vec<String> = vec![
        "".into(),
        "{".into(),
        "[]".into(),
        "null".into(),
        r#"{"task_id":"LEAKME"}"#.into(),
        r#"{"chunk_id":"LEAKME"}"#.into(),
        r#"{"task_id":"","chunk_id":"LEAKME"}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":""}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"a\u0000b"}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","before":4}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","after":-1}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","after":1.5}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","before":"1"}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","before":999999999999}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","evidence_version_id":"v1"}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","allowed_version_ids":["v1"]}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","scope":"all"}"#.into(),
        r#"{"task_id":"LEAKME","chunk_id":"c","source_id":"s1"}"#.into(),
        format!(r#"{{"task_id":"{long}","chunk_id":"c"}}"#),
        format!(r#"{{"task_id":"{t}","chunk_id":"{}"}}"#, "x".repeat(201)),
        format!(r#"{{"task_id":"{t}","chunk_id":"{}"}}"#, "é".repeat(201)),
        format!(
            r#"{{"task_id":"LEAKME","chunk_id":"c","pad":"{}"}}"#,
            "x".repeat(70_000)
        ),
        format!(
            r#"{{"task_id":"LEAKME","chunk_id":"c","pad":"{}"}}"#,
            "x".repeat(9_000)
        ),
    ];
    for b in &bodies {
        let (c, resp) = d.evidence_raw(b).await;
        assert_eq!(c, 400, "{}", &b[..b.len().min(80)]);
        assert_eq!(err_code(&resp), "bad_request");
        let e: ApiError = serde_json::from_str(&resp).unwrap();
        assert_eq!(e.error.message, "invalid evidence request");
        assert!(!resp.contains("LEAKME") && !resp.contains(&t), "{resp}");
    }
    // Boundary values are valid (and an unknown task is not_cited, not 400).
    let (c, b) = d
        .evidence_raw(&format!(
            r#"{{"task_id":"nope","chunk_id":"{}","before":3,"after":3}}"#,
            "é".repeat(200)
        ))
        .await;
    assert_eq!((c, b.as_str()), (200, NOT_CITED));
    // Wrong method and path shape: the chunk id is never in a URL.
    assert_eq!(
        d.http("GET", "/v1/evidence/get".into(), None, vec![])
            .await
            .0,
        405
    );
    assert_eq!(d.evcalls().len(), calls);
    d.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn worst_case_128_kib_multibyte_result_round_trips_intact() {
    let mut d = Daemon::start(EV).await;
    let t = d.done_task("cite big1 c1").await;
    let (c, b) = d.evidence_raw(&ev_body(&t, "big1")).await;
    assert_eq!(c, 200);
    assert!(b.len() > 120_000, "response was {} bytes", b.len());
    let text = passage_text(&b);
    let unit = "\u{20ac}\u{8a9e}\u{1F600}";
    assert!(text.len() > 110_000);
    assert_eq!(text, unit.repeat(text.chars().count() / 3));
    assert_eq!(outcome(&b), "ok");
    // The very next read on the same worker is unaffected.
    assert_eq!(outcome(&d.ev(&t, "c1").await), "ok");
    assert_eq!(d.starts(), 1);
    d.stop().await;
}

// --- logging: the real binary, stderr captured ---------------------------

struct Proc {
    child: std::process::Child,
    sock: PathBuf,
    log: PathBuf,
    _dir: TempDir,
}

impl Proc {
    fn start(extra_env: &[(&str, &str)], worker_extra: &[&str]) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let sock = dir.path().join("d.sock");
        let log = dir.path().join("stderr.log");
        let cmd = fake_cmd(dir.path(), worker_extra).join(" ");
        let mut c = std::process::Command::new(env!("CARGO_BIN_EXE_docketd"));
        c.env_clear()
            .env("PATH", std::env::var("PATH").unwrap_or_default())
            .env("DOCKET_SOCKET", &sock)
            .env("DOCKET_WORKER_CMD", cmd)
            .env("DOCKET_LOG", "debug")
            .stderr(std::fs::File::create(&log).unwrap())
            .stdout(std::process::Stdio::null());
        for (k, v) in extra_env {
            c.env(k, v);
        }
        Self {
            child: c.spawn().unwrap(),
            sock,
            log,
            _dir: dir,
        }
    }

    fn req(&self, method: &str, path: &str, body: Option<&str>) -> (u16, String) {
        let r = client::request(
            &self.sock,
            method,
            path,
            &[],
            body.map(str::as_bytes),
            Some(T),
        )
        .unwrap();
        (r.status, r.text().unwrap())
    }

    fn wait_up(&self) {
        let t0 = Instant::now();
        loop {
            if let Ok(r) = client::get(&self.sock, "/v1/status", T) {
                if r.text().is_ok_and(|b| b.contains(r#""state":"up""#)) {
                    return;
                }
            }
            assert!(t0.elapsed() < T, "daemon never came up");
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    fn stop(mut self) -> String {
        // SAFETY: plain kill(2) of our own child.
        unsafe { libc::kill(self.child.id() as i32, libc::SIGTERM) };
        let _ = self.child.wait();
        std::fs::read_to_string(&self.log).unwrap()
    }
}

#[test]
fn evidence_reads_never_log_content_ids_or_params() {
    const CHUNK: &str = "SECRETCHUNK-4242";
    let p = Proc::start(
        &[("DOCKET_EVIDENCE_TIMEOUT_MS", "300")],
        &["--evidence", "--grants", "--evidence-delay", "0.6"],
    );
    p.wait_up();
    let ids = [
        CHUNK.to_string(),
        format!("slow{CHUNK}"),
        format!("nf{CHUNK}"),
        format!("rpcerr{CHUNK}"),
        format!("wrongver{CHUNK}"),
        format!("crash{CHUNK}"),
    ];
    let q = format!("cite {}", ids.join(" "));
    let (c, b) = p.req(
        "POST",
        "/v1/tasks",
        Some(&serde_json::json!({"kind":"ask","question":q}).to_string()),
    );
    assert_eq!(c, 202, "{b}");
    let task = serde_json::from_str::<CreateTaskResponse>(&b)
        .unwrap()
        .task_id
        .0;
    let t0 = Instant::now();
    loop {
        let (_, l) = p.req("GET", "/v1/tasks", None);
        if l.contains("succeeded")
            && !p
                .req("GET", "/v1/status", None)
                .1
                .contains(r#""busy":true"#)
        {
            break;
        }
        assert!(t0.elapsed() < T);
        std::thread::sleep(Duration::from_millis(25));
    }
    let mut seen = vec![];
    for id in &ids {
        let body = serde_json::json!({"task_id": task, "chunk_id": id}).to_string();
        let (c, b) = p.req("POST", "/v1/evidence/get", Some(&body));
        assert_eq!(c, 200, "{b}");
        seen.push(outcome(&b));
        if id.starts_with("slow") {
            std::thread::sleep(Duration::from_millis(500));
        }
        if id.starts_with("crash") {
            std::thread::sleep(Duration::from_millis(600));
        }
    }
    // ok, timeout, not_found, worker_unavailable (rpc), not_found, worker_unavailable (crash)
    assert_eq!(
        seen,
        [
            "ok",
            "timeout",
            "not_found",
            "worker_unavailable",
            "not_found",
            "worker_unavailable"
        ]
    );
    // A malformed one too.
    let (c, _) = p.req(
        "POST",
        "/v1/evidence/get",
        Some(&format!(
            r#"{{"task_id":"{task}","chunk_id":"{CHUNK}","x":1}}"#
        )),
    );
    assert_eq!(c, 400);
    let log = p.stop();
    assert!(log.contains("request"), "logging was not captured: {log}");
    assert!(log.contains("/v1/evidence/get"), "{log}");
    for secret in ["SECRET", "excerpt of", "TEXT chunk"] {
        assert!(!log.contains(secret), "log leaked {secret:?}:\n{log}");
    }
    // The task lifecycle lines (R1, `docketd::tasks`) name the task by design;
    // nothing else, in particular nothing on the evidence path, may.
    let rest: Vec<&str> = log
        .lines()
        .filter(|l| !l.contains("docketd::tasks"))
        .collect();
    assert!(
        !rest.iter().any(|l| l.contains(&task)),
        "task id leaked:\n{log}"
    );
}

#[test]
fn evidence_timeout_env_is_bounded() {
    for bad in ["0", "50", "999999"] {
        let mut p = Proc::start(&[("DOCKET_EVIDENCE_TIMEOUT_MS", bad)], &["--evidence"]);
        let st = p.child.wait().unwrap();
        assert_eq!(st.code(), Some(2), "{bad}");
        let log = std::fs::read_to_string(&p.log).unwrap();
        assert!(log.contains("DOCKET_EVIDENCE_TIMEOUT_MS"), "{log}");
    }
}
