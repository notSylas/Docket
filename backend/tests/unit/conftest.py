"""Shared fixtures for unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from docket.db.engine import get_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(sqlite_path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "src" / "docket" / "db" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return config


@pytest.fixture
def migrated_sqlite_engine(tmp_path: Path) -> Engine:
    """A SQLite `Engine` pointed at a fresh DB with the CP1 migration applied
    (so `chunks`, `fts_chunks`, etc. all exist), for tests that need a real
    schema rather than an in-memory stand-in."""
    sqlite_path = tmp_path / "docket.sqlite3"
    command.upgrade(_alembic_config(sqlite_path), "head")
    engine = get_engine(sqlite_path)
    yield engine
    engine.dispose()
