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
