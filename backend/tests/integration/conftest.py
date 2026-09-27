"""Fixtures shared by integration tests.

`migrated_sqlite_engine` mirrors `tests/unit/conftest.py`'s fixture of the
same name (a SQLite `Engine` pointed at a fresh, `tmp_path`-backed DB with
CP1's migration applied) -- duplicated here rather than imported across
test-tree boundaries, since pytest fixtures are directory-scoped and
`tests/integration/` previously had no conftest of its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from docket.core.db.engine import get_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(sqlite_path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "src" / "docket" / "core" / "db" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return config


@pytest.fixture
def migrated_sqlite_engine(tmp_path: Path) -> Engine:
    sqlite_path = tmp_path / "docket.sqlite3"
    command.upgrade(_alembic_config(sqlite_path), "head")
    engine = get_engine(sqlite_path)
    yield engine
    engine.dispose()
