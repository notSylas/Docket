"""Chunk context prefix (design 04 §8): the shared `build_index_text`, what the
index manager embeds / FTS-indexes versus what stays verbatim, and the
manifest's `index_text_version`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from docket.infra.index.base import ChunkRecord
from docket.infra.index.context import INDEX_TEXT_VERSION, build_index_text, index_text_for_chunk
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.manifest import (
    IndexManifest,
    IndexManifestGuard,
    new_manifest,
    read_manifest,
    write_manifest,
)
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway


def test_build_index_text_file_and_full_path() -> None:
    assert (
        build_index_text("report.docx", ["Intro", "Scope"], "body")
        == "report.docx > Intro > Scope\nbody"
    )


def test_build_index_text_no_heading_is_file_only() -> None:
    assert build_index_text("report.docx", [], "body") == "report.docx\nbody"


def test_build_index_text_basename_only() -> None:
    posix = build_index_text("/home/alice/Secret Dir/report.docx", [], "b")
    windows = build_index_text("C:\\Users\\bob\\report.docx", [], "b")
    assert posix == "report.docx\nb"
    assert windows == "report.docx\nb"
    assert "alice" not in posix and "bob" not in windows


def test_build_index_text_nothing_to_prefix_is_verbatim() -> None:
    assert build_index_text(None, [], "body") == "body"
    assert build_index_text("", ["", "  "], "body") == "body"


def test_build_index_text_skips_blank_segments_and_keeps_special_chars() -> None:
    out = build_index_text("Q3 (final) – résumé.xlsx", ["", "R&D > \"Costs\"", " "], "x\ny")
    assert out == 'Q3 (final) – résumé.xlsx > R&D > "Costs"\nx\ny'


def test_index_text_for_chunk_prefers_heading_path_then_heading() -> None:
    loc = json.dumps({"heading_path": ["A", "B"]})
    assert index_text_for_chunk("/d/f.docx", loc, "B", "t") == "f.docx > A > B\nt"
    # No heading_path (XLSX sheet / PPTX slide): fall back to the leaf label.
    assert index_text_for_chunk("/d/f.xlsx", None, "Sheet1", "t") == "f.xlsx > Sheet1\nt"
    assert index_text_for_chunk("/d/f.pptx", '{"slide": 2}', "Slide 2", "t") == (
        "f.pptx > Slide 2\nt"
    )
    assert index_text_for_chunk("/d/f.docx", "not json", None, "t") == "f.docx\nt"
    assert index_text_for_chunk(None, None, None, "t") == "t"


def _record(chunk_id: str, text: str, index_text: str | None = None) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id="src_1",
        evidence_version_id="ev_1",
        evidence_unit_id="eu_1",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text=text,
        content_hash="hash_" + chunk_id,
        index_text=index_text,
    )


def _fts_match(engine: Engine, query: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH ?", (query,)
        )
        return {r[0] for r in rows}


def test_upsert_embeds_and_fts_indexes_prefixed_text_but_lancedb_text_is_verbatim(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    manager = IndexManager(FtsIndexWriter(migrated_sqlite_engine), vector, gateway)
    prefixed = "handbook.docx > Zanzibar > Pay\nwages are weekly"

    manager.upsert_chunks([_record("chk_a", "wages are weekly", prefixed)])

    assert gateway.embed_calls == [prefixed]
    row = vector._open_table().search().to_list()[0]
    assert row["text"] == "wages are weekly"
    assert list(row["vector"]) == pytest.approx(gateway.embed(prefixed), abs=1e-6)
    with migrated_sqlite_engine.connect() as conn:
        fts_text = conn.exec_driver_sql("SELECT text FROM fts_chunks").scalar()
    assert fts_text == prefixed


def test_keyword_only_in_breadcrumb_matches_via_fts_now_and_not_without_prefix(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager = IndexManager(
        FtsIndexWriter(migrated_sqlite_engine),
        LanceIndexWriter(tmp_path / "lancedb"),
        FakeInferenceGateway(),
    )
    manager.upsert_chunks(
        [
            _record("with_prefix", "wages are weekly", "handbook.docx > Zanzibar\nwages are weekly"),
            _record("without_prefix", "wages are weekly"),  # the old behavior
        ]
    )
    assert _fts_match(migrated_sqlite_engine, "zanzibar") == {"with_prefix"}
    assert _fts_match(migrated_sqlite_engine, "handbook") == {"with_prefix"}


def test_no_index_text_means_same_as_text(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    gateway = FakeInferenceGateway()
    manager = IndexManager(
        FtsIndexWriter(migrated_sqlite_engine), LanceIndexWriter(tmp_path / "lancedb"), gateway
    )
    manager.upsert_chunks([_record("chk_a", "plain body")])
    assert gateway.embed_calls == ["plain body"]
    assert _fts_match(migrated_sqlite_engine, "plain") == {"chk_a"}


# --- manifest -------------------------------------------------------------


def test_new_manifest_records_index_text_version(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    write_manifest(path, new_manifest("m1", 32))
    assert read_manifest(path).index_text_version == INDEX_TEXT_VERSION == 2


def test_legacy_manifest_without_field_reads_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    legacy = {
        "schema_version": 1,
        "embed_model": "m1",
        "embed_dimension": 32,
        "embed_instruction": None,
        "tokenizer": None,
        "created_at": "t",
        "updated_at": "t",
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    manifest = read_manifest(path)
    assert isinstance(manifest, IndexManifest)
    assert manifest.index_text_version == 0


def _guarded(engine: Engine, tmp_path: Path):
    vector = LanceIndexWriter(tmp_path / "lancedb", engine=engine)
    path = tmp_path / "index_manifest.json"
    guard = IndexManifestGuard(path, "m1", lambda: [vector.vector_dimension()])
    manager = IndexManager(
        FtsIndexWriter(engine), vector, FakeInferenceGateway(), manifest_guard=guard
    )
    return manager, path


def test_first_write_records_version_and_ordinary_writes_do_not_bump_old_manifest(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    manager, path = _guarded(migrated_sqlite_engine, tmp_path)
    manager.upsert_chunks([_record("chk_a", "alpha")])
    assert read_manifest(path).index_text_version == INDEX_TEXT_VERSION == 2

    old = read_manifest(path)
    write_manifest(path, IndexManifest(**{**old.__dict__, "index_text_version": 0}))
    manager.upsert_chunks([_record("chk_b", "beta")])  # a difference is not an error
    assert read_manifest(path).index_text_version == 0


def test_legacy_adoption_records_zero(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    legacy, _ = _guarded(migrated_sqlite_engine, tmp_path)
    legacy._manifest_guard = None  # written before manifests existed
    legacy.upsert_chunks([_record("chk_a", "alpha")])

    manager, path = _guarded(migrated_sqlite_engine, tmp_path)
    manager.upsert_chunks([_record("chk_b", "beta")])
    assert read_manifest(path).index_text_version == 0


# --- doc 05 step 3: Context line ------------------------------------------


def test_context_line_rendered_after_heading_line() -> None:
    loc = json.dumps({"sheet": "S", "context": ["FY2025-26", "fiscal year 2025 2026"]})
    assert index_text_for_chunk("/d/R.xlsx", loc, "S", "row") == (
        "R.xlsx > S\nContext: FY2025-26; fiscal year 2025 2026\nrow"
    )


def test_context_absent_or_malformed_leaves_text_unchanged() -> None:
    assert index_text_for_chunk("/d/R.xlsx", json.dumps({"sheet": "S"}), "S", "t") == "R.xlsx > S\nt"
    assert index_text_for_chunk("/d/R.xlsx", json.dumps({"context": "x"}), "S", "t") == "R.xlsx > S\nt"
    assert index_text_for_chunk("/d/R.xlsx", json.dumps({"context": []}), "S", "t") == "R.xlsx > S\nt"
    assert index_text_for_chunk("/d/f.docx", json.dumps({"heading_path": ["A"]}), None, "t") == (
        "f.docx > A\nt"
    )


def test_context_line_has_hard_cap() -> None:
    loc = json.dumps({"context": ["x" * 1000]})
    line = index_text_for_chunk("/d/R.xlsx", loc, "S", "t").split("\n")[1]
    assert len(line) == len("Context: ") + 400


def test_fts_matches_bare_year_token_from_context(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    manager = IndexManager(
        FtsIndexWriter(migrated_sqlite_engine),
        LanceIndexWriter(tmp_path / "lancedb"),
        FakeInferenceGateway(),
    )
    loc = json.dumps({"sheet": "S", "context": ["FY2025-26", "fiscal year 2025 2026", "July"]})
    body = "Sheet: S | Row: 7\nMonth: Jul"
    manager.upsert_chunks(
        [
            _record("ctx", body, index_text_for_chunk("/d/Revenue-FY2025-26.xlsx", loc, "S", body)),
            _record("plain", body, index_text_for_chunk("/d/Revenue-FY2025-26.xlsx", None, "S", body)),
        ]
    )
    assert _fts_match(migrated_sqlite_engine, "2025") == {"ctx"}
    assert _fts_match(migrated_sqlite_engine, "july") == {"ctx"}
