"""Migration 0012 (folder-first columns): additive, nullable, reversible."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "a7d2e4c18b5f"  # 0011_fts_version_columns


def _config(sqlite_path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "src" / "docket" / "core" / "db" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return config


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_0012_adds_nullable_columns_keeps_rows_and_downgrades(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _config(sqlite_path)
    command.upgrade(config, PREVIOUS_HEAD)

    now = "2024-01-01T00:00:00"
    con = sqlite3.connect(str(sqlite_path))
    try:
        cur = con.cursor()
        cur.execute("INSERT INTO workspaces (id, name, created_at) VALUES ('ws_1', 'ws', ?)", (now,))
        cur.execute(
            "INSERT INTO authorized_sources (id, workspace_id, scope_path, created_at) "
            "VALUES ('auth_1', 'ws_1', '/tmp', ?)",
            (now,),
        )
        cur.execute(
            "INSERT INTO sources (id, workspace_id, authorized_source_id, source_type, path, "
            "status, created_at, updated_at) "
            "VALUES ('src_1', 'ws_1', 'auth_1', 'local_folder', '/tmp/docs', 'ACTIVE', ?, ?)",
            (now, now),
        )
        cur.execute(
            "INSERT INTO evidence_versions (id, source_id, content_hash, byte_size, mime_type, "
            "observed_at, parser_name, parser_version, status) "
            "VALUES ('ev_1', 'src_1', 'h1', 5, NULL, ?, 'docling', '1.0', 'FAILED')",
            (now,),
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    assert {"display_name", "rules_json"} <= _columns(engine, "sources")
    assert {"failure_reason", "source_mtime"} <= _columns(engine, "evidence_versions")
    with engine.connect() as conn:
        src = conn.execute(
            text("SELECT id, path, display_name, rules_json FROM sources")
        ).one()
        ver = conn.execute(
            text("SELECT id, content_hash, failure_reason, source_mtime FROM evidence_versions")
        ).one()
    assert tuple(src) == ("src_1", "/tmp/docs", None, None)
    assert tuple(ver) == ("ev_1", "h1", None, None)
    engine.dispose()

    command.downgrade(config, PREVIOUS_HEAD)
    engine = create_engine(f"sqlite:///{sqlite_path}")
    assert not ({"display_name", "rules_json"} & _columns(engine, "sources"))
    assert not ({"failure_reason", "source_mtime"} & _columns(engine, "evidence_versions"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sources")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM evidence_versions")).scalar() == 1
    engine.dispose()
