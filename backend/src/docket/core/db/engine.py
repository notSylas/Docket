"""SQLite engine/session helpers for Docket.

Enables WAL journaling, a busy timeout, synchronous=NORMAL (better concurrent read/write behavior for a
local-first desktop app) and foreign key enforcement (off by default in
SQLite) on every new connection.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker

# How long a statement waits on another connection's lock before SQLITE_BUSY.
# The cross-process writer lock (core.writer_lock) is the coordination layer;
# this only absorbs short overlaps such as a WAL checkpoint or a reader's
# brief schema lock.
BUSY_TIMEOUT_MS = 15_000


def get_engine(sqlite_path: Path) -> Engine:
    sqlite_path = Path(sqlite_path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{sqlite_path}", future=True)
    # Long-lived windows must not write through pre-restore connections.
    # Writer services carry this identity even when constructed lazily.
    directory_stat = sqlite_path.parent.stat()
    engine._docket_directory_identity = (directory_stat.st_dev, directory_stat.st_ino)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        # NORMAL is crash-safe under WAL (a power loss can lose the last
        # commits but never corrupts the database) and avoids an fsync per
        # commit; FULL buys nothing for a local, re-derivable index.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
