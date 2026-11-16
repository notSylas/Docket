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


def test_reindex_renders_locator_context_same_as_ingestion(env: _Env) -> None:
    import json

    from docket.infra.index.context import index_text_for_chunk

    import dataclasses

    rec = dataclasses.replace(_rec("chk_x", "Sheet: S | Row: 7\nMonth: Jul"), heading="S")
    env.build([rec])
    locator = json.dumps({"sheet": "S", "context": ["FY2025-26", "fiscal year 2025 2026", "July"]})
    with env.engine.begin() as conn:
        conn.execute(text("UPDATE evidence_units SET locator_json = :l"), {"l": locator})
        conn.execute(
            text("UPDATE evidence_versions SET file_path = '/d/Revenue-FY2025-26.xlsx'")
        )
    env.reindex(FakeInferenceGateway(embed_dim=16))

    expected = index_text_for_chunk("/d/Revenue-FY2025-26.xlsx", locator, "S", rec.text)
    assert "Context: FY2025-26; fiscal year 2025 2026; July" in expected
    with env.engine.connect() as conn:
        fts_text = conn.exec_driver_sql("SELECT text FROM fts_chunks").scalar()
    assert fts_text == expected
    assert fts_search(env.engine, "2025", 2) == ["chk_x"]
    assert read_manifest(env.manifest_path).index_text_version == 2


# -- resumable reindex -----------------------------------------------------------

_MANY = [_rec(f"chk_{i:02d}", f"document number {i} about topic {i}") for i in range(7)]


class _Dies(FakeInferenceGateway):
    """Embeds `ok_batches` batches, then raises (a kill / Ollama going away)."""

    def __init__(self, ok_batches: int, exc: BaseException | None = None, **kw):
        super().__init__(**kw)
        self.ok_batches = ok_batches
        self.exc = exc or InferenceUnavailableError("interrupted")

    def embed_batch(self, texts):
        if len(self.embed_batch_calls) >= self.ok_batches:
            raise self.exc
        return super().embed_batch(texts)


def _scratch(env: _Env) -> Path:
    return env.db_path.with_name("lancedb-reindex")


@pytest.mark.parametrize("exc", [None, KeyboardInterrupt()])
def test_interrupted_reindex_resumes_instead_of_restarting(env: _Env, exc) -> None:
    env.build(_MANY)
    old_manifest = read_manifest(env.manifest_path)
    with pytest.raises((InferenceUnavailableError, KeyboardInterrupt)):
        env.reindex(_Dies(2, exc, embed_dim=16), batch_size=2)
    assert read_manifest(env.manifest_path) == old_manifest  # live index untouched
    assert _scratch(env).exists()
    assert sorted(env.vector._db.list_tables().tables) == ["chunks", "pages"]

    gateway = FakeInferenceGateway(embed_dim=16)
    result = env.reindex(gateway, batch_size=2)

    embedded = [t for batch in gateway.embed_batch_calls for t in batch]
    assert result.chunks == 7 and result.resumed_rows == 4
    # only the 3 unfinished chunks (+ the page description) were embedded again
    assert len([t for t in embedded if "document number" in t]) == 3
    assert not _scratch(env).exists()
    assert not _scratch(env).with_name("lancedb-reindex.json").exists()

    # identical outcome to an uninterrupted run
    fresh = FakeInferenceGateway(embed_dim=16)
    rows = {r["chunk_id"]: r["vector"] for r in env.vector.table.to_arrow().to_pylist()}
    assert sorted(rows) == sorted(r.chunk_id for r in _MANY)
    assert env.vector.table.count_rows() == 7
    assert read_manifest(env.manifest_path).embed_model == "m2"
    assert vector_search(
        env.vector.table, env.engine, fresh, "document number 3 about topic 3", 3, env.guard("m2")
    )


def test_resume_matches_uninterrupted_result_exactly(env: _Env, tmp_path: Path) -> None:
    env.build(_MANY)
    with pytest.raises(InferenceUnavailableError):
        env.reindex(_Dies(3, embed_dim=16), batch_size=2)
    env.reindex(FakeInferenceGateway(embed_dim=16), batch_size=2)
    resumed = {r["chunk_id"]: r["vector"] for r in env.vector.table.to_arrow().to_pylist()}

    (tmp_path / "other").mkdir()
    other = _Env(env.engine, tmp_path / "other")
    other.reindex(FakeInferenceGateway(embed_dim=16), batch_size=2)
    straight = {r["chunk_id"]: r["vector"] for r in other.vector.table.to_arrow().to_pylist()}
    assert resumed == straight


def test_resume_discarded_when_model_changes(env: _Env) -> None:
    env.build(_MANY)
    with pytest.raises(InferenceUnavailableError):
        env.reindex(_Dies(2, embed_dim=16), batch_size=2)
    gateway = FakeInferenceGateway(embed_dim=24)
    result = env.reindex(gateway, model="m3", batch_size=2)
    assert result.resumed_rows == 0 and result.embed_dimension == 24
    assert len([t for b in gateway.embed_batch_calls for t in b if "document number" in t]) == 7


def test_resume_discarded_when_staged_rows_no_longer_match(env: _Env) -> None:
    env.build(_MANY)
    with pytest.raises(InferenceUnavailableError):
        env.reindex(_Dies(2, embed_dim=16), batch_size=2)
    with env.engine.begin() as conn:  # a staged chunk's text changes meanwhile
        conn.execute(text("UPDATE chunks SET text = 'edited text' WHERE id = 'chk_01'"))
    gateway = FakeInferenceGateway(embed_dim=16)
    result = env.reindex(gateway, batch_size=2)
    assert result.resumed_rows == 0
    assert result.chunks == 7 and env.vector.table.count_rows() == 7


def test_resume_survives_second_interruption_and_swap_phase_failure(env: _Env, monkeypatch) -> None:
    env.build(_MANY)
    with pytest.raises(InferenceUnavailableError):
        env.reindex(_Dies(1, embed_dim=16), batch_size=2)
    with pytest.raises(InferenceUnavailableError):
        env.reindex(_Dies(1, embed_dim=16), batch_size=2)  # resumes 2 rows, embeds 2 more, dies
    state = __import__("json").loads(_scratch(env).with_name("lancedb-reindex.json").read_text())
    assert state["tables"]["chunks__reindex"]["rows"] == 4

    def boom(*a, **k):
        raise RuntimeError("fts write failed")

    monkeypatch.setattr(reindex_module, "_rewrite_fts", boom)
    gateway = FakeInferenceGateway(embed_dim=16)
    with pytest.raises(RuntimeError):
        env.reindex(gateway, batch_size=2)
    monkeypatch.undo()
    gateway2 = FakeInferenceGateway(embed_dim=16)
    result = env.reindex(gateway2, batch_size=2)
    assert result.resumed_rows == 8 and result.chunks == 7  # 7 chunks + 1 page, nothing left to embed
    assert [t for b in gateway2.embed_batch_calls for t in b if "document number" in t] == []
