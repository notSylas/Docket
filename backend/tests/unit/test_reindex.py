"""`docket reindex`: rebuild FTS5 + LanceDB from SQLite under the current model."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from docket.core.db.models import SourceStatus
from docket.infra.index import reindex as reindex_module
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.manifest import (
    IndexManifestGuard,
    IndexManifestMismatchError,
    read_manifest,
)
from docket.infra.index.reindex import reindex
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.index.visual_index import LancePageIndexWriter, PageRecord
from docket.infra.inference.gateway import FakeInferenceGateway, InferenceUnavailableError
from docket.infra.retrieval.hybrid import fts_search, vector_search, visual_search
from test_hybrid_retrieval import _insert_metadata


def _rec(chunk_id: str, body: str, source_id: str = "src_1", version: str = "ev_1") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id=source_id,
        evidence_version_id=version,
        evidence_unit_id=f"eu_{version}",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text=body,
        content_hash="hash_" + chunk_id,
    )


class _Env:
    def __init__(self, engine: Engine, tmp_path: Path):
        self.engine = engine
        self.db_path = tmp_path / "lancedb"
        self.manifest_path = tmp_path / "index_manifest.json"
        self.vector = LanceIndexWriter(self.db_path, engine=engine)
        self.pages = LancePageIndexWriter(self.db_path)

    def guard(self, model: str) -> IndexManifestGuard:
        return IndexManifestGuard(self.manifest_path, model)

    def build(self, records, *, model="m1", dim=32, page_text="a page about alpha"):
        _insert_metadata(self.engine, records)
        gateway = FakeInferenceGateway(embed_dim=dim)
        IndexManager(
            FtsIndexWriter(self.engine),
            self.vector,
            gateway,
            self.pages,
            manifest_guard=IndexManifestGuard(self.manifest_path, model),
        ).upsert_chunks(records)
        self.pages.upsert(
            [PageRecord(records[0].evidence_version_id, records[0].source_id, 1, page_text)],
            [gateway.embed(page_text)],
        )

    def reindex(self, gateway, model="m2", **kw):
        return reindex(
            engine=self.engine,
            db_path=self.db_path,
            gateway=gateway,
            manifest_path=self.manifest_path,
            embed_model=model,
            sleep=lambda s: None,
            **kw,
        )


@pytest.fixture()
def env(migrated_sqlite_engine: Engine, tmp_path: Path) -> _Env:
    return _Env(migrated_sqlite_engine, tmp_path)


_RECORDS = [_rec("chk_a", "alpha volcano ash"), _rec("chk_b", "beta glacier ice")]


def test_reindex_switches_dimension_and_search_works(env: _Env) -> None:
    env.build(_RECORDS)
    gateway = FakeInferenceGateway(embed_dim=16)
    # Old manifest refuses the new model until reindex runs.
    with pytest.raises(IndexManifestMismatchError):
        vector_search(env.vector.table, env.engine, gateway, "alpha", 2, env.guard("m2"))

    result = env.reindex(gateway, batch_size=1)

    assert (result.chunks, result.pages, result.embed_dimension) == (2, 1, 16)
    manifest = read_manifest(env.manifest_path)
    assert (manifest.embed_model, manifest.embed_dimension) == ("m2", 16)
    assert env.vector.vector_dimension() == 16
    assert env.pages.vector_dimension() == 16
    guard = env.guard("m2")
    assert vector_search(
        env.vector.table, env.engine, gateway, "alpha volcano ash", 2, guard
    )[0] == "chk_a"
    # No chunk spans page 1 so the result is empty, but the search must not raise.
    assert visual_search(env.pages.table, env.engine, gateway, "a page", 2, guard) == []
    assert fts_search(env.engine, "glacier", 2) == ["chk_b"]
    assert gateway.embed_batch_calls  # embedded in batches
    assert env.vector.table.count_rows() == 2


def test_reindex_reembeds_pages_without_describe_image(env: _Env) -> None:
    env.build(_RECORDS)
    gateway = FakeInferenceGateway(embed_dim=16)
    env.reindex(gateway)
    assert gateway.describe_image_calls == []
    assert "a page about alpha" in gateway.embed_calls
    row = env.pages.table.to_arrow().to_pylist()[0]
    assert row["description"] == "a page about alpha"
    assert row["vector"] == pytest.approx(gateway.embed("a page about alpha"))


def test_reindex_failure_midway_leaves_old_index_and_manifest(env: _Env) -> None:
    env.build(_RECORDS)
    old_manifest = read_manifest(env.manifest_path)
    old_chunks_version = env.vector.table.version

    class _Flaky(FakeInferenceGateway):
        def embed_batch(self, texts):
            if self.embed_batch_calls:  # first batch ok, second fails
                raise InferenceUnavailableError("ollama died")
            return super().embed_batch(texts)

    with pytest.raises(InferenceUnavailableError):
        env.reindex(_Flaky(embed_dim=16), batch_size=1)

    assert read_manifest(env.manifest_path) == old_manifest
    assert env.vector.table.version == old_chunks_version
    old_gateway = FakeInferenceGateway(embed_dim=32)
    assert vector_search(
        env.vector.table, env.engine, old_gateway, "alpha volcano ash", 2, env.guard("m1")
    )[0] == "chk_a"
    assert fts_search(env.engine, "glacier", 2) == ["chk_b"]
    assert sorted(env.vector._db.list_tables().tables) == ["chunks", "pages"]  # scratch dropped


def test_reindex_failure_during_swap_restores_tables(env: _Env, monkeypatch) -> None:
    env.build(_RECORDS)
    old_manifest = read_manifest(env.manifest_path)

    def boom(*a, **k):
        raise OSError("manifest write failed")

    monkeypatch.setattr(reindex_module, "write_manifest", boom)
    with pytest.raises(OSError):
        env.reindex(FakeInferenceGateway(embed_dim=16))

    assert read_manifest(env.manifest_path) == old_manifest
    assert env.vector.vector_dimension() == 32
    assert env.pages.vector_dimension() == 32
    assert vector_search(
        env.vector.table,
        env.engine,
        FakeInferenceGateway(embed_dim=32),
        "alpha volcano ash",
        2,
        env.guard("m1"),
    )[0] == "chk_a"


def test_reindex_excludes_revoked_sources_and_rebuilds_fts(env: _Env) -> None:
    env.build(_RECORDS)
    env.build([_rec("chk_r", "revoked words", source_id="src_r", version="ev_r")])
    with env.engine.begin() as conn:
        conn.execute(
            text("UPDATE sources SET status = :s WHERE id = 'src_r'"),
            {"s": SourceStatus.REVOKED.name},
        )
        conn.execute(text("INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) "
                          "VALUES ('chk_orphan', 'orphan', 'ev_gone', 'src_1')"))

    result = env.reindex(FakeInferenceGateway(embed_dim=16))

    assert result.chunks == 2
    assert env.vector.existing_chunk_ids(["chk_a", "chk_b", "chk_r"]) == {"chk_a", "chk_b"}
    assert FtsIndexWriter(env.engine).existing_chunk_ids(
        ["chk_a", "chk_b", "chk_r", "chk_orphan"]
    ) == {"chk_a", "chk_b"}
    assert env.pages.version_ids_for_source("src_r") == set()


def test_reindex_cli(migrated_sqlite_engine, tmp_path, monkeypatch) -> None:
    from docket.interfaces.cli import main as cli_main
    from docket.interfaces.cli.context import AppContext

    gateway = FakeInferenceGateway(embed_dim=16)
    context = AppContext.for_testing(data_dir=tmp_path / "data", gateway=gateway)
    monkeypatch.setattr(cli_main, "build_context", lambda: context)
    _insert_metadata(context.engine, _RECORDS)
    FtsIndexWriter(context.engine).upsert(_RECORDS, None)

    result = CliRunner().invoke(cli_main.app, ["reindex"])

    assert result.exit_code == 0, result.output
    assert "chunks=2" in result.output and "dimension=16" in result.output
    assert read_manifest(context.settings.index_manifest_path).embed_dimension == 16
