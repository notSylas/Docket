"""Unit tests for `IndexManager` -- the regression suite proving the
validation spike's flaw (full re-embed/re-index of the corpus on every
ingestion run, via LanceDB `mode="overwrite"` and a SQLite FTS5
DROP/recreate) is actually fixed: unchanged chunks are neither re-embedded
nor duplicated, and per-source deletes/reconciliation only touch what they
should.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from docket.index.base import ChunkRecord
from docket.index.fts_index import FtsIndexWriter
from docket.index.manager import IndexManager
from docket.index.vector_index import LanceIndexWriter
from docket.inference.gateway import FakeInferenceGateway


def _record(
    chunk_id: str, text: str, source_id: str = "src_1", ordinal: int = 0
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id=source_id,
        evidence_version_id="ev_1",
        evidence_unit_id="eu_1",
        chunk_recipe_id="rcp_1",
        ordinal=ordinal,
        heading=None,
        text=text,
        content_hash="hash_" + chunk_id,
    )


def _manager(migrated_sqlite_engine: Engine, tmp_path: Path) -> IndexManager:
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    return IndexManager(fts, vector, gateway)


def test_upsert_same_chunk_twice_no_duplicates(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    manager = IndexManager(fts, vector, gateway)

    record = _record("chk_a", "the quick brown fox")
    manager.upsert_chunks([record])
    manager.upsert_chunks([record])

    with migrated_sqlite_engine.connect() as conn:
        fts_rows = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM fts_chunks WHERE chunk_id = 'chk_a'"
        ).scalar()
    assert fts_rows == 1

    vector_table = vector._open_table()
    assert len(vector_table.search().to_list()) == 1


def test_reindexing_identical_records_makes_no_embed_calls(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    manager = IndexManager(fts, vector, gateway)

    records = [_record("chk_a", "alpha text"), _record("chk_b", "beta text")]
    stats1 = manager.upsert_chunks(records)
    assert stats1.upserted == 2
    assert stats1.skipped_unchanged == 0
    assert len(gateway.embed_calls) == 2

    # Re-ingest the exact same records: this is the spike's flaw -- it used
    # to re-embed and re-index everything. It must now embed nothing.
    stats2 = manager.upsert_chunks(records)
    assert stats2.upserted == 0
    assert stats2.skipped_unchanged == 2
    assert len(gateway.embed_calls) == 2  # unchanged from before -- no new calls


def test_upsert_mixed_new_and_existing_only_embeds_new(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager = _manager(migrated_sqlite_engine, tmp_path)
    gateway = manager._gateway

    existing = [_record("chk_a", "alpha text"), _record("chk_b", "beta text")]
    manager.upsert_chunks(existing)
    assert len(gateway.embed_calls) == 2

    new_record = _record("chk_c", "gamma text")
    stats = manager.upsert_chunks(existing + [new_record])

    assert stats.upserted == 1
    assert stats.skipped_unchanged == 2
    assert len(gateway.embed_calls) == 3
    assert gateway.embed_calls[-1] == "gamma text"


def test_upsert_empty_records_is_a_noop(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager = _manager(migrated_sqlite_engine, tmp_path)
    stats = manager.upsert_chunks([])
    assert stats == type(stats)(upserted=0, skipped_unchanged=0, deleted=0)
    assert len(manager._gateway.embed_calls) == 0


def test_delete_source_removes_only_that_sources_chunks(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    manager = IndexManager(fts, vector, gateway)

    manager.upsert_chunks(
        [
            _record("chk_a1", "src1 chunk one", source_id="src_1"),
            _record("chk_a2", "src1 chunk two", source_id="src_1"),
            _record("chk_b1", "src2 chunk one", source_id="src_2"),
        ]
    )

    manager.delete_source("src_1")

    assert fts.existing_chunk_ids(["chk_a1", "chk_a2", "chk_b1"]) == {"chk_b1"}
    assert vector.existing_chunk_ids(["chk_a1", "chk_a2", "chk_b1"]) == {"chk_b1"}
    assert vector.chunk_ids_for_source("src_1") == set()
    assert vector.chunk_ids_for_source("src_2") == {"chk_b1"}


def test_reconcile_source_deletes_only_stale_chunks(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    manager = IndexManager(fts, vector, gateway)

    manager.upsert_chunks(
        [
            _record("chk_1", "one", source_id="src_1", ordinal=0),
            _record("chk_2", "two", source_id="src_1", ordinal=1),
            _record("chk_3", "three", source_id="src_1", ordinal=2),
        ]
    )

    stats = manager.reconcile_source("src_1", current_chunk_ids={"chk_1", "chk_2"})

    assert stats.deleted == 1
    assert stats.upserted == 0
    assert stats.skipped_unchanged == 0
    assert fts.existing_chunk_ids(["chk_1", "chk_2", "chk_3"]) == {"chk_1", "chk_2"}
    assert vector.existing_chunk_ids(["chk_1", "chk_2", "chk_3"]) == {"chk_1", "chk_2"}


def test_reconcile_source_no_stale_chunks_is_a_noop(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager = _manager(migrated_sqlite_engine, tmp_path)
    manager.upsert_chunks([_record("chk_1", "one", source_id="src_1")])

    stats = manager.reconcile_source("src_1", current_chunk_ids={"chk_1"})
    assert stats.deleted == 0


def test_reconcile_source_unknown_source_is_a_noop(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager = _manager(migrated_sqlite_engine, tmp_path)
    stats = manager.reconcile_source("src_missing", current_chunk_ids=set())
    assert stats.deleted == 0


def test_fixture_corpus_smoke_fts_and_vector_search(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    """Index a small fixture corpus, then query FTS5 and LanceDB directly
    (bypassing IndexManager) to confirm each backend actually returns the
    right chunk for a query that should distinctly match it."""
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    manager = IndexManager(fts, vector, gateway)

    corpus = [
        _record("chk_penguins", "Emperor penguins huddle together in Antarctica", ordinal=0),
        _record("chk_volcanoes", "Volcanic eruptions release ash and magma", ordinal=1),
        _record("chk_coffee", "Coffee beans are roasted before brewing", ordinal=2),
        _record("chk_orchestra", "The orchestra tuned their instruments before the concert", ordinal=3),
        _record("chk_glaciers", "Glaciers slowly carve valleys over centuries", ordinal=4),
    ]
    manager.upsert_chunks(corpus)

    # FTS5: a lexical query for a term unique to one chunk should return it.
    with migrated_sqlite_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH 'coffee beans'"
        ).fetchall()
    assert [r[0] for r in rows] == ["chk_coffee"]

    # Vector: FakeInferenceGateway's embeddings are deterministic (same text
    # -> same vector), so embedding a chunk's own text again and searching
    # with it must nearest-match that same chunk.
    query_vector = gateway.embed("Volcanic eruptions release ash and magma")
    table = vector._open_table()
    results = table.search(query_vector).limit(1).to_list()
    assert results[0]["chunk_id"] == "chk_volcanoes"
