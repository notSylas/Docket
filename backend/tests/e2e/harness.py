"""Helpers for the R2b-1c end-to-end evidence tests: a scratch library builder,
a real-``docketd`` process driver and a stdlib HTTP-over-unix-socket client.

Everything lives in a short temp dir (``/tmp/dk-e2e-*``); nothing here ever
touches a real library."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
DOCKETD = REPO / "app" / "target" / "debug" / "docketd"
WORKER = Path(__file__).resolve().parent / "e2e_worker.py"
sys.path.insert(0, str(BACKEND / "tests" / "unit"))

from test_evidence_get import Lib  # noqa: E402  (synthetic scratch library helper, reused)

from docket.core.config import Settings  # noqa: E402
from docket.core.db.engine import get_engine, get_session_factory  # noqa: E402
from docket.core.db.identity import compute_chunk_id, compute_recipe_id  # noqa: E402
from docket.core.db.models import AuthorizedSource, ChunkRecipe, Workspace  # noqa: E402
from docket.interfaces.cli.context import ensure_schema  # noqa: E402


class E2ELib(Lib):
    """``tests/unit`` ``Lib`` on a REAL alembic-migrated database at the
    data dir's own ``docket.sqlite3`` (what the harness worker opens)."""

    def __init__(self, data_dir: Path):
        settings = Settings(data_dir=data_dir)
        settings.ensure_data_dirs()
        ensure_schema(settings.sqlite_path)
        self.db = settings.sqlite_path
        self.engine = get_engine(self.db)
        self.factory = get_session_factory(self.engine)
        with self.factory() as s:
            ws = Workspace(name="w")
            s.add(ws)
            s.flush()
            auth = AuthorizedSource(workspace_id=ws.id, scope_path="/a")
            s.add(auth)
            s.flush()
            self.ws, self.auth = ws.id, auth.id
            kw = dict(chunk_size=1, overlap=0, splitter="w", parser_name="p", parser_version="1")
            self.recipe = compute_recipe_id(**kw)
            s.add(ChunkRecipe(id=self.recipe, **kw))
            s.commit()
        self.n = 0

    def doc(self, path: str, texts, *, source_id: str | None = None, headings=None, **chunk_kw):
        """One source+version (or version in ``source_id``) with one chunk per text."""
        src = source_id or self.source(os.path.dirname(path) or "/lib")
        ver = self.version(src, file_path=path)
        ids = []
        for i, t in enumerate(texts):
            kw = dict(chunk_kw)
            if headings:
                kw["heading"] = headings[i]
            ids.append(self.chunk(src, ver, i, t, **kw))
        return src, ver, ids

    def query(self, sql: str, *params):
        import sqlite3

        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class Task:
    def __init__(self, task_id, state, events):
        self.id, self.state, self.events = task_id, state, events

    @property
    def result(self):
        for e in self.events:
            if e.get("type") == "result":
                return e["result"]
        return None

    @property
    def citations(self):
        return (self.result or {}).get("citations", [])


class Daemon:
    """A real ``docketd`` supervising the harness worker on a scratch socket."""

    def __init__(self, base: Path, *, data_dir: Path | None = None, old=False, tasks=True,
                 evidence_timeout_ms: int | None = None, slow_ids_file: Path | None = None,
                 extra_env: dict | None = None, name="d"):
        self.base = base
        self.data_dir = data_dir or base / "data"
        self.sock = f"{base}/{name}.sock"
        assert len(self.sock) < 100, self.sock
        self.pidfile = base / f"{name}.worker.pid"
        self.counter = base / f"{name}.evidence.count"
        self.err_path = base / f"{name}.daemon.err"
        self.proc: subprocess.Popen | None = None
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DOCKET_", "E2E_"))}
        env.update(
            PYTHONPATH=str(BACKEND / "src"),
            DOCKET_DATA_DIR=str(self.data_dir),
            DOCKET_SOCKET=self.sock,
            DOCKET_WORKER_CMD=f"{sys.executable} {WORKER}",
            DOCKET_LOG="debug",
            DOCKET_EMBED_TOKENIZER="heuristic",
            DOCKET_HEALTH_INTERVAL_MS="0",
            DOCKET_CANCEL_GRACE_MS="300",
            DOCKET_BACKOFF_BASE_MS="100",
            DOCKET_BACKOFF_MAX_MS="500",
            E2E_PIDFILE=str(self.pidfile),
            E2E_COUNTER_FILE=str(self.counter),
            OLLAMA_HOST="http://127.0.0.1:9",  # nothing listens: never reach a real model server
        )
        if old:
            env["E2E_OLD"] = "1"
        if not tasks:
            env["DOCKET_TASKS"] = "off"
        if evidence_timeout_ms is not None:
            env["DOCKET_EVIDENCE_TIMEOUT_MS"] = str(evidence_timeout_ms)
        if slow_ids_file is not None:
            env["E2E_SLOW_IDS_FILE"] = str(slow_ids_file)
        env.update(extra_env or {})
        self.env = env
        self.counter.touch()

    # -- lifecycle ---------------------------------------------------------
    def start(self, wait_up=True):
        assert self.proc is None or self.proc.poll() is not None
        self._err = open(self.err_path, "ab")
        self.proc = subprocess.Popen([str(DOCKETD)], env=self.env, stdout=subprocess.DEVNULL,
                                     stderr=self._err, start_new_session=True)
        if wait_up:
            self.wait_up()
        return self

    def wait_up(self, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            assert self.proc.poll() is None, f"docketd exited: {self.stderr()[-2000:]}"
            try:
                st = self.status()
                if st["worker"]["state"] == "up":
                    return st
            except OSError:
                pass
            time.sleep(0.1)
        raise AssertionError("daemon/worker did not come up\n" + self.stderr()[-2000:])

    def stop(self):
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(5)
        self.proc = None
        self._kill_worker()
        for f in (self.sock,):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            self._err.close()
        except Exception:  # noqa: BLE001
            pass

    def restart(self):
        self.stop()
        return self.start()

    def _kill_worker(self):
        try:
            pid = int(self.pidfile.read_text())
            os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def worker_pid(self) -> int:
        return int(self.pidfile.read_text())

    def kill_worker(self):
        os.kill(self.worker_pid(), signal.SIGKILL)

    def stderr(self) -> str:
        return self.err_path.read_text(errors="replace") if self.err_path.exists() else ""

    def evidence_calls(self) -> int:
        return len(self.counter.read_text().splitlines())

    # -- HTTP --------------------------------------------------------------
    def request(self, method, path, body=None, raw: bytes | None = None, headers=None, timeout=30,
                want_bytes=False):
        c = UnixConn(self.sock, timeout)
        try:
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            h = {"content-type": "application/json", **(headers or {})}
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            payload = r.read()
            if want_bytes:
                return r.status, payload
            try:
                return r.status, json.loads(payload) if payload else None
            except ValueError:
                return r.status, payload
        finally:
            c.close()

    def status(self):
        return self.request("GET", "/v1/status")[1]

    def capabilities(self):
        return self.status().get("capabilities", [])

    def create_task(self, question, scope=None, deadline_ms=None):
        body = {"kind": "ask", "question": question}
        if scope:
            body["scope"] = scope
        if deadline_ms is not None:
            body["deadline_ms"] = deadline_ms
        code, js = self.request("POST", "/v1/tasks", body)
        assert code == 202, (code, js)
        return js["task_id"]

    def events(self, task_id, timeout=60):
        """Read the SSE stream to its end (the task's terminal state)."""
        c = UnixConn(self.sock, timeout)
        out = []
        try:
            c.request("GET", f"/v1/tasks/{task_id}/events")
            r = c.getresponse()
            assert r.status == 200, r.status
            data = None
            while True:
                line = r.readline()
                if not line:
                    break
                line = line.decode().rstrip("\n")
                if line.startswith("data:"):
                    data = line[5:].strip()
                elif line == "" and data is not None:
                    out.append(json.loads(data))
                    data = None
        finally:
            c.close()
        return out

    def ask(self, question, scope=None, deadline_ms=None) -> Task:
        tid = self.create_task(question, scope, deadline_ms)
        ev = self.events(tid)
        state = [e["state"] for e in ev if e.get("type") == "state"][-1]
        return Task(tid, state, ev)

    def task_state(self, task_id):
        for t in self.request("GET", "/v1/tasks")[1]["tasks"]:
            if t["task_id"] == task_id:
                return t["state"]
        return None

    def wait_state(self, task_id, wanted, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = self.task_state(task_id)
            if s in wanted:
                return s
            time.sleep(0.05)
        raise AssertionError(f"task state {self.task_state(task_id)} not in {wanted}")

    def evidence(self, task_id, chunk_id, **kw):
        body = {"task_id": task_id, "chunk_id": chunk_id, **kw}
        code, js = self.request("POST", "/v1/evidence/get", body)
        assert code == 200, (code, js)
        return js


def new_base() -> Path:
    return Path(tempfile.mkdtemp(prefix="dk-e2e-"))


def cleanup(base: Path):
    shutil.rmtree(base, ignore_errors=True)
