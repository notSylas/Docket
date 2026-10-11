"""One advisory library writer per data directory, without locking readers.

The file is permanent: unlinking it would let two writers lock different
inodes. Its JSON is diagnostic only; flock, never a PID check, owns the lock.
Nested operations on the same thread share ownership, while other threads
and processes wait for at most the configured timeout.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator

LOCK_FILENAME = "library-writer.lock"
DEFAULT_WAIT_SECONDS = 5.0


class WriterLockBusyError(RuntimeError):
    """A bounded wait expired before this operation made any changes."""

    ui_message = "Another Docket window is updating the library"

    def __init__(self, owner: dict, timeout: float):
        self.owner = owner
        self.timeout = timeout
        description = (
            f"process {owner.get('pid', 'unknown')}, "
            f"started {owner.get('started_at', 'unknown')}, "
            f"doing {owner.get('operation', 'a library update')}"
        )
        super().__init__(
            f"The library is being updated by {description}. "
            f"Waited {timeout:g} seconds. Try again when that update finishes."
        )


@dataclass
class _State:
    mutex: threading.RLock = field(default_factory=threading.RLock)
    fd: int | None = None
    depth: int = 0


_states: dict[Path, _State] = {}
_registry_mutex = threading.Lock()


def _after_fork() -> None:
    # A child must not inherit re-entrancy or keep a parent's flock alive.
    global _registry_mutex
    for state in _states.values():
        if state.fd is not None:
            os.close(state.fd)  # close only, never LOCK_UN the parent's lock
    _states.clear()
    _registry_mutex = threading.Lock()


os.register_at_fork(after_in_child=_after_fork)


class WriterLock:
    def __init__(self, data_dir: Path | str, *, timeout: float = DEFAULT_WAIT_SECONDS):
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("writer lock timeout must be finite and non-negative")
        self.path = Path(data_dir).expanduser().resolve() / LOCK_FILENAME
        self.timeout = timeout

    def _owner(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextmanager
    def hold(self, operation: str) -> Iterator[None]:
        deadline = time.monotonic() + self.timeout
        with _registry_mutex:
            state = _states.setdefault(self.path, _State())
        if not state.mutex.acquire(timeout=self.timeout):
            raise WriterLockBusyError(self._owner(), self.timeout)
        try:
            if state.depth:
                state.depth += 1
                try:
                    yield
                finally:
                    state.depth -= 1
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            state.fd = fd
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise WriterLockBusyError(self._owner(), self.timeout) from None
                        time.sleep(min(0.05, remaining))
                owner = {
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "operation": operation,
                }
                os.ftruncate(fd, 0)
                os.write(fd, json.dumps(owner).encode("utf-8"))
                state.depth = 1
                try:
                    yield
                finally:
                    state.depth = 0
                    # Diagnostic metadata may survive a kill. Clear it on a
                    # normal exit, while still owning the OS lock.
                    os.ftruncate(fd, 0)
            finally:
                os.close(fd)  # releases flock, including exceptional exits
                state.fd = None
        finally:
            state.mutex.release()


def lock_for_sessions(session_factory) -> WriterLock:
    """Use the actual database location, including explicitly rooted tests."""
    from docket.core.config import settings
    from sqlalchemy import Engine

    bind = getattr(session_factory, "kw", {}).get("bind")
    database = bind.url.database if isinstance(bind, Engine) else None
    data_dir = Path(database).parent if database and database != ":memory:" else settings.data_dir
    return WriterLock(data_dir)


def writer_operation(operation: str, *, dry_run: bool = False) -> Callable:
    """Wrap a service's complete transition, including nested writer services."""
    def decorate(method):
        @wraps(method)
        def guarded(self, *args, **kwargs):
            if dry_run and kwargs.get("dry_run", False):
                return method(self, *args, **kwargs)
            with self._writer_lock.hold(operation):
                return method(self, *args, **kwargs)
        return guarded
    return decorate
