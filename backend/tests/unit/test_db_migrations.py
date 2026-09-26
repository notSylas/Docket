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
        "script_location", str(REPO_ROOT / "src" / "docket" / "db" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return config


def test_migration_upgrades_cleanly_to_head(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "docket.sqlite3"
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
    sqlite_path = tmp_path / "docket.sqlite3"
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
    sqlite_path = tmp_path / "docket.sqlite3"
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


def _insert_minimal_chunk_row(con: sqlite3.Connection, chunk_id: str, chunk_text: str) -> None:
    """Insert one row into every parent table `chunks` FKs to, then one
    `chunks` row itself -- the minimal fixture for testing that the FTS
    porter-stemming migration (0003) correctly backfills `fts_chunks` from
    real `chunks` data rather than from a hand-inserted `fts_chunks` row."""
    now = "2024-01-01T00:00:00"
    cur = con.cursor()
    cur.execute(
        "INSERT INTO workspaces (id, name, created_at) VALUES ('ws_1', 'ws', ?)", (now,)
    )
    cur.execute(
        "INSERT INTO authorized_sources (id, workspace_id, scope_path, created_at) "
        "VALUES ('auth_1', 'ws_1', '/tmp', ?)",
        (now,),
    )
    cur.execute(
        "INSERT INTO sources (id, workspace_id, authorized_source_id, source_type, path, "
        "status, created_at, updated_at) "
        "VALUES ('src_1', 'ws_1', 'auth_1', 'local_folder', '/tmp/doc.pdf', 'active', ?, ?)",
        (now, now),
    )
    cur.execute(
        "INSERT INTO evidence_versions (id, source_id, content_hash, byte_size, mime_type, "
        "observed_at, parser_name, parser_version, is_current) "
        "VALUES ('ev_1', 'src_1', 'hash_ev', 100, 'application/pdf', ?, 'docling', '1.0', 1)",
        (now,),
    )
    cur.execute(
        "INSERT INTO evidence_units (id, evidence_version_id, unit_index, heading, "
        "content_hash) VALUES ('eu_1', 'ev_1', 0, NULL, 'hash_eu')"
    )
    cur.execute(
        "INSERT INTO chunk_recipes (id, chunk_size, overlap, splitter, parser_name, "
        "parser_version, created_at) VALUES ('rcp_1', 400, 50, 'words', 'docling', '1.0', ?)",
        (now,),
    )
    cur.execute(
        "INSERT INTO chunks (id, source_id, evidence_version_id, evidence_unit_id, "
        "chunk_recipe_id, ordinal, heading, text, content_hash) "
        "VALUES (?, 'src_1', 'ev_1', 'eu_1', 'rcp_1', 0, 'Journeys', ?, 'hash_chk')",
        (chunk_id, chunk_text),
    )
    con.commit()


def test_fts_porter_migration_backfills_fts_chunks_from_chunks_table(tmp_path: Path) -> None:
    """0003_fts_porter_stemming drops and recreates `fts_chunks` -- this
    proves the backfill actually re-derives its rows from `chunks` (the
    source of truth) rather than losing data, by inserting only into
    `chunks` on the pre-migration schema and checking the row survives the
    upgrade to head, searchable under the new tokenizer."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    # Schema as of 0002, one revision before the FTS porter migration.
    command.upgrade(config, "b3f1c9a02d17")

    con = sqlite3.connect(str(sqlite_path))
    try:
        _insert_minimal_chunk_row(con, "chk_1", "The PRD defines six user journeys.")
    finally:
        con.close()

    command.upgrade(config, "head")

    con = sqlite3.connect(str(sqlite_path))
    try:
        cur = con.cursor()
        # Singular "journey" query against text that only contains the
        # plural "journeys" -- passes only under the porter stemmer.
        cur.execute("SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH 'journey'")
        assert cur.fetchall() == [("chk_1",)]
    finally:
        con.close()


def test_fts_porter_migration_downgrade_recreates_plain_tokenizer(tmp_path: Path) -> None:
    """Downgrading past 0003 must not crash and must re-backfill the plain
    (non-stemmed) `fts_chunks` table from `chunks` -- exact-word matches
    still work even without stemming."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    command.upgrade(config, "head")

    con = sqlite3.connect(str(sqlite_path))
    try:
        _insert_minimal_chunk_row(con, "chk_1", "The PRD defines six user journeys.")
    finally:
        con.close()

    command.downgrade(config, "b3f1c9a02d17")

    con = sqlite3.connect(str(sqlite_path))
    try:
        cur = con.cursor()
        cur.execute("SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH 'journeys'")
        assert cur.fetchall() == [("chk_1",)]
    finally:
        con.close()


def test_ensure_schema_migrates_an_existing_pre_head_db(tmp_path: Path) -> None:
    """`_ensure_schema` used to skip Alembic entirely once the DB file
    existed, so a schema change (like 0003's FTS5 retokenization) would
    never reach an existing install unless someone ran `alembic upgrade
    head` by hand. It must now bring an existing DB forward to head too."""
    from docket.cli.context import _ensure_schema

    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    command.upgrade(config, "b3f1c9a02d17")  # one revision behind head (0003)
    assert sqlite_path.exists()

    con = sqlite3.connect(str(sqlite_path))
    try:
        _insert_minimal_chunk_row(con, "chk_1", "The PRD defines six user journeys.")
    finally:
        con.close()

    _ensure_schema(sqlite_path)

    # Functional proof of reaching 0003, not just a revision-id check:
    # singular "journey" only matches plural "journeys" under the porter
    # stemmer that 0003 introduces.
    con = sqlite3.connect(str(sqlite_path))
    try:
        cur = con.cursor()
        cur.execute("SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH 'journey'")
        assert cur.fetchall() == [("chk_1",)]
    finally:
        con.close()


def test_ensure_schema_is_a_safe_no_op_when_already_at_head(tmp_path: Path) -> None:
    """Calling `_ensure_schema` twice (e.g. two CLI invocations in a row)
    must not error or reset data -- alembic upgrade("head") is a no-op once
    the DB is already current."""
    from docket.cli.context import _ensure_schema

    sqlite_path = tmp_path / "docket.sqlite3"
    _ensure_schema(sqlite_path)
    con = sqlite3.connect(str(sqlite_path))
    try:
        _insert_minimal_chunk_row(con, "chk_1", "The PRD defines six user journeys.")
    finally:
        con.close()

    _ensure_schema(sqlite_path)  # must not drop/recreate tables or error

    con = sqlite3.connect(str(sqlite_path))
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM chunks WHERE id = 'chk_1'")
        assert cur.fetchall() == [("chk_1",)]
    finally:
        con.close()


def test_migration_enables_foreign_keys_pragma_is_settable(tmp_path: Path) -> None:
    """Sanity check that the migrated schema's FKs are enforceable (the app
    engine turns this pragma on itself; here we just prove the schema
    supports it once the pragma is set)."""
    sqlite_path = tmp_path / "docket.sqlite3"
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
