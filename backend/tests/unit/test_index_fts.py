"""Unit tests for `FtsIndexWriter` (SQLite FTS5-backed lexical index)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from attest.index.base import ChunkRecord
from attest.index.fts_index import FtsIndexWriter


def _record(chunk_id: str, text: str, source_id: str = "src_1") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id=source_id,
        evidence_version_id="ev_1",
        evidence_unit_id="eu_1",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text=text,
        content_hash="hash_" + chunk_id,
    )


def test_upsert_then_match_query(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert([_record("chk_a", "the quick brown fox")], embeddings=None)

    with migrated_sqlite_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH 'quick fox'"
        ).fetchall()
    assert [r[0] for r in rows] == ["chk_a"]


def test_upsert_same_id_twice_no_duplicates(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert([_record("chk_a", "hello world")], embeddings=None)
    writer.upsert([_record("chk_a", "hello world updated")], embeddings=None)

    with migrated_sqlite_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT chunk_id, text FROM fts_chunks WHERE chunk_id = 'chk_a'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "hello world updated"


def test_delete_removes_rows(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert(
        [_record("chk_a", "alpha text"), _record("chk_b", "beta text")], embeddings=None
    )
    writer.delete(["chk_a"])

    assert writer.existing_chunk_ids(["chk_a", "chk_b"]) == {"chk_b"}


def test_delete_missing_ids_is_a_noop(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert([_record("chk_a", "alpha text")], embeddings=None)
    writer.delete(["chk_does_not_exist"])
    assert writer.existing_chunk_ids(["chk_a"]) == {"chk_a"}


def test_delete_empty_list_is_a_noop(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert([_record("chk_a", "alpha text")], embeddings=None)
    writer.delete([])
    assert writer.existing_chunk_ids(["chk_a"]) == {"chk_a"}


def test_existing_chunk_ids_subset(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert([_record("chk_a", "alpha"), _record("chk_b", "beta")], embeddings=None)

    result = writer.existing_chunk_ids(["chk_a", "chk_b", "chk_missing"])
    assert result == {"chk_a", "chk_b"}


def test_existing_chunk_ids_empty_input(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    assert writer.existing_chunk_ids([]) == set()


def test_delete_by_source_not_implemented(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    with pytest.raises(NotImplementedError, match="IndexManager.delete_source"):
        writer.delete_by_source("src_1")


def test_chunk_ids_for_source_not_implemented(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    with pytest.raises(NotImplementedError, match="IndexManager.reconcile_source"):
        writer.chunk_ids_for_source("src_1")
