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
    _sanitize_fts_query,
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


# ---------------------------------------------------------------------------
# _sanitize_fts_query -- pure function, no I/O, no fixtures needed.
# ---------------------------------------------------------------------------


def test_sanitize_drops_stopwords_and_quotes_content_terms() -> None:
    sanitized = _sanitize_fts_query("What are the six user journeys defined in the PRD")
    # None of "what/are/the/in" (stopwords) should survive; every surviving
    # content term must be quoted so FTS5 can never treat it as an operator.
    assert sanitized == '"six" OR "user" OR "journeys" OR "defined" OR "prd"'


def test_sanitize_is_case_insensitive() -> None:
    assert _sanitize_fts_query("What Is The Capital") == '"capital"'


def test_sanitize_bare_and_or_not_are_not_left_as_fts5_operators() -> None:
    # "and"/"or"/"not" are both common English words and FTS5 boolean
    # operators. They're stopwords here, so a question using them as plain
    # English never reaches the MATCH string as bare (unquoted, operator-
    # parseable) tokens.
    sanitized = _sanitize_fts_query("cats and dogs or not fish")
    assert "and" not in sanitized.split()
    assert " AND " not in sanitized
    assert '"cats"' in sanitized
    assert '"dogs"' in sanitized
    assert '"fish"' in sanitized
    # No bare (unquoted) occurrence of the operator words.
    for operator_word in ("and", "or", "not"):
        assert f' {operator_word} ' not in f" {sanitized} "


def test_sanitize_all_stopword_query_falls_back_to_unfiltered_terms() -> None:
    # Every token here is a stopword -- falling back to the empty string
    # would make `fts_search` short-circuit to [] (acceptable), but the
    # sanitizer itself must not blow up or silently produce a query that
    # matches everything; it falls back to the original (quoted) tokens.
    sanitized = _sanitize_fts_query("what is the")
    assert sanitized == '"what" OR "is" OR "the"'


def test_sanitize_empty_query_returns_empty_string() -> None:
    assert _sanitize_fts_query("") == ""
    assert _sanitize_fts_query("!!!---:::") == ""


def test_sanitize_quotes_protect_non_stopword_fts5_operator_words() -> None:
    # "near" is an FTS5 proximity operator but isn't in the (short,
    # deliberately non-exhaustive) stopword list -- quoting every surviving
    # term is what keeps it from being parsed as syntax, independent of the
    # stopword list's coverage.
    sanitized = _sanitize_fts_query("cats NEAR dogs")
    assert sanitized == '"cats" OR "near" OR "dogs"'
    assert " NEAR " not in sanitized.upper().replace('"NEAR"', "")


def test_sanitize_hyphenated_ids_keep_a_findable_token() -> None:
    # "J-01" and "NFR-003" split on the hyphen (same as the FTS5 unicode61
    # tokenizer splits on ingest), but each keeps at least one specific
    # surviving token ("01" / "nfr" or "003") that a query can still find.
    j01 = _sanitize_fts_query("What is J-01?")
    assert '"01"' in j01
    nfr = _sanitize_fts_query("What does NFR-003 require?")
    assert '"nfr"' in nfr and '"003"' in nfr


# ---------------------------------------------------------------------------
# Regression: the exact "six user journeys" style zero-hit failure.
# ---------------------------------------------------------------------------


def test_fts_search_finds_journeys_question_that_previously_returned_zero_rows(
    migrated_sqlite_engine: Engine,
) -> None:
    """Reproduces the measured baseline failure: the old
    `_sanitize_fts_query` ANDed every word of a natural question together
    (including "what"/"are"/"the"/"in"), so "What are the six user journeys
    defined in the PRD" matched zero rows even when a chunk plainly
    contained that phrasing. The fixed sanitizer (stopwords dropped, terms
    OR'd) must find it."""
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert(
        [
            ChunkRecord(
                chunk_id="chk_journeys",
                source_id="src_1",
                evidence_version_id="ev_1",
                evidence_unit_id="eu_1",
                chunk_recipe_id="rcp_1",
                ordinal=0,
                heading="User Journeys",
                text=(
                    "The PRD defines six user journeys covering onboarding, "
                    "search, checkout, support, renewal, and offboarding."
                ),
                content_hash="hash_journeys",
            ),
            ChunkRecord(
                chunk_id="chk_unrelated",
                source_id="src_1",
                evidence_version_id="ev_1",
                evidence_unit_id="eu_1",
                chunk_recipe_id="rcp_1",
                ordinal=1,
                heading="Pricing",
                text="Pricing tiers are Free, Pro, and Enterprise.",
                content_hash="hash_pricing",
            ),
        ],
        embeddings=None,
    )

    results = fts_search(
        migrated_sqlite_engine, "What are the six user journeys defined in the PRD", top_k=8
    )

    assert "chk_journeys" in results
    assert "chk_unrelated" not in results


def test_fts_search_handles_literal_and_or_not_without_raising(
    migrated_sqlite_engine: Engine,
) -> None:
    """A question using "and"/"or"/"not" as ordinary English words must not
    raise an FTS5 syntax error and must still find the obviously relevant
    chunk (regression guard for the pre-fix behavior where these were
    passed through as bare, operator-parseable tokens)."""
    _populate_fts(migrated_sqlite_engine)

    results = fts_search(
        migrated_sqlite_engine, "is the fox and the dog or not the plants", top_k=8
    )
    assert "chk_alpha" in results  # "fox"/"dog" chunk


def test_fts_search_finds_hyphenated_id_like_nfr_003(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert(
        [
            ChunkRecord(
                chunk_id="chk_nfr003",
                source_id="src_1",
                evidence_version_id="ev_1",
                evidence_unit_id="eu_1",
                chunk_recipe_id="rcp_1",
                ordinal=0,
                heading="Non-Functional Requirements",
                text="NFR-003: the system must respond within 200ms at p95.",
                content_hash="hash_nfr003",
            ),
        ],
        embeddings=None,
    )

    results = fts_search(migrated_sqlite_engine, "What does NFR-003 require?", top_k=8)
    assert "chk_nfr003" in results


def test_fts_search_finds_journey_stem_variant_via_porter_stemming(
    migrated_sqlite_engine: Engine,
) -> None:
    """`fts_chunks` is migrated to `tokenize='porter unicode61 ...'`
    (0003_fts_porter_stemming) -- a query for the singular "journey" should
    still find a chunk that only contains the plural "journeys"."""
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert(
        [
            ChunkRecord(
                chunk_id="chk_journeys",
                source_id="src_1",
                evidence_version_id="ev_1",
                evidence_unit_id="eu_1",
                chunk_recipe_id="rcp_1",
                ordinal=0,
                heading=None,
                text="The PRD defines six user journeys.",
                content_hash="hash_journeys",
            ),
        ],
        embeddings=None,
    )

    results = fts_search(migrated_sqlite_engine, "user journey", top_k=8)
    assert "chk_journeys" in results


# ---------------------------------------------------------------------------
# Ranking: fts_search orders by bm25(), best match first.
# ---------------------------------------------------------------------------


def test_fts_search_ranks_stronger_match_first_via_bm25(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    writer.upsert(
        [
            ChunkRecord(
                chunk_id="chk_weak",
                source_id="src_1",
                evidence_version_id="ev_1",
                evidence_unit_id="eu_1",
                chunk_recipe_id="rcp_1",
                ordinal=0,
                heading=None,
                # "fusion" appears once, buried among unrelated content.
                text="This chunk briefly mentions fusion once and otherwise "
                "discusses gardening, weather, and travel plans.",
                content_hash="hash_weak",
            ),
            ChunkRecord(
                chunk_id="chk_strong",
                source_id="src_1",
                evidence_version_id="ev_1",
                evidence_unit_id="eu_1",
                chunk_recipe_id="rcp_1",
                ordinal=1,
                heading=None,
                # "fusion" is the entire subject -- much higher term
                # frequency relative to document length, so bm25 should
                # rank it above chk_weak.
                text="fusion fusion fusion",
                content_hash="hash_strong",
            ),
        ],
        embeddings=None,
    )

    results = fts_search(migrated_sqlite_engine, "fusion", top_k=8)
    assert results.index("chk_strong") < results.index("chk_weak")
