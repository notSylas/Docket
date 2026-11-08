"""Process-based parse worker pool for the heavy (Docling) parser.

Workers only *parse*; every database/index write stays on the calling thread
and commits in discovery order, so a parallel run produces the same rows as a
sequential one (and the writer lock is held exactly as before). The pipeline
`submit`s upcoming files in a small look-ahead window and `take`s each result
when it reaches that file; a file that was never submitted is parsed inline by
the caller as before.

Each worker is a separate OS process that builds its parser once (Docling's
converter loads models on construction) and then serves files over a pipe.
A per-file timeout kills the worker and starts a fresh one; a worker that
crashes (segfault, OOM kill, `os._exit`) fails only the file it was on.
Failures surface as `ParseError`, the same type the sequential path raises, so
per-file failure recording is unchanged.
"""

from __future__ import annotations

import atexit
import logging
import multiprocessing
import os
import queue
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

from docket.infra.parsing.docling_wrapper import ParseError

logger = logging.getLogger(__name__)


def default_parse_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 4) // 4))


def _worker_main(conn, factory) -> None:
    parser = None
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        source_id, path = msg
        try:
            if parser is None:
                parser = factory()
            conn.send(("ok", parser.parse(source_id, Path(path))))
        except BaseException as exc:  # noqa: BLE001 -- report, keep serving
            try:
                conn.send(("err", str(getattr(exc, "cause", None) or exc)))
            except Exception:  # noqa: BLE001
                return


class _Slot:
    """One worker process plus the pipe to it; restarted on timeout/crash."""

    def __init__(self, ctx, factory) -> None:
        self._ctx = ctx
        self._factory = factory
        self._proc = None
        self._conn = None

    def _start(self) -> None:
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(target=_worker_main, args=(child, self._factory), daemon=True)
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent

    def kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.join(5)
            except Exception:  # noqa: BLE001
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._proc = self._conn = None

    def run(self, source_id: str, path: Path, timeout_s: float | None) -> Any:
        if self._proc is None or not self._proc.is_alive():
            self.kill()
            self._start()
        try:
            self._conn.send((source_id, str(path)))
            if not self._conn.poll(timeout_s):
                self.kill()
                raise ParseError(
                    source_id, path, TimeoutError(f"parse timed out after {timeout_s:g}s")
                )
            kind, payload = self._conn.recv()
        except (EOFError, OSError, BrokenPipeError):
            code = self._proc.exitcode if self._proc is not None else None
            self.kill()
            raise ParseError(
                source_id, path, RuntimeError(f"parse worker died (exit code {code})")
            ) from None
        if kind == "ok":
            return payload
        raise ParseError(source_id, path, RuntimeError(payload))


class ParsePool:
    def __init__(
        self,
        parser_factory: Callable[[], Any],
        *,
        workers: int,
        timeout_s: float | None = 600.0,
        mp_context: str = "spawn",
    ) -> None:
        self.workers = max(1, workers)
        self._timeout_s = timeout_s if timeout_s and timeout_s > 0 else None
        self._ctx = multiprocessing.get_context(mp_context)
        self._factory = parser_factory
        self._queue: queue.Queue = queue.Queue()
        self._futures: dict[tuple[str, str], Future] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._slots: list[_Slot] = []
        self._closed = False
        atexit.register(self.close)

    @property
    def window(self) -> int:
        """How many files ahead of the writer to keep in flight."""
        return self.workers * 2

    def _ensure_threads(self) -> None:
        if self._threads:
            return
        for _ in range(self.workers):
            slot = _Slot(self._ctx, self._factory)
            self._slots.append(slot)
            t = threading.Thread(target=self._loop, args=(slot,), daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self, slot: _Slot) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                slot.kill()
                return
            fut, source_id, path = task
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(slot.run(source_id, path, self._timeout_s))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)

    def submit(self, source_id: str, path: Path) -> None:
        if self._closed:
            return
        key = (source_id, str(path))
        with self._lock:
            if key in self._futures:
                return
            self._ensure_threads()
            fut: Future = Future()
            self._futures[key] = fut
        self._queue.put((fut, source_id, Path(path)))

    def in_flight(self) -> int:
        with self._lock:
            return len(self._futures)

    def take(self, source_id: str, path: Path):
        """The parsed document for a submitted file (blocking), raising
        `ParseError` on failure; `None` if the file was never submitted."""
        with self._lock:
            fut = self._futures.pop((source_id, str(path)), None)
        if fut is None:
            return None
        return fut.result()

    def discard(self, source_id: str, path: Path) -> None:
        """Forget a submitted file whose result is no longer needed."""
        with self._lock:
            fut = self._futures.pop((source_id, str(path)), None)
        if fut is not None:
            fut.cancel()

    def reset(self) -> None:
        """Drop everything not yet consumed (end of a run): queued tasks are
        cancelled, running ones finish and are discarded."""
        with self._lock:
            futs = list(self._futures.values())
            self._futures.clear()
        for f in futs:
            f.cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.reset()
        for _ in self._threads:
            self._queue.put(None)
        for slot in self._slots:
            slot.kill()
