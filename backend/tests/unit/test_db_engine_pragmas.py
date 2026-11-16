"""SQLite connection pragmas set by get_engine, and the writer lock staying independent of them."""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import text

from docket.core.db.engine import BUSY_TIMEOUT_MS, get_engine
from docket.core.writer_lock import WriterLock, WriterLockBusyError


def _pragma(engine, name: str):
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_pragmas_are_set_on_every_connection(tmp_path: Path) -> None:
    engine = get_engine(tmp_path / "d.sqlite3")
    try:
        assert _pragma(engine, "journal_mode") == "wal"
        assert _pragma(engine, "foreign_keys") == 1
        assert _pragma(engine, "busy_timeout") == BUSY_TIMEOUT_MS
        assert _pragma(engine, "synchronous") == 1  # NORMAL
    finally:
        engine.dispose()


def test_busy_timeout_is_applied_to_new_pool_connections(tmp_path: Path) -> None:
    engine = get_engine(tmp_path / "d.sqlite3")
    try:
        with engine.connect() as a, engine.connect() as b:
            for conn in (a, b):
                assert conn.execute(text("PRAGMA busy_timeout")).scalar() == BUSY_TIMEOUT_MS
    finally:
        engine.dispose()


def test_writer_lock_still_excludes_a_second_holder(tmp_path: Path) -> None:
    get_engine(tmp_path / "d.sqlite3").dispose()
    first = WriterLock(tmp_path, timeout=0.2)
    errors: list[BaseException] = []

    def contender() -> None:
        try:
            with WriterLock(tmp_path, timeout=0.2).hold("second"):
                pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with first.hold("first"):
        t = threading.Thread(target=contender)
        t.start()
        t.join()
    assert len(errors) == 1 and isinstance(errors[0], WriterLockBusyError)


def test_writer_lock_is_reentrant_for_same_thread(tmp_path: Path) -> None:
    lock = WriterLock(tmp_path, timeout=0.2)
    with lock.hold("outer"):
        with lock.hold("inner"):
            pass
