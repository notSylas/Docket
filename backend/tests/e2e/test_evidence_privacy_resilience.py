"""R2b-1c Step 4: privacy of logs and request paths, the worker is not invoked for
malformed/unauthorized requests, and grant lifetime across daemon and worker restarts."""

from __future__ import annotations

import re
import threading
import time

import pytest

import harness

pytestmark = pytest.mark.e2e

NOT_CITED = {"outcome": "not_cited"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")

SECRET_TEXT = "CONFIDENTIAL-TEXT-7731"
SECRET_HEADING = "SECRET-HEADING-4412"
SECRET_FILE = "payroll-SECRET-FILE-9917.txt"
SECRET_DIR = "secret-dir-5528"
EXCEPTION_SECRET = "CONFIDENTIAL-IN-EXCEPTION"


def secret_library(lib):
    src, ver, ids = lib.doc(
        f"/lib/{SECRET_DIR}/{SECRET_FILE}",
        [f"privword0 {SECRET_TEXT} first", f"privword1 {SECRET_TEXT} second", f"privword2 {SECRET_TEXT} third"],
        headings=[f"{SECRET_HEADING} a", f"{SECRET_HEADING} b", f"{SECRET_HEADING} c"],
    )
    return src, ver, ids


def test_logs_and_paths_never_contain_content_or_ids(base, lib, daemons):
    src, ver, ids = secret_library(lib)
    slow_file = base / "slow.txt"
    slow_file.write_text(f"fail:{ids[2]}\n")
    d = daemons(slow_ids_file=slow_file)
    t = d.ask("privword1")
    assert t.state == "succeeded"
    assert d.evidence(t.id, ids[1])["outcome"] == "ok"
    assert d.evidence(t.id, ids[1], before=3, after=3)["outcome"] == "ok"
    assert d.evidence(t.id, ids[0]) == NOT_CITED
    assert d.evidence("t-00000000-9", ids[1]) == NOT_CITED
    assert d.request("POST", "/v1/evidence/get", raw=b"{not json " + SECRET_TEXT.encode())[0] == 400
    assert d.request("POST", "/v1/evidence/get", {"task_id": t.id, "chunk_id": ids[1], "scope": SECRET_TEXT})[0] == 400
    # a reader failure carrying content in the exception message (worker-side): ask for the failing chunk
    t2 = d.ask("privword2")
    assert d.evidence(t2.id, ids[2])["outcome"] == "worker_unavailable"  # JSON-RPC error: generic, typed
    assert d.evidence(t2.id, ids[2])["outcome"] == "worker_unavailable"
    d.stop()
    log = ANSI.sub("", d.stderr())
    assert "e2e-worker: ready" in log  # the worker's stderr is in what the daemon captured
    worker_lines = [ln for ln in log.splitlines() if "worker" in ln]
    assert worker_lines
    secrets = {"text": SECRET_TEXT, "heading": SECRET_HEADING, "file": SECRET_FILE, "dir": SECRET_DIR,
               "path": "/lib/", "exception": EXCEPTION_SECRET, "chunk_ids": None}
    for name, s in secrets.items():
        if s:
            assert s not in log, f"{name} leaked into daemon/worker logs"
    for cid in ids:
        assert cid not in log and cid[4:] not in log and cid[:16] not in log, "chunk id leaked"
    for ident in (src, ver):
        assert ident not in log, "source/version id leaked"
    # (the reader raised an exception whose message carried content; the worker turned it into a
    # generic "evidence read failed" JSON-RPC error and logged nothing about it)
    # request lines: method + path only, and the evidence path never carries ids
    reqs = [ln for ln in log.splitlines() if "docketd::http" in ln and "evidence" in ln]
    assert len(reqs) >= 7
    for ln in reqs:
        assert re.search(r"path=/v1/evidence/get(\s|$)", ln), ln
        assert "POST" in ln
    # task ids may appear on 'task finished' / task event request lines (pre-existing); never next to evidence
    for ln in log.splitlines():
        if "evidence" in ln:
            assert t.id not in ln and t2.id not in ln, ln
    # debug evidence lines carry the outcome code only
    ev = [ln for ln in log.splitlines() if "docketd::evidence" in ln]
    assert ev and all(re.search(r'outcome="?[a-z_]+"?\s*$', ln) or "mismatch" in ln for ln in ev), ev[:3]


def test_malformed_and_unauthorized_requests_never_invoke_the_worker(lib, daemon):
    _, _, ids = secret_library(lib)
    t = daemon.ask("privword1")
    assert daemon.evidence_calls() == 0
    big = "x" * 201
    bad_bodies = [b"", b"{", b"[]", b'"str"', b'{"task_id":"a"}', b'{"chunk_id":"a"}',
                  b'{"task_id":"","chunk_id":"c"}', b'{"task_id":"t","chunk_id":""}',
                  f'{{"task_id":"t","chunk_id":"{big}"}}'.encode(), f'{{"task_id":"{big}","chunk_id":"c"}}'.encode(),
                  b'{"task_id":"t\\u0000","chunk_id":"c"}', b'{"task_id":"t","chunk_id":"c","before":9}',
                  b'{"task_id":"t","chunk_id":"c","after":-1}', b'{"task_id":"t","chunk_id":"c","scope":"x"}',
                  b'{"task_id":1,"chunk_id":"c"}', b'{"task_id":"t","chunk_id":"c","extra":true}',
                  b'{"task_id":"t","chunk_id":"c"} trailing', b"\xff\xfe"]
    for raw in bad_bodies:
        code, js = daemon.request("POST", "/v1/evidence/get", raw=raw)
        assert code == 400, (raw, code)
        assert js["error"]["message"] == "invalid evidence request"
    assert daemon.request("POST", "/v1/evidence/get", raw=b"x" * 5000)[0] in (400, 413)
    assert daemon.request("GET", "/v1/evidence/get")[0] == 405
    # well-formed but unauthorized: unknown task, other chunk, unknown chunk
    for task, chunk in [("t-0-0", ids[1]), (t.id, ids[0]), (t.id, ids[2]), (t.id, "chk_nope"), ("t-0-0", "c")]:
        assert daemon.evidence(task, chunk) == NOT_CITED
    assert daemon.evidence_calls() == 0
    assert daemon.evidence(t.id, ids[1])["outcome"] == "ok"
    assert daemon.evidence_calls() == 1  # exactly the one authorized read reached the worker


def test_daemon_restart_invalidates_grants_and_ids_cannot_be_replayed(lib, daemon):
    _, _, ids = secret_library(lib)
    t = daemon.ask("privword1")
    assert daemon.evidence(t.id, ids[1])["outcome"] == "ok"
    daemon.restart()
    assert daemon.evidence(t.id, ids[1]) == NOT_CITED
    assert daemon.request("GET", "/v1/tasks")[1]["tasks"] == []
    t2 = daemon.ask("privword1")
    assert t2.id != t.id and t2.id.split("-")[1] != t.id.split("-")[1]  # a fresh boot tag
    assert daemon.evidence(t2.id, ids[1])["outcome"] == "ok"
    # replaying the old id (and an id forged with the new boot tag and the old counter) grants nothing
    forged = f"t-{t2.id.split('-')[1]}-{t.id.split('-')[2]}"
    assert forged == t2.id or daemon.evidence(forged, ids[1]) == NOT_CITED
    assert daemon.evidence(t.id, ids[1]) == NOT_CITED
    other = f"t-{t2.id.split('-')[1]}-99"
    assert daemon.evidence(other, ids[1]) == NOT_CITED


def test_worker_crash_and_restart_keep_grants_and_service_recovers(lib, daemon):
    _, _, ids = secret_library(lib)
    t = daemon.ask("privword1")
    assert daemon.evidence(t.id, ids[1])["outcome"] == "ok"
    pid = daemon.worker_pid()
    daemon.kill_worker()
    # immediately after the kill: typed outcome (never a crash, never unauthorised data)
    r = daemon.evidence(t.id, ids[1])
    assert r["outcome"] in ("worker_unavailable", "ok"), r
    assert daemon.evidence("t-0-0", ids[1]) == NOT_CITED  # authorization is decided before worker state
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        st = daemon.status()
        if st["worker"]["state"] == "up" and st["worker"]["restarts"] >= 1:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("worker did not restart")
    assert daemon.worker_pid() != pid
    r = daemon.evidence(t.id, ids[1])  # the grant lives in the daemon, not the worker
    assert r["outcome"] == "ok" and r["passage"]["chunk_id"] == ids[1]
    t2 = daemon.ask("privword2")
    assert t2.state == "succeeded" and daemon.evidence(t2.id, ids[2])["outcome"] == "ok"
    assert "evidence.v1" in daemon.capabilities()


def test_worker_killed_mid_read_recovers(base, lib, daemons):
    _, _, ids = secret_library(lib)
    slow_file = base / "slow.txt"
    slow_file.write_text("")
    d = daemons(slow_ids_file=slow_file, extra_env={"E2E_SLOW_S": "2.0"})
    t = d.ask("privword1")
    slow_file.write_text(ids[1] + "\n")
    out = {}
    th = threading.Thread(target=lambda: out.update(r=d.evidence(t.id, ids[1])))
    th.start()
    time.sleep(0.5)
    d.kill_worker()
    th.join(30)
    assert out["r"]["outcome"] == "worker_unavailable", out
    slow_file.write_text("")
    d.wait_up()
    assert d.evidence(t.id, ids[1])["outcome"] == "ok"
    assert d.ask("privword2").state == "succeeded"
