"""R2b-1c Step 3: latency, ask/evidence interleaving, soft timeout and SQLite contention,
measured end to end (real docketd + real worker protocol layer + real SQLite file).
Numbers are written to $E2E_REPORT_DIR (default: the test's temp dir) and printed (-s)."""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
import time
from pathlib import Path

import pytest

import harness
from docket.core.db.engine import BUSY_TIMEOUT_MS

pytestmark = pytest.mark.integration


def lib_with_chunks(lib, n=8):
    src, ver, ids = lib.doc("/lib/a/alpha.txt", [f"kiwi{i} body text number {i}" for i in range(n)],
                            headings=[f"Heading {i}" for i in range(n)])
    return src, ver, ids


def pct(samples, p):
    s = sorted(samples)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def summary(samples):
    return {"n": len(samples), "p50": round(pct(samples, 50), 2), "p95": round(pct(samples, 95), 2),
            "p99": round(pct(samples, 99), 2), "max": round(max(samples), 2),
            "mean": round(statistics.mean(samples), 2)}


def report(base: Path, name: str, data: dict):
    out = Path(os.environ.get("E2E_REPORT_DIR", base))
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(data, indent=1))
    print(f"\n[e2e-report] {name}: {json.dumps(data)}")


def ms(fn):
    t = time.perf_counter()
    r = fn()
    return (time.perf_counter() - t) * 1000, r


def test_latency_sequential_and_concurrent(base, lib, daemon):
    _, _, ids = lib_with_chunks(lib)
    t = daemon.ask("kiwi4")
    seq = []
    for _ in range(300):
        d, r = ms(lambda: daemon.evidence(t.id, ids[4], before=3, after=3))
        assert r["outcome"] == "ok"
        seq.append(d)
    conc, errors = [], []
    lock = threading.Lock()

    def client():
        for _ in range(40):
            try:
                d, r = ms(lambda: daemon.evidence(t.id, ids[4]))
                assert r["outcome"] == "ok", r
                with lock:
                    conc.append(d)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    threads = [threading.Thread(target=client) for _ in range(8)]
    wall = time.perf_counter()
    [x.start() for x in threads]
    [x.join() for x in threads]
    wall = time.perf_counter() - wall
    assert not errors, errors[:3]
    report(base, "latency", {"sequential_ms": summary(seq), "concurrent_8_clients_ms": summary(conc),
                             "concurrent_wall_s": round(wall, 2),
                             "concurrent_throughput_per_s": round(len(conc) / wall, 1)})
    assert summary(seq)["p99"] < 1000 and summary(conc)["max"] < 3000


def test_ask_arriving_during_an_evidence_read_waits_then_succeeds(base, lib, daemons):
    _, _, ids = lib_with_chunks(lib)
    slow_file = base / "slow.txt"
    slow_file.write_text("")
    d = daemons(slow_ids_file=slow_file, extra_env={"E2E_SLOW_S": "1.5"})
    t = d.ask("kiwi4")
    slow_file.write_text(ids[4] + "\n")
    out = {}

    def read():
        out["ms"], out["r"] = ms(lambda: d.evidence(t.id, ids[4]))

    th = threading.Thread(target=read)
    th.start()
    time.sleep(0.4)  # the read is in the worker now
    ask_ms, t2 = ms(lambda: d.ask("kiwi2"))
    th.join()
    assert out["r"]["outcome"] == "ok" and out["ms"] >= 1400
    assert t2.state == "succeeded" and t2.citations
    # the worker serves one request at a time: the question queued behind the read
    assert ask_ms >= 900, ask_ms
    report(base, "ask_during_read", {"read_ms": round(out["ms"]), "ask_ms": round(ask_ms),
                                     "note": "ask started 0.4s into a 1.5s read"})


def test_read_during_an_ask_is_busy_and_never_reaches_the_worker(base, lib, daemon):
    _, _, ids = lib_with_chunks(lib)
    t = daemon.ask("kiwi4")
    assert daemon.evidence(t.id, ids[4])["outcome"] == "ok"
    before = daemon.evidence_calls()
    tid = daemon.create_task("slow 2.5 kiwi1")
    daemon.wait_state(tid, {"running"})
    time.sleep(0.3)
    busy = []
    for _ in range(5):
        d_ms, r = ms(lambda: daemon.evidence(t.id, ids[4]))
        busy.append(d_ms)
        assert r == {"outcome": "busy"}
    assert max(busy) < 500 and daemon.evidence_calls() == before
    assert daemon.status()["busy"] is True
    daemon.events(tid)  # wait for the question to finish
    assert daemon.task_state(tid) == "succeeded"
    assert daemon.evidence(t.id, ids[4])["outcome"] == "ok"
    report(base, "read_during_ask", {"busy_reply_ms": [round(x, 1) for x in busy]})


def test_soft_timeout_does_not_restart_the_worker(base, lib, daemons):
    _, _, ids = lib_with_chunks(lib)
    slow_file = base / "slow.txt"
    slow_file.write_text("")
    d = daemons(evidence_timeout_ms=100, slow_ids_file=slow_file, extra_env={"E2E_SLOW_S": "1.0"})
    t = d.ask("kiwi4")
    pid, restarts = d.worker_pid(), d.status()["worker"]["restarts"]
    slow_file.write_text(ids[4] + "\n")
    el, r = ms(lambda: d.evidence(t.id, ids[4]))
    assert r == {"outcome": "timeout"} and 80 <= el < 800, (r, el)
    assert d.worker_pid() == pid and d.status()["worker"]["restarts"] == restarts
    # a read right behind the stuck one: the worker is still busy with it; whatever the
    # outcome, it must be typed and the worker must not be replaced
    slow_file.write_text("")
    _, nxt = ms(lambda: d.evidence(t.id, ids[4]))
    assert nxt["outcome"] in ("ok", "timeout")
    time.sleep(1.3)
    _, r2 = ms(lambda: d.evidence(t.id, ids[4]))
    assert r2["outcome"] == "ok"
    assert d.worker_pid() == pid and d.status()["worker"]["restarts"] == restarts
    assert d.ask("kiwi1").state == "succeeded"
    report(base, "soft_timeout", {"timeout_reply_ms": round(el), "next_read_during_stuck": nxt["outcome"],
                                  "worker_pid_unchanged": True, "restarts": restarts})


class Contention:
    """Another SQLite connection to the scratch library holding locks."""

    def __init__(self, db: Path):
        self.db = db

    def conn(self, timeout=0.0):
        return sqlite3.connect(self.db, timeout=timeout, isolation_level=None)


def measure(d, t, ids, n=15):
    ask_ms, a = ms(lambda: d.ask("kiwi2"))
    assert a.state == "succeeded", a.state
    reads = []
    for _ in range(n):
        el, r = ms(lambda: d.evidence(t.id, ids[4]))
        assert r["outcome"] == "ok", r
        reads.append(el)
    return {"ask_ms": round(ask_ms, 1), "reads_ms": summary(reads)}


def test_sqlite_writer_lock_and_checkpoint_effect_on_reads_and_asks(base, lib, daemon):
    _, _, ids = lib_with_chunks(lib)
    t = daemon.ask("kiwi4")
    c = Contention(lib.db)
    res = {"configured": {"sqlite_busy_timeout_ms": BUSY_TIMEOUT_MS, "evidence_soft_timeout_ms": 3000,
                          "daemon_call_timeout_ms": 15000, "ask_timeout_ms": 300000}}
    res["baseline"] = measure(daemon, t, ids)

    # 1) a write transaction held open by another connection
    w = c.conn()
    w.execute("BEGIN IMMEDIATE")
    w.execute("UPDATE chunks SET ordinal = ordinal WHERE id = ?", (ids[0],))
    try:
        res["writer_held"] = measure(daemon, t, ids)
    finally:
        w.execute("COMMIT")
        w.close()

    # 2) a checkpoint (TRUNCATE) waiting while a writer is active
    w = c.conn()
    w.execute("BEGIN IMMEDIATE")
    w.execute("UPDATE chunks SET ordinal = ordinal WHERE id = ?", (ids[0],))
    ck = {}

    def checkpoint():
        k = c.conn(timeout=6.0)
        k.execute("PRAGMA busy_timeout=6000")
        ck["ms"], ck["row"] = ms(lambda: k.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
        k.close()

    th = threading.Thread(target=checkpoint)
    th.start()
    time.sleep(0.3)
    try:
        res["checkpoint_waiting_on_writer"] = measure(daemon, t, ids)
    finally:
        w.execute("COMMIT")
        w.close()
        th.join()
    res["checkpoint_waiting_on_writer"]["checkpoint_call_ms"] = round(ck["ms"])
    res["checkpoint_waiting_on_writer"]["checkpoint_result_busy_log_ckpt"] = list(ck["row"][0])

    # 3) a checkpoint blocked by a long reader (pins the WAL) while another write lands
    r = c.conn()
    r.execute("BEGIN")
    r.execute("SELECT count(*) FROM chunks").fetchall()
    w = c.conn()
    w.execute("UPDATE chunks SET ordinal = ordinal WHERE id = ?", (ids[1],))
    ck.clear()
    th = threading.Thread(target=checkpoint)
    th.start()
    time.sleep(0.3)
    try:
        res["checkpoint_blocked_by_reader"] = measure(daemon, t, ids)
    finally:
        r.execute("COMMIT")
        r.close()
        w.close()
        th.join()
    res["checkpoint_blocked_by_reader"]["checkpoint_call_ms"] = round(ck["ms"])
    res["checkpoint_blocked_by_reader"]["checkpoint_result_busy_log_ckpt"] = list(ck["row"][0])
    report(base, "sqlite_contention", res)
    # observed worst case must stay inside the configured bound (it is far below it in WAL mode)
    for k in ("writer_held", "checkpoint_waiting_on_writer", "checkpoint_blocked_by_reader"):
        assert res[k]["ask_ms"] < BUSY_TIMEOUT_MS and res[k]["reads_ms"]["max"] < BUSY_TIMEOUT_MS, k
