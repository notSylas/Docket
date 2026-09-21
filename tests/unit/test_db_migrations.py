"""Tests that exercise the real Alembic migration (not Base.metadata.create_all())."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(sqlite_path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "src" / "attest" / "db" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return config


def test_migration_upgrades_cleanly_to_head(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "attest.sqlite3"
    config = _alembic_config(sqlite_path)

    command.upgrade(config, "head")

    assert sqlite_path.exists()

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected_tables = {
        "workspaces",
        "authorized_sources",
        "sources",
        "evidence_versions",
        "evidence_units",
        "chunk_recipes",
        "chunks",
        "ingestion_jobs",
    }
    assert expected_tables.issubset(table_names)
    engine.dispose()


def test_migration_downgrade_drops_everything(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "attest.sqlite3"
    config = _alembic_config(sqlite_path)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    # alembic_version is the only table alembic itself manages/keeps.
    assert table_names <= {"alembic_version"}
    engine.dispose()


def test_fts_chunks_table_created_and_queryable(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "attest.sqlite3"
    config = _alembic_config(sqlite_path)

    command.upgrade(config, "head")

    con = sqlite3.connect(str(sqlite_path))
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO fts_chunks (chunk_id, text) VALUES (?, ?)",
            ("chk_deadbeef", "the quick brown fox jumps over the lazy dog"),
        )
        con.commit()

        cur.execute("SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH ?", ("quick fox",))
        rows = cur.fetchall()
        assert rows == [("chk_deadbeef",)]

        cur.execute("SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH ?", ("nonexistent",))
        assert cur.fetchall() == []
    finally:
        con.close()


def test_migration_enables_foreign_keys_pragma_is_settable(tmp_path: Path) -> None:
    """Sanity check that the migrated schema's FKs are enforceable (the app
    engine turns this pragma on itself; here we just prove the schema
    supports it once the pragma is set)."""
    sqlite_path = tmp_path / "attest.sqlite3"
    config = _alembic_config(sqlite_path)
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO sources (id, workspace_id, authorized_source_id, "
                    "source_type, path, status, created_at, updated_at) "
                    "VALUES ('src_x', 'ws_missing', 'auth_missing', 'local_folder', "
                    "'/tmp', 'active', '2024-01-01', '2024-01-01')"
                )
            )
    engine.dispose()
