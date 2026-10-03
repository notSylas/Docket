"""Index manifest (Upgrade doc 04 section 7): round-trip, atomic write, and the
write-side / query-side refusal to mix embedding spaces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from docket.infra.index import manifest as manifest_module
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.manifest import (
    IndexManifestGuard,
    IndexManifestMismatchError,
    new_manifest,
    read_manifest,
    write_manifest,
)
from docket.infra.index.base import ChunkRecord
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.index.visual_index import LancePageIndexWriter, PageRecord
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.retrieval.hybrid import vector_search, visual_search
from test_hybrid_retrieval import _insert_metadata


def _rec(chunk_id: str, body: str) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id="src_1",
        evidence_version_id="ev_1",
        evidence_unit_id="eu_1",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text=body,
        content_hash="hash_" + chunk_id,
    )


def _setup(engine: Engine, tmp_path: Path, *, model: str = "m1", dim: int = 32):
    fts = FtsIndexWriter(engine)
    vector = LanceIndexWriter(tmp_path / "lancedb", engine=engine)
    pages = LancePageIndexWriter(tmp_path / "lancedb")
    path = tmp_path / "index_manifest.json"
    guard = IndexManifestGuard(
        path, model, lambda: [vector.vector_dimension(), pages.vector_dimension()]
    )
    gateway = FakeInferenceGateway(embed_dim=dim)
    manager = IndexManager(fts, vector, gateway, pages, manifest_guard=guard)
    return manager, vector, pages, path, guard, gateway


def _fts_ids(engine: Engine) -> set[str]:
    return FtsIndexWriter(engine).existing_chunk_ids(["chk_a", "chk_b"])


def test_manifest_round_trip_and_atomic_write(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "m.json"
    assert read_manifest(path) is None
    manifest = new_manifest("m1", 32)
    write_manifest(path, manifest)
    assert read_manifest(path) == manifest
    assert json.loads(path.read_text())["tokenizer"] is None
    assert json.loads(path.read_text())["embed_instruction"] is None

    # A crash during replace leaves the old file intact and no temp litter.
    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(manifest_module.os, "replace", boom)
    with pytest.raises(OSError):
        write_manifest(path, new_manifest("m2", 8))
    assert read_manifest(path) == manifest
    assert [p.name for p in tmp_path.iterdir()] == ["m.json"]


def test_first_write_creates_manifest(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    manager, _, _, path, _, _ = _setup(migrated_sqlite_engine, tmp_path)
    assert read_manifest(path) is None
    manager.upsert_chunks([_rec("chk_a", "alpha")])
    manifest = read_manifest(path)
    assert (manifest.embed_model, manifest.embed_dimension) == ("m1", 32)


def test_model_mismatch_refuses_write(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    manager, vector, _, path, _, _ = _setup(migrated_sqlite_engine, tmp_path)
    manager.upsert_chunks([_rec("chk_a", "alpha")])

    other, *_ = _setup(migrated_sqlite_engine, tmp_path, model="m2")
    with pytest.raises(IndexManifestMismatchError, match="docket reindex"):
        other.upsert_chunks([_rec("chk_b", "beta")])

    assert vector.existing_chunk_ids(["chk_a", "chk_b"]) == {"chk_a"}
    assert _fts_ids(migrated_sqlite_engine) == {"chk_a"}
    assert read_manifest(path).embed_model == "m1"


def test_dimension_mismatch_refuses_write(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    manager, vector, _, path, _, _ = _setup(migrated_sqlite_engine, tmp_path)
    manager.upsert_chunks([_rec("chk_a", "alpha")])

    other, *_ = _setup(migrated_sqlite_engine, tmp_path, dim=16)
    with pytest.raises(IndexManifestMismatchError, match="16"):
        other.upsert_chunks([_rec("chk_b", "beta")])

    assert vector.existing_chunk_ids(["chk_a", "chk_b"]) == {"chk_a"}
    assert _fts_ids(migrated_sqlite_engine) == {"chk_a"}
    assert read_manifest(path).embed_dimension == 32


def test_legacy_table_with_matching_dimension_is_adopted(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    legacy_manager, *_ = _setup(migrated_sqlite_engine, tmp_path)
    legacy_manager._manifest_guard = None  # written before manifests existed
    legacy_manager.upsert_chunks([_rec("chk_a", "alpha")])

    manager, vector, _, path, _, _ = _setup(migrated_sqlite_engine, tmp_path)
    assert read_manifest(path) is None
    manager.upsert_chunks([_rec("chk_b", "beta")])

    assert read_manifest(path).embed_dimension == 32
    assert vector.existing_chunk_ids(["chk_a", "chk_b"]) == {"chk_a", "chk_b"}


def test_legacy_table_with_other_dimension_is_refused(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    legacy_manager, *_ = _setup(migrated_sqlite_engine, tmp_path)
    legacy_manager._manifest_guard = None
    legacy_manager.upsert_chunks([_rec("chk_a", "alpha")])

    manager, vector, _, path, _, _ = _setup(migrated_sqlite_engine, tmp_path, dim=16)
    with pytest.raises(IndexManifestMismatchError, match="docket reindex"):
        manager.upsert_chunks([_rec("chk_b", "beta")])

    assert read_manifest(path) is None
    assert vector.existing_chunk_ids(["chk_a", "chk_b"]) == {"chk_a"}
    assert _fts_ids(migrated_sqlite_engine) == {"chk_a"}


def test_query_side_refuses_when_manifest_disagrees(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager, vector, pages, path, guard, gateway = _setup(migrated_sqlite_engine, tmp_path)
    record = _rec("chk_a", "alpha")
    _insert_metadata(migrated_sqlite_engine, [record])
    manager.upsert_chunks([record])
    pages.upsert([PageRecord("ev_1", "src_1", 1, "page")], [gateway.embed("page")])
    table = vector.table

    assert vector_search(table, migrated_sqlite_engine, gateway, "alpha", 3, guard) == ["chk_a"]

    wrong_model = IndexManifestGuard(path, "m2")
    with pytest.raises(IndexManifestMismatchError):
        vector_search(table, migrated_sqlite_engine, gateway, "alpha", 3, wrong_model)
    with pytest.raises(IndexManifestMismatchError):
        visual_search(pages.table, migrated_sqlite_engine, gateway, "x", 3, wrong_model)
    with pytest.raises(IndexManifestMismatchError, match="16"):
        vector_search(
            table, migrated_sqlite_engine, FakeInferenceGateway(embed_dim=16), "alpha", 3, guard
        )


def test_query_side_without_manifest_is_unchecked(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    guard = IndexManifestGuard(tmp_path / "none.json", "m1")
    guard.check_query(7)  # no manifest -> nothing to compare, no error
