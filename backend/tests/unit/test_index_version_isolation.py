"""Index isolation by evidence version (Upgrade doc 04 section 6): index rows
carry `evidence_version_id`, vector top-k is pre-filtered to eligible
versions, and reconcile/purge find FTS-only orphans and `pages` rows."""

from __future__ import annotations

from pathlib import Path

import lancedb
import pytest
from sqlalchemy import Engine, text

from docket.core.db.engine import get_session_factory
from docket.core.db.models import SourceStatus, VersionStatus
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.index.visual_index import LancePageIndexWriter, PageRecord
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.retrieval.hybrid import vector_search
from docket.services.sources.manager import SourceManager
from docket.services.sources.purge import SourcePurgeService
from test_hybrid_retrieval import _insert_metadata


def _rec(chunk_id: str, version_id: str, body: str, source_id: str = "src_1") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id=source_id,
        evidence_version_id=version_id,
        evidence_unit_id=f"eu_{version_id}",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text=body,
        content_hash="hash_" + chunk_id,
    )


def _fts_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM fts_chunks")).scalar_one()


# -- (b) pre-filter: stale rows cannot displace valid top-k ------------------


def test_stale_vector_rows_do_not_displace_valid_top_k(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    query = "the query text"
    # Three superseded rows sit exactly on the query vector, so an
    # unfiltered top-2 is entirely stale; two READY rows are farther away.
    stale = [_rec(f"chk_stale{i}", "ev_old", query) for i in range(3)]
    valid = [_rec(f"chk_ok{i}", "ev_new", f"unrelated {i}") for i in range(2)]
    _insert_metadata(migrated_sqlite_engine, stale, version_status=VersionStatus.SUPERSEDED)
    _insert_metadata(migrated_sqlite_engine, valid)

    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(stale, [gateway.embed(r.text) for r in stale])
    writer.upsert(valid, [gateway.embed(r.text) for r in valid])

    results = vector_search(writer.table, migrated_sqlite_engine, gateway, query, top_k=2)

    assert set(results) == {"chk_ok0", "chk_ok1"}


def test_vector_search_with_no_eligible_versions_returns_empty(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    records = [_rec("chk_a", "ev_1", "alpha")]
    _insert_metadata(migrated_sqlite_engine, records, status=SourceStatus.REVOKED)
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(records, [gateway.embed("alpha")])

    assert vector_search(writer.table, migrated_sqlite_engine, gateway, "alpha", top_k=3) == []


# -- (e) legacy LanceDB table without evidence_version_id --------------------


def _make_legacy_table(db_path: Path, gateway: FakeInferenceGateway, records: list[ChunkRecord]):
    db = lancedb.connect(str(db_path))
    db.create_table(
        "chunks",
        data=[
            {
                "chunk_id": r.chunk_id,
                "source_id": r.source_id,
                "text": r.text,
                "vector": gateway.embed(r.text),
            }
            for r in records
        ],
    )


def test_legacy_table_is_upgraded_and_backfilled_from_sqlite(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    records = [_rec("chk_a", "ev_1", "alpha"), _rec("chk_b", "ev_1", "beta")]
    _insert_metadata(migrated_sqlite_engine, records)
    # A row with no SQLite counterpart: stays NULL, so it can never be served.
    orphan = _rec("chk_orphan", "ev_gone", "alpha")
    _make_legacy_table(tmp_path / "lancedb", gateway, records + [orphan])

    writer = LanceIndexWriter(tmp_path / "lancedb", engine=migrated_sqlite_engine)
    table = writer.table

    rows = {r["chunk_id"]: r["evidence_version_id"] for r in table.search().to_list()}
    assert rows == {"chk_a": "ev_1", "chk_b": "ev_1", "chk_orphan": None}
    served = vector_search(table, migrated_sqlite_engine, gateway, "alpha", top_k=5)
    assert set(served) == {"chk_a", "chk_b"}
    # Upsert after the upgrade works against the evolved schema.
    new = _rec("chk_c", "ev_1", "gamma")
    writer.upsert([new], [gateway.embed("gamma")])
    assert writer.chunk_ids_for_source("src_1") >= {"chk_c"}


def test_legacy_table_without_engine_refuses_instead_of_mixing_versions(
    tmp_path: Path,
) -> None:
    gateway = FakeInferenceGateway()
    _make_legacy_table(tmp_path / "lancedb", gateway, [_rec("chk_a", "ev_1", "alpha")])

    with pytest.raises(RuntimeError, match="evidence_version_id"):
        LanceIndexWriter(tmp_path / "lancedb").table


# -- (d) FTS-only orphans: reconcile and purge -------------------------------


def _index_manager(engine: Engine, tmp_path: Path) -> IndexManager:
    return IndexManager(
        FtsIndexWriter(engine),
        LanceIndexWriter(tmp_path / "lancedb", engine=engine),
        FakeInferenceGateway(),
        LancePageIndexWriter(tmp_path / "lancedb"),
    )


def test_reconcile_removes_fts_only_orphan(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    manager = _index_manager(migrated_sqlite_engine, tmp_path)
    keep = _rec("chk_keep", "ev_1", "keep me")
    manager.upsert_chunks([keep])
    # Crash between the two writes: FTS row exists, vector row never written.
    FtsIndexWriter(migrated_sqlite_engine).upsert([_rec("chk_orphan", "ev_1", "orphan")], None)

    stats = manager.reconcile_source("src_1", {"chk_keep"})

    assert stats.deleted == 1
    fts = FtsIndexWriter(migrated_sqlite_engine)
    assert fts.existing_chunk_ids(["chk_keep", "chk_orphan"]) == {"chk_keep"}


def test_purge_removes_fts_only_orphan_and_pages(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    session_factory = get_session_factory(migrated_sqlite_engine)
    source_manager = SourceManager(session_factory)
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)
    manager = _index_manager(migrated_sqlite_engine, tmp_path)
    FtsIndexWriter(migrated_sqlite_engine).upsert(
        [_rec("chk_orphan", "ev_gone", "orphan", source_id=source.id)], None
    )
    pages = LancePageIndexWriter(tmp_path / "lancedb")
    pages.upsert(
        [PageRecord("ev_gone", source.id, 1, "a page"), PageRecord("ev_other", "src_x", 1, "x")],
        [FakeInferenceGateway().embed("a page"), FakeInferenceGateway().embed("x")],
    )
    source_manager.deactivate_source(source.id)
    source_manager.request_hard_delete(source.id)
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)

    SourcePurgeService(
        session_factory, ContentAddressedStore(store_root), index_manager=manager
    ).purge_source(source.id)

    assert _fts_count(migrated_sqlite_engine) == 0
    assert pages.version_ids_for_source(source.id) == set()
    assert pages.version_ids_for_source("src_x") == {"ev_other"}


def test_reconcile_removes_pages_of_non_current_versions(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager = _index_manager(migrated_sqlite_engine, tmp_path)
    pages = LancePageIndexWriter(tmp_path / "lancedb")
    emb = FakeInferenceGateway().embed("p")
    pages.upsert(
        [PageRecord("ev_old", "src_1", 1, "p"), PageRecord("ev_new", "src_1", 1, "p")], [emb, emb]
    )

    manager.reconcile_source("src_1", set(), current_version_ids={"ev_new"})

    assert pages.version_ids_for_source("src_1") == {"ev_new"}


def test_delete_version_removes_fts_vector_and_pages(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager = _index_manager(migrated_sqlite_engine, tmp_path)
    manager.upsert_chunks([_rec("chk_old", "ev_old", "old"), _rec("chk_new", "ev_new", "new")])
    pages = LancePageIndexWriter(tmp_path / "lancedb")
    emb = FakeInferenceGateway().embed("p")
    pages.upsert(
        [PageRecord("ev_old", "src_1", 1, "p"), PageRecord("ev_new", "src_1", 1, "p")], [emb, emb]
    )

    manager.delete_version("ev_old")

    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb", engine=migrated_sqlite_engine)
    assert fts.chunk_ids_for_source("src_1") == {"chk_new"}
    assert vector.chunk_ids_for_source("src_1") == {"chk_new"}
    assert pages.version_ids_for_source("src_1") == {"ev_new"}
