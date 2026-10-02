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
        "script_location", str(REPO_ROOT / "src" / "docket" / "core" / "db" / "migrations")
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


def test_version_status_migration_backfills_from_is_current(tmp_path: Path) -> None:
    """0008_version_status drops `is_current` in favor of `status`. Prove the
    backfill is exactly the deterministic mapping Upgrade doc 03 section 4
    specifies: `is_current=True` -> `READY`, `is_current=False` ->
    `SUPERSEDED` -- by inserting real rows on the pre-0008 schema (one
    revision behind head) with both values, then checking what `status` each
    one lands on after upgrading to head."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    # One revision behind head (0007), before `status` exists.
    command.upgrade(config, "e9d307126406")

    now = "2024-01-01T00:00:00"
    con = sqlite3.connect(str(sqlite_path))
    try:
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
            "VALUES ('ev_current', 'src_1', 'hash_current', 1, NULL, ?, 'docling', '1.0', 1)",
            (now,),
        )
        cur.execute(
            "INSERT INTO evidence_versions (id, source_id, content_hash, byte_size, mime_type, "
            "observed_at, parser_name, parser_version, is_current) "
            "VALUES ('ev_old', 'src_1', 'hash_old', 1, NULL, ?, 'docling', '1.0', 0)",
            (now,),
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    column_names = {col["name"] for col in inspector.get_columns("evidence_versions")}
    assert "status" in column_names
    assert "is_current" not in column_names

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT id, status FROM evidence_versions WHERE id IN ('ev_current', 'ev_old')")
            ).all()
        )
    assert rows == {"ev_current": "READY", "ev_old": "SUPERSEDED"}
    engine.dispose()


def test_version_status_migration_downgrade_restores_is_current(tmp_path: Path) -> None:
    """Downgrading past 0008 must restore `is_current`, derived from
    `status == READY`, and drop `status`."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    command.upgrade(config, "head")

    now = "2024-01-01T00:00:00"
    con = sqlite3.connect(str(sqlite_path))
    try:
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
            "observed_at, parser_name, parser_version, status) "
            "VALUES ('ev_ready', 'src_1', 'hash_ready', 1, NULL, ?, 'docling', '1.0', 'READY')",
            (now,),
        )
        cur.execute(
            "INSERT INTO evidence_versions (id, source_id, content_hash, byte_size, mime_type, "
            "observed_at, parser_name, parser_version, status) "
            "VALUES ('ev_failed', 'src_1', 'hash_failed', 1, NULL, ?, 'docling', '1.0', 'FAILED')",
            (now,),
        )
        con.commit()
    finally:
        con.close()

    command.downgrade(config, "e9d307126406")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    column_names = {col["name"] for col in inspector.get_columns("evidence_versions")}
    assert "is_current" in column_names
    assert "status" not in column_names

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT id, is_current FROM evidence_versions WHERE id IN ('ev_ready', 'ev_failed')")
            ).all()
        )
    assert rows == {"ev_ready": 1, "ev_failed": 0}
    engine.dispose()


def test_unit_kind_locator_provenance_migration_backfills_existing_rows(tmp_path: Path) -> None:
    """0009 adds `evidence_units.unit_kind`/`locator_json` and
    `chunks.provenance` -- prove existing rows (inserted on the pre-0009
    schema, one revision behind head) get the server_default values
    (`unit_kind='section'`, `provenance='extracted'`) after upgrading to
    head, per Upgrade doc 03 section 7."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    # One revision behind head (0008), before the new columns exist.
    command.upgrade(config, "0f0bcf448de1")

    now = "2024-01-01T00:00:00"
    con = sqlite3.connect(str(sqlite_path))
    try:
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
            "observed_at, parser_name, parser_version, status) "
            "VALUES ('ev_1', 'src_1', 'hash_ev', 100, 'application/pdf', ?, 'docling', '1.0', 'READY')",
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
            "VALUES ('chk_1', 'src_1', 'ev_1', 'eu_1', 'rcp_1', 0, 'Journeys', 'some text', 'hash_chk')"
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    unit_columns = {col["name"] for col in inspector.get_columns("evidence_units")}
    chunk_columns = {col["name"] for col in inspector.get_columns("chunks")}
    assert {"unit_kind", "locator_json"} <= unit_columns
    assert "provenance" in chunk_columns

    with engine.connect() as conn:
        unit_row = conn.execute(
            text("SELECT unit_kind, locator_json FROM evidence_units WHERE id = 'eu_1'")
        ).one()
        chunk_row = conn.execute(
            text("SELECT provenance FROM chunks WHERE id = 'chk_1'")
        ).one()
    assert unit_row == ("section", None)
    assert chunk_row == ("extracted",)
    engine.dispose()


def test_unit_kind_locator_provenance_migration_downgrade_drops_columns(tmp_path: Path) -> None:
    """Downgrading past 0009 must cleanly drop `unit_kind`/`locator_json`/
    `provenance` and leave the rest of the row intact."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    command.upgrade(config, "head")

    now = "2024-01-01T00:00:00"
    con = sqlite3.connect(str(sqlite_path))
    try:
        _insert_minimal_chunk_row(con, "chk_1", "some text")
    finally:
        con.close()

    command.downgrade(config, "0f0bcf448de1")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    unit_columns = {col["name"] for col in inspector.get_columns("evidence_units")}
    chunk_columns = {col["name"] for col in inspector.get_columns("chunks")}
    assert "unit_kind" not in unit_columns
    assert "locator_json" not in unit_columns
    assert "provenance" not in chunk_columns

    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM chunks WHERE id = 'chk_1'")).one()
    assert row == ("chk_1",)
    engine.dispose()


def test_source_lifecycle_and_blob_references_migration_adds_columns_and_table(
    tmp_path: Path,
) -> None:
    """0010 adds `sources.sync_paused_at`/`retention_deadline`/`status_reason`
    and the new `evidence_blob_references` table -- prove the columns/table
    exist at head and that a pre-existing `sources` row (inserted on the
    pre-0010 schema) survives the upgrade with NULLs for the new columns
    (no backfill needed -- see Upgrade doc 03 sections 6/8)."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    # One revision behind head (0009), before the new columns/table exist.
    command.upgrade(config, "f9749e87a0e7")

    now = "2024-01-01T00:00:00"
    con = sqlite3.connect(str(sqlite_path))
    try:
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
        con.commit()
    finally:
        con.close()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    source_columns = {col["name"] for col in inspector.get_columns("sources")}
    assert {"sync_paused_at", "retention_deadline", "status_reason"} <= source_columns
    assert "evidence_blob_references" in set(inspector.get_table_names())
    ebr_columns = {col["name"] for col in inspector.get_columns("evidence_blob_references")}
    assert {"id", "content_hash", "referencing_table", "referencing_id", "role", "created_at"} <= (
        ebr_columns
    )

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT sync_paused_at, retention_deadline, status_reason FROM sources "
                "WHERE id = 'src_1'"
            )
        ).one()
    assert row == (None, None, None)
    engine.dispose()


def test_source_lifecycle_and_blob_references_migration_downgrade_drops_them(
    tmp_path: Path,
) -> None:
    """Downgrading past 0010 must cleanly drop the three new `sources`
    columns and the `evidence_blob_references` table."""
    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    command.upgrade(config, "head")

    command.downgrade(config, "f9749e87a0e7")

    engine = create_engine(f"sqlite:///{sqlite_path}")
    inspector = inspect(engine)
    source_columns = {col["name"] for col in inspector.get_columns("sources")}
    assert not ({"sync_paused_at", "retention_deadline", "status_reason"} & source_columns)
    assert "evidence_blob_references" not in set(inspector.get_table_names())
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
    real `chunks` data rather than from a hand-inserted `fts_chunks` row.

    Called against both pre-0008 schemas (`evidence_versions.is_current`)
    and post-0008 schemas (`evidence_versions.status`) by different tests in
    this module, so the `evidence_versions` insert introspects which column
    actually exists on the connection it's given rather than hardcoding one.
    """
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
    evidence_version_columns = {
        row[1] for row in cur.execute("PRAGMA table_info(evidence_versions)").fetchall()
    }
    if "status" in evidence_version_columns:
        cur.execute(
            "INSERT INTO evidence_versions (id, source_id, content_hash, byte_size, mime_type, "
            "observed_at, parser_name, parser_version, status) "
            "VALUES ('ev_1', 'src_1', 'hash_ev', 100, 'application/pdf', ?, 'docling', '1.0', 'READY')",
            (now,),
        )
    else:
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
    """`ensure_schema` used to skip Alembic entirely once the DB file
    existed, so a schema change (like 0003's FTS5 retokenization) would
    never reach an existing install unless someone ran `alembic upgrade
    head` by hand. It must now bring an existing DB forward to head too."""
    from docket.interfaces.cli.context import ensure_schema

    sqlite_path = tmp_path / "docket.sqlite3"
    config = _alembic_config(sqlite_path)
    command.upgrade(config, "b3f1c9a02d17")  # one revision behind head (0003)
    assert sqlite_path.exists()

    con = sqlite3.connect(str(sqlite_path))
    try:
        _insert_minimal_chunk_row(con, "chk_1", "The PRD defines six user journeys.")
    finally:
        con.close()

    ensure_schema(sqlite_path)

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
    """Calling `ensure_schema` twice (e.g. two CLI invocations in a row)
    must not error or reset data -- alembic upgrade("head") is a no-op once
    the DB is already current."""
    from docket.interfaces.cli.context import ensure_schema

    sqlite_path = tmp_path / "docket.sqlite3"
    ensure_schema(sqlite_path)
    con = sqlite3.connect(str(sqlite_path))
    try:
        _insert_minimal_chunk_row(con, "chk_1", "The PRD defines six user journeys.")
    finally:
        con.close()

    ensure_schema(sqlite_path)  # must not drop/recreate tables or error

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
