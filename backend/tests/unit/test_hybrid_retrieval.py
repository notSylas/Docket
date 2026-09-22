"""Unit tests for `docket.retrieval.hybrid` (RRF fusion + FTS5/vector search
orchestration)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from docket.index.base import ChunkRecord
from docket.index.fts_index import FtsIndexWriter
from docket.index.vector_index import LanceIndexWriter
from docket.inference.gateway import FakeInferenceGateway
from docket.retrieval.hybrid import (
    RankedChunk,
    fts_search,
    hybrid_search,
    reciprocal_rank_fusion,
    vector_search,
)


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion -- pure function, no I/O, no fixtures needed.
# ---------------------------------------------------------------------------


def test_rrf_overlapping_chunk_scores_higher_than_single_list_tops() -> None:
    # list_a ranks: chk_x (0), chk_y (1)
    # list_b ranks: chk_y (0), chk_z (1)
    # chk_y appears in both lists (ranks 1 and 0) so its fused score should
    # exceed either list's rank-0-only entry (chk_x or chk_z).
    k = 60
    list_a = ["chk_x", "chk_y"]
    list_b = ["chk_y", "chk_z"]

    fused = reciprocal_rank_fusion([list_a, list_b], k=k)

    expected_scores = {
        "chk_x": 1.0 / (k + 0 + 1),
        "chk_y": 1.0 / (k + 1 + 1) + 1.0 / (k + 0 + 1),
        "chk_z": 1.0 / (k + 1 + 1),
    }
    scores_by_id = {rc.chunk_id: rc.score for rc in fused}
    assert scores_by_id == expected_scores

    # chk_y (in both lists) ranks above both single-list-only entries.
    assert fused[0].chunk_id == "chk_y"
    assert [rc.chunk_id for rc in fused] == ["chk_y", "chk_x", "chk_z"]


def test_rrf_chunk_in_only_one_list_still_appears_with_nonzero_score() -> None:
    fused = reciprocal_rank_fusion([["chk_a", "chk_b"], []])
    ids = [rc.chunk_id for rc in fused]
    assert "chk_a" in ids
    assert "chk_b" in ids
    assert all(rc.score > 0 for rc in fused)


def test_rrf_empty_input_lists_returns_empty_output() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_rrf_larger_k_flattens_score_differences() -> None:
    ranked = ["chk_a", "chk_b", "chk_c"]

    small_k = reciprocal_rank_fusion([ranked], k=1)
    large_k = reciprocal_rank_fusion([ranked], k=1000)

    small_k_scores = {rc.chunk_id: rc.score for rc in small_k}
    large_k_scores = {rc.chunk_id: rc.score for rc in large_k}

    small_k_spread = small_k_scores["chk_a"] - small_k_scores["chk_c"]
    large_k_spread = large_k_scores["chk_a"] - large_k_scores["chk_c"]

    assert small_k_spread > large_k_spread
    # Order is preserved either way.
    assert [rc.chunk_id for rc in small_k] == ranked
    assert [rc.chunk_id for rc in large_k] == ranked


def test_rrf_returns_ranked_chunk_dataclass_instances() -> None:
    fused = reciprocal_rank_fusion([["chk_a"]])
    assert len(fused) == 1
    assert isinstance(fused[0], RankedChunk)
    assert fused[0].chunk_id == "chk_a"


# ---------------------------------------------------------------------------
# fts_search / vector_search / hybrid_search -- small real-backend fixture.
# ---------------------------------------------------------------------------

_DOCS = [
    ("chk_alpha", "The quick brown fox jumps over the lazy dog."),
    ("chk_beta", "Photosynthesis converts sunlight into chemical energy in plants."),
    ("chk_gamma", "The Reciprocal Rank Fusion algorithm combines ranked search results."),
]


def _populate_fts(engine: Engine) -> FtsIndexWriter:
    writer = FtsIndexWriter(engine)
    writer.upsert(
        [
            ChunkRecord(
                chunk_id=chunk_id,
                source_id="src_1",
                evidence_version_id="ev_1",
                evidence_unit_id="eu_1",
                chunk_recipe_id="rcp_1",
                ordinal=i,
                heading=None,
                text=text,
                content_hash="hash_" + chunk_id,
            )
            for i, (chunk_id, text) in enumerate(_DOCS)
        ],
        embeddings=None,
    )
    return writer


def _populate_vector(tmp_path: Path, gateway: FakeInferenceGateway):
    writer = LanceIndexWriter(tmp_path / "lancedb")
    records = [
        ChunkRecord(
            chunk_id=chunk_id,
            source_id="src_1",
            evidence_version_id="ev_1",
            evidence_unit_id="eu_1",
            chunk_recipe_id="rcp_1",
            ordinal=i,
            heading=None,
            text=text,
            content_hash="hash_" + chunk_id,
        )
        for i, (chunk_id, text) in enumerate(_DOCS)
    ]
    embeddings = [gateway.embed(text) for _, text in _DOCS]
    writer.upsert(records, embeddings=embeddings)
    return writer._open_table()


def test_fts_search_finds_chunk_by_exact_term(migrated_sqlite_engine: Engine) -> None:
    _populate_fts(migrated_sqlite_engine)
    results = fts_search(migrated_sqlite_engine, "photosynthesis", top_k=8)
    assert "chk_beta" in results


def test_fts_search_sanitizes_special_characters_without_raising(
    migrated_sqlite_engine: Engine,
) -> None:
    _populate_fts(migrated_sqlite_engine)
    # quotes, colons, dashes are FTS5-syntax-significant and would raise on
    # a raw MATCH query.
    results = fts_search(migrated_sqlite_engine, 'fox: "quick"-brown?!', top_k=8)
    assert "chk_alpha" in results


def test_fts_search_empty_after_sanitization_returns_empty_list(
    migrated_sqlite_engine: Engine,
) -> None:
    _populate_fts(migrated_sqlite_engine)
    assert fts_search(migrated_sqlite_engine, "!!!---:::", top_k=8) == []


def test_vector_search_returns_chunk_embedded_with_same_query_text(tmp_path: Path) -> None:
    gateway = FakeInferenceGateway()
    table = _populate_vector(tmp_path, gateway)

    # Query with the exact same text as one of the indexed chunks -- with the
    # deterministic FakeInferenceGateway this must be the nearest neighbor.
    query_text = _DOCS[1][1]
    results = vector_search(table, gateway, query_text, top_k=1)
    assert results == ["chk_beta"]


def test_hybrid_search_end_to_end_returns_expected_top_result(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    _populate_fts(migrated_sqlite_engine)
    table = _populate_vector(tmp_path, gateway)

    query = _DOCS[2][1]  # obviously about RRF -- both lexical and vector should agree
    fused = hybrid_search(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, query=query, top_k=3
    )

    assert len(fused) > 0
    assert fused[0].chunk_id == "chk_gamma"


def test_hybrid_search_respects_top_k(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    gateway = FakeInferenceGateway()
    _populate_fts(migrated_sqlite_engine)
    table = _populate_vector(tmp_path, gateway)

    fused = hybrid_search(
        engine=migrated_sqlite_engine,
        table=table,
        gateway=gateway,
        query="fox dog photosynthesis fusion",
        top_k=2,
    )
    assert len(fused) <= 2
