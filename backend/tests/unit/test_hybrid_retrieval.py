"""Unit tests for `docket.retrieval.hybrid` (RRF fusion + FTS5/vector search
orchestration)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from docket.core.db.engine import get_session_factory
from docket.core.db.models import (
    AuthorizedSource,
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    Source,
    SourceStatus,
    Workspace,
)
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.index.visual_index import LancePageIndexWriter, PageRecord
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.retrieval.hybrid import (
    RankedChunk,
    _filter_active_and_current_versions,
    _sanitize_fts_query,
    fts_search,
    hybrid_search,
    reciprocal_rank_fusion,
    vector_search,
    visual_search,
)


def _insert_metadata(
    engine: Engine,
    records: list[ChunkRecord],
    *,
    status: SourceStatus = SourceStatus.ACTIVE,
    is_current: bool = True,
) -> None:
    """Insert the `Source`/`EvidenceVersion`/`EvidenceUnit`/`ChunkRecipe`/
    `Chunk` ORM rows that `hybrid_search`'s M3 status/`is_current` join
    (`fts_search`'s SQL join, `vector_search`'s post-filter) reads from.

    Pre-M3, `fts_search`/`vector_search` only ever touched the `fts_chunks`
    FTS5 table / the LanceDB table directly, so this module's fixtures
    (`_populate_fts`/`_populate_vector`) never needed to populate the real
    `chunks`/`sources`/`evidence_versions` tables (unlike
    `test_query_service.py`'s `built` fixture, which always has -- it builds
    the real ORM chain for `EvidenceResolver`'s sake). Now that
    `hybrid_search` joins through to them for every query, this fixture has
    to populate them too, or every chunk here would look like it belongs to
    a nonexistent (therefore unfilterable-as-active) source. One call
    handles every (source_id, evidence_version_id) pair present in
    `records`, defaulting to an ACTIVE, current source -- the status the
    vast majority of this file's pre-existing tests need to keep passing;
    the new revoked/non-current tests below pass `status`/`is_current`
    explicitly.
    """
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        seen_sources: set[str] = set()
        seen_versions: set[str] = set()
        seen_recipes: set[str] = set()
        seen_units: set[str] = set()
        for record in records:
            if record.source_id not in seen_sources:
                seen_sources.add(record.source_id)
                workspace = Workspace(id=f"ws_{record.source_id}", name=f"ws-{record.source_id}")
                session.merge(workspace)
                auth = AuthorizedSource(
                    id=f"auth_{record.source_id}", workspace_id=workspace.id, scope_path="/tmp"
                )
                session.merge(auth)
                session.merge(
                    Source(
                        id=record.source_id,
                        workspace_id=workspace.id,
                        authorized_source_id=auth.id,
                        source_type="local_folder",
                        path=f"/tmp/{record.source_id}",
                        status=status,
                    )
                )
            if record.evidence_version_id not in seen_versions:
                seen_versions.add(record.evidence_version_id)
                session.merge(
                    EvidenceVersion(
                        id=record.evidence_version_id,
                        source_id=record.source_id,
                        content_hash=f"hash_{record.evidence_version_id}",
                        byte_size=1,
                        parser_name="test",
                        parser_version="1",
                        is_current=is_current,
                    )
                )
            if record.chunk_recipe_id not in seen_recipes:
                seen_recipes.add(record.chunk_recipe_id)
                session.merge(
                    ChunkRecipe(
                        id=record.chunk_recipe_id,
                        chunk_size=100,
                        overlap=0,
                        splitter="test",
                        parser_name="test",
                        parser_version="1",
                    )
                )
            if record.evidence_unit_id not in seen_units:
                seen_units.add(record.evidence_unit_id)
                session.merge(
                    EvidenceUnit(
                        id=record.evidence_unit_id,
                        evidence_version_id=record.evidence_version_id,
                        unit_index=record.ordinal,
                        heading=record.heading,
                        content_hash=record.content_hash,
                    )
                )
            session.merge(
                Chunk(
                    id=record.chunk_id,
                    source_id=record.source_id,
                    evidence_version_id=record.evidence_version_id,
                    evidence_unit_id=record.evidence_unit_id,
                    chunk_recipe_id=record.chunk_recipe_id,
                    ordinal=record.ordinal,
                    heading=record.heading,
                    text=record.text,
                    content_hash=record.content_hash,
                    page_start=record.page_start,
                    page_end=record.page_end,
                )
            )
        session.commit()


def _set_source_status(engine: Engine, source_id: str, status: SourceStatus) -> None:
    """Flip an already-inserted source's status in place -- used by the
    reactivation test to mirror `SourceManager.deactivate_source`/the eval
    runner's `_set_status` (revoke, then restore to ACTIVE) without needing
    the full `SourceManager`."""
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        source = session.get(Source, source_id)
        assert source is not None
        source.status = status
        session.commit()


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
    writer.upsert(records, embeddings=None)
    _insert_metadata(engine, records)
    return writer


def _populate_vector(tmp_path: Path, engine: Engine, gateway: FakeInferenceGateway):
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
    _insert_metadata(engine, records)
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


def test_vector_search_returns_chunk_embedded_with_same_query_text(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    table = _populate_vector(tmp_path, migrated_sqlite_engine, gateway)

    # Query with the exact same text as one of the indexed chunks -- with the
    # deterministic FakeInferenceGateway this must be the nearest neighbor.
    query_text = _DOCS[1][1]
    results = vector_search(table, migrated_sqlite_engine, gateway, query_text, top_k=1)
    assert results == ["chk_beta"]


def test_hybrid_search_end_to_end_returns_expected_top_result(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    _populate_fts(migrated_sqlite_engine)
    table = _populate_vector(tmp_path, migrated_sqlite_engine, gateway)

    query = _DOCS[2][1]  # obviously about RRF -- both lexical and vector should agree
    fused = hybrid_search(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, query=query, top_k=3
    )

    assert len(fused) > 0
    assert fused[0].chunk_id == "chk_gamma"


def test_hybrid_search_respects_top_k(migrated_sqlite_engine: Engine, tmp_path: Path) -> None:
    gateway = FakeInferenceGateway()
    _populate_fts(migrated_sqlite_engine)
    table = _populate_vector(tmp_path, migrated_sqlite_engine, gateway)

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
    records = [
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
    ]
    writer.upsert(records, embeddings=None)
    _insert_metadata(migrated_sqlite_engine, records)

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
    records = [
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
    ]
    writer.upsert(records, embeddings=None)
    _insert_metadata(migrated_sqlite_engine, records)

    results = fts_search(migrated_sqlite_engine, "What does NFR-003 require?", top_k=8)
    assert "chk_nfr003" in results


def test_fts_search_finds_journey_stem_variant_via_porter_stemming(
    migrated_sqlite_engine: Engine,
) -> None:
    """`fts_chunks` is migrated to `tokenize='porter unicode61 ...'`
    (0003_fts_porter_stemming) -- a query for the singular "journey" should
    still find a chunk that only contains the plural "journeys"."""
    writer = FtsIndexWriter(migrated_sqlite_engine)
    records = [
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
    ]
    writer.upsert(records, embeddings=None)
    _insert_metadata(migrated_sqlite_engine, records)

    results = fts_search(migrated_sqlite_engine, "user journey", top_k=8)
    assert "chk_journeys" in results


# ---------------------------------------------------------------------------
# Ranking: fts_search orders by bm25(), best match first.
# ---------------------------------------------------------------------------


def test_fts_search_ranks_stronger_match_first_via_bm25(migrated_sqlite_engine: Engine) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    records = [
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
    ]
    writer.upsert(records, embeddings=None)
    _insert_metadata(migrated_sqlite_engine, records)

    results = fts_search(migrated_sqlite_engine, "fusion", top_k=8)
    assert results.index("chk_strong") < results.index("chk_weak")


# ---------------------------------------------------------------------------
# M3 -- revoked sources / non-current evidence versions must never surface,
# even when they're the single best lexical or semantic match.
# ---------------------------------------------------------------------------


def test_fts_search_excludes_revoked_source_chunk_even_as_best_lexical_match(
    migrated_sqlite_engine: Engine,
) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    records = [
        ChunkRecord(
            chunk_id="chk_revoked",
            source_id="src_revoked",
            evidence_version_id="ev_revoked",
            evidence_unit_id="eu_revoked",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            # "zephyrion" repeated -- the strongest possible bm25 match --
            # so if the revoked filter weren't airtight, this chunk would
            # win the ranking outright.
            text="zephyrion zephyrion zephyrion zephyrion",
            content_hash="hash_revoked",
        ),
        ChunkRecord(
            chunk_id="chk_active",
            source_id="src_active",
            evidence_version_id="ev_active",
            evidence_unit_id="eu_active",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text="zephyrion appears once here, among other unrelated words.",
            content_hash="hash_active",
        ),
    ]
    writer.upsert(records, embeddings=None)
    _insert_metadata(migrated_sqlite_engine, [records[0]], status=SourceStatus.REVOKED)
    _insert_metadata(migrated_sqlite_engine, [records[1]], status=SourceStatus.ACTIVE)

    results = fts_search(migrated_sqlite_engine, "zephyrion", top_k=8)

    assert "chk_revoked" not in results
    assert "chk_active" in results


def test_vector_search_excludes_revoked_source_chunk_even_as_best_semantic_match(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    writer = LanceIndexWriter(tmp_path / "lancedb")
    revoked_text = "The quarterly compliance audit findings are summarized here."
    records = [
        ChunkRecord(
            chunk_id="chk_revoked",
            source_id="src_revoked",
            evidence_version_id="ev_revoked",
            evidence_unit_id="eu_revoked",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text=revoked_text,
            content_hash="hash_revoked",
        ),
        ChunkRecord(
            chunk_id="chk_active",
            source_id="src_active",
            evidence_version_id="ev_active",
            evidence_unit_id="eu_active",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text="Unrelated content about garden irrigation schedules.",
            content_hash="hash_active",
        ),
    ]
    embeddings = [gateway.embed(r.text) for r in records]
    writer.upsert(records, embeddings=embeddings)
    _insert_metadata(migrated_sqlite_engine, [records[0]], status=SourceStatus.REVOKED)
    _insert_metadata(migrated_sqlite_engine, [records[1]], status=SourceStatus.ACTIVE)
    table = writer._open_table()

    # Query with the exact revoked chunk's own text -- with the deterministic
    # FakeInferenceGateway this is a guaranteed distance-0 nearest neighbor,
    # so it would win top_k=1 outright if the filter weren't applied.
    results = vector_search(table, migrated_sqlite_engine, gateway, revoked_text, top_k=2)

    assert "chk_revoked" not in results


def test_hybrid_search_excludes_revoked_source_chunk_from_fused_result(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    revoked_text = "zephyrion zephyrion zephyrion -- the quarterly compliance findings."
    active_text = "Unrelated content about garden irrigation schedules."
    records = [
        ChunkRecord(
            chunk_id="chk_revoked",
            source_id="src_revoked",
            evidence_version_id="ev_revoked",
            evidence_unit_id="eu_revoked",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text=revoked_text,
            content_hash="hash_revoked",
        ),
        ChunkRecord(
            chunk_id="chk_active",
            source_id="src_active",
            evidence_version_id="ev_active",
            evidence_unit_id="eu_active",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text=active_text,
            content_hash="hash_active",
        ),
    ]

    fts_writer = FtsIndexWriter(migrated_sqlite_engine)
    fts_writer.upsert(records, embeddings=None)

    vector_writer = LanceIndexWriter(tmp_path / "lancedb")
    embeddings = [gateway.embed(r.text) for r in records]
    vector_writer.upsert(records, embeddings=embeddings)

    _insert_metadata(migrated_sqlite_engine, [records[0]], status=SourceStatus.REVOKED)
    _insert_metadata(migrated_sqlite_engine, [records[1]], status=SourceStatus.ACTIVE)
    table = vector_writer._open_table()

    # Query with the revoked chunk's own text: best possible lexical match
    # ("zephyrion" x3) AND a guaranteed distance-0 semantic match. If either
    # leg's filter leaked, RRF would still surface it.
    fused = hybrid_search(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, query=revoked_text, top_k=8
    )

    assert "chk_revoked" not in [rc.chunk_id for rc in fused]


def test_fts_search_excludes_non_current_evidence_version_chunk(
    migrated_sqlite_engine: Engine,
) -> None:
    writer = FtsIndexWriter(migrated_sqlite_engine)
    records = [
        ChunkRecord(
            chunk_id="chk_old_version",
            source_id="src_1",
            evidence_version_id="ev_old",
            evidence_unit_id="eu_old",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text="glorbnex glorbnex glorbnex -- superseded content.",
            content_hash="hash_old",
        ),
        ChunkRecord(
            chunk_id="chk_new_version",
            source_id="src_1",
            evidence_version_id="ev_new",
            evidence_unit_id="eu_new",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text="glorbnex appears once in the current re-ingested content.",
            content_hash="hash_new",
        ),
    ]
    writer.upsert(records, embeddings=None)
    # Same source (ACTIVE), but the old record's evidence version is no
    # longer current -- e.g. the file was re-ingested with new content.
    _insert_metadata(migrated_sqlite_engine, [records[0]], is_current=False)
    _insert_metadata(migrated_sqlite_engine, [records[1]], is_current=True)

    results = fts_search(migrated_sqlite_engine, "glorbnex", top_k=8)

    assert "chk_old_version" not in results
    assert "chk_new_version" in results


def test_vector_search_excludes_non_current_evidence_version_chunk(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    writer = LanceIndexWriter(tmp_path / "lancedb")
    old_text = "The superseded onboarding steps, before the process changed."
    records = [
        ChunkRecord(
            chunk_id="chk_old_version",
            source_id="src_1",
            evidence_version_id="ev_old",
            evidence_unit_id="eu_old",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text=old_text,
            content_hash="hash_old",
        ),
        ChunkRecord(
            chunk_id="chk_new_version",
            source_id="src_1",
            evidence_version_id="ev_new",
            evidence_unit_id="eu_new",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text="Unrelated current content about a different topic entirely.",
            content_hash="hash_new",
        ),
    ]
    embeddings = [gateway.embed(r.text) for r in records]
    writer.upsert(records, embeddings=embeddings)
    _insert_metadata(migrated_sqlite_engine, [records[0]], is_current=False)
    _insert_metadata(migrated_sqlite_engine, [records[1]], is_current=True)
    table = writer._open_table()

    results = vector_search(table, migrated_sqlite_engine, gateway, old_text, top_k=2)

    assert "chk_old_version" not in results


def test_reactivated_source_becomes_searchable_again(migrated_sqlite_engine: Engine) -> None:
    """`SourceStatus` supports flipping a source's status back to ACTIVE
    (this is exactly what the eval runner's `_set_status` does after a
    `setup.revoke` question: revoke for the duration of that one question,
    then restore to ACTIVE) -- a re-activated source's chunks must become
    searchable again, proving the filter is a live per-query status check,
    not a one-way/cached decision."""
    writer = FtsIndexWriter(migrated_sqlite_engine)
    records = [
        ChunkRecord(
            chunk_id="chk_flip",
            source_id="src_flip",
            evidence_version_id="ev_flip",
            evidence_unit_id="eu_flip",
            chunk_recipe_id="rcp_1",
            ordinal=0,
            heading=None,
            text="woozlebane appears only in this chunk.",
            content_hash="hash_flip",
        ),
    ]
    writer.upsert(records, embeddings=None)
    _insert_metadata(migrated_sqlite_engine, records, status=SourceStatus.REVOKED)

    assert fts_search(migrated_sqlite_engine, "woozlebane", top_k=8) == []

    _set_source_status(migrated_sqlite_engine, "src_flip", SourceStatus.ACTIVE)

    assert fts_search(migrated_sqlite_engine, "woozlebane", top_k=8) == ["chk_flip"]


# ---------------------------------------------------------------------------
# Visual retrieval checkpoint 3 -- reciprocal_rank_fusion with 3 lists,
# _filter_active_and_current_versions, visual_search, and hybrid_search's
# optional page_table leg.
# ---------------------------------------------------------------------------


def test_rrf_three_lists_fuses_correctly() -> None:
    k = 60
    list_a = ["chk_x", "chk_y"]  # chk_x rank 0, chk_y rank 1
    list_b = ["chk_y", "chk_z"]  # chk_y rank 0, chk_z rank 1
    list_c = ["chk_z", "chk_w"]  # chk_z rank 0, chk_w rank 1

    fused = reciprocal_rank_fusion([list_a, list_b, list_c], k=k)

    expected_scores = {
        "chk_x": 1.0 / (k + 0 + 1),
        "chk_y": 1.0 / (k + 1 + 1) + 1.0 / (k + 0 + 1),
        "chk_z": 1.0 / (k + 1 + 1) + 1.0 / (k + 0 + 1),
        "chk_w": 1.0 / (k + 1 + 1),
    }
    scores_by_id = {rc.chunk_id: rc.score for rc in fused}
    assert scores_by_id == expected_scores
    # chk_y/chk_z each appear in two of the three lists, so both outrank the
    # single-list-only entries (chk_x/chk_w).
    assert {rc.chunk_id for rc in fused[:2]} == {"chk_y", "chk_z"}


def test_filter_active_and_current_versions_keeps_active_current_version(
    migrated_sqlite_engine: Engine,
) -> None:
    records = [
        ChunkRecord(
            chunk_id="chk_1", source_id="src_1", evidence_version_id="ev_1",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
            text="x", content_hash="hash_1",
        )
    ]
    _insert_metadata(migrated_sqlite_engine, records)

    assert _filter_active_and_current_versions(migrated_sqlite_engine, ["ev_1"]) == ["ev_1"]


def test_filter_active_and_current_versions_drops_revoked_source(
    migrated_sqlite_engine: Engine,
) -> None:
    records = [
        ChunkRecord(
            chunk_id="chk_revoked", source_id="src_revoked", evidence_version_id="ev_revoked",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
            text="x", content_hash="hash_revoked",
        )
    ]
    _insert_metadata(migrated_sqlite_engine, records, status=SourceStatus.REVOKED)

    assert _filter_active_and_current_versions(migrated_sqlite_engine, ["ev_revoked"]) == []


def test_filter_active_and_current_versions_drops_non_current_version(
    migrated_sqlite_engine: Engine,
) -> None:
    records = [
        ChunkRecord(
            chunk_id="chk_old", source_id="src_1", evidence_version_id="ev_old",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
            text="x", content_hash="hash_old",
        )
    ]
    _insert_metadata(migrated_sqlite_engine, records, is_current=False)

    assert _filter_active_and_current_versions(migrated_sqlite_engine, ["ev_old"]) == []


def test_filter_active_and_current_versions_preserves_order_of_mixed_input(
    migrated_sqlite_engine: Engine,
) -> None:
    good = ChunkRecord(
        chunk_id="chk_good", source_id="src_good", evidence_version_id="ev_good",
        evidence_unit_id="eu_good", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
        text="x", content_hash="hash_good",
    )
    bad = ChunkRecord(
        chunk_id="chk_bad", source_id="src_bad", evidence_version_id="ev_bad",
        evidence_unit_id="eu_bad", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
        text="y", content_hash="hash_bad",
    )
    _insert_metadata(migrated_sqlite_engine, [good], status=SourceStatus.ACTIVE)
    _insert_metadata(migrated_sqlite_engine, [bad], status=SourceStatus.REVOKED)

    result = _filter_active_and_current_versions(migrated_sqlite_engine, ["ev_bad", "ev_good"])
    assert result == ["ev_good"]


def test_filter_active_and_current_versions_empty_input_returns_empty(
    migrated_sqlite_engine: Engine,
) -> None:
    assert _filter_active_and_current_versions(migrated_sqlite_engine, []) == []


def _populate_pages(
    tmp_path: Path, records: list[PageRecord], embeddings: list[list[float]]
):
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert(records, embeddings=embeddings)
    return writer.table


def test_visual_search_maps_page_to_chunk_ids_via_page_span_ordered_by_ordinal(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    chunk_records = [
        ChunkRecord(
            chunk_id="chk_p1a", source_id="src_1", evidence_version_id="ev_1",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
            text="chunk one on page 1", content_hash="hash_p1a", page_start=1, page_end=1,
        ),
        ChunkRecord(
            chunk_id="chk_p1b", source_id="src_1", evidence_version_id="ev_1",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=1, heading=None,
            text="chunk two also on page 1", content_hash="hash_p1b", page_start=1, page_end=1,
        ),
        ChunkRecord(
            chunk_id="chk_p2", source_id="src_1", evidence_version_id="ev_1",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=2, heading=None,
            text="chunk three on page 2", content_hash="hash_p2", page_start=2, page_end=2,
        ),
    ]
    _insert_metadata(migrated_sqlite_engine, chunk_records)

    description = "a diagram describing widgets on page one"
    page_table = _populate_pages(
        tmp_path,
        [PageRecord(evidence_version_id="ev_1", source_id="src_1", page_no=1, description=description)],
        [gateway.embed(description)],
    )

    results = visual_search(page_table, migrated_sqlite_engine, gateway, description, top_k=5)

    # page 1's two chunks, in ordinal order; page 2's chunk never resolved.
    assert results == ["chk_p1a", "chk_p1b"]


def test_visual_search_dedupes_chunk_spanning_multiple_retrieved_pages(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    chunk_records = [
        ChunkRecord(
            chunk_id="chk_spanning", source_id="src_1", evidence_version_id="ev_1",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
            text="a chunk spanning pages 1 and 2", content_hash="hash_span",
            page_start=1, page_end=2,
        ),
    ]
    _insert_metadata(migrated_sqlite_engine, chunk_records)

    desc_page1 = "widgets page one description"
    desc_page2 = "widgets page two description"
    page_table = _populate_pages(
        tmp_path,
        [
            PageRecord(evidence_version_id="ev_1", source_id="src_1", page_no=1, description=desc_page1),
            PageRecord(evidence_version_id="ev_1", source_id="src_1", page_no=2, description=desc_page2),
        ],
        [gateway.embed(desc_page1), gateway.embed(desc_page2)],
    )

    # Both pages rank -- the single spanning chunk must appear exactly once,
    # not twice.
    results = visual_search(page_table, migrated_sqlite_engine, gateway, "widgets", top_k=5)
    assert results == ["chk_spanning"]


def test_visual_search_excludes_revoked_source_page_at_evidence_version_level(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    chunk_records = [
        ChunkRecord(
            chunk_id="chk_revoked_page", source_id="src_revoked", evidence_version_id="ev_revoked",
            evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
            text="revoked source content", content_hash="hash_rev", page_start=1, page_end=1,
        ),
    ]
    _insert_metadata(migrated_sqlite_engine, chunk_records, status=SourceStatus.REVOKED)

    description = "a description of the revoked page"
    page_table = _populate_pages(
        tmp_path,
        [PageRecord(evidence_version_id="ev_revoked", source_id="src_revoked", page_no=1, description=description)],
        [gateway.embed(description)],
    )

    assert visual_search(page_table, migrated_sqlite_engine, gateway, description, top_k=5) == []


def test_visual_search_chunk_level_filter_catches_inconsistent_revoked_chunk(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    """Belt-and-suspenders: `visual_search` re-checks active/current at the
    chunk level (via `_filter_active_and_current`) even after the
    evidence_version-level check (`_filter_active_and_current_versions`)
    already passed -- matching the paranoia `fts_search`'s own SQL join
    already has. Builds a deliberately inconsistent fixture (a chunk whose
    own `source_id` points at a REVOKED source, while the evidence_version
    it belongs to is attached to a separate, ACTIVE source) that only the
    chunk-level re-check can catch, to prove that second check is real and
    not a no-op."""
    gateway = FakeInferenceGateway()
    session_factory = get_session_factory(migrated_sqlite_engine)
    with session_factory() as session:
        session.merge(Workspace(id="ws_1", name="ws-1"))
        session.merge(AuthorizedSource(id="auth_active", workspace_id="ws_1", scope_path="/tmp"))
        session.merge(AuthorizedSource(id="auth_revoked", workspace_id="ws_1", scope_path="/tmp"))
        session.merge(
            Source(
                id="src_active_ev", workspace_id="ws_1", authorized_source_id="auth_active",
                source_type="local_folder", path="/tmp/a", status=SourceStatus.ACTIVE,
            )
        )
        session.merge(
            Source(
                id="src_chunk_revoked", workspace_id="ws_1", authorized_source_id="auth_revoked",
                source_type="local_folder", path="/tmp/b", status=SourceStatus.REVOKED,
            )
        )
        session.merge(
            EvidenceVersion(
                id="ev_1", source_id="src_active_ev", content_hash="hash_ev", byte_size=1,
                parser_name="test", parser_version="1", is_current=True,
            )
        )
        session.merge(
            ChunkRecipe(id="rcp_1", chunk_size=100, overlap=0, splitter="test", parser_name="test", parser_version="1")
        )
        session.merge(
            EvidenceUnit(id="eu_1", evidence_version_id="ev_1", unit_index=0, heading=None, content_hash="hash_eu")
        )
        session.merge(
            Chunk(
                id="chk_inconsistent", source_id="src_chunk_revoked", evidence_version_id="ev_1",
                evidence_unit_id="eu_1", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
                text="inconsistent chunk", content_hash="hash_inconsistent",
                page_start=1, page_end=1,
            )
        )
        session.commit()

    description = "description of the page"
    page_table = _populate_pages(
        tmp_path,
        [PageRecord(evidence_version_id="ev_1", source_id="src_active_ev", page_no=1, description=description)],
        [gateway.embed(description)],
    )

    assert visual_search(page_table, migrated_sqlite_engine, gateway, description, top_k=5) == []


def test_visual_search_returns_empty_when_pages_table_not_created(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    gateway = FakeInferenceGateway()
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    assert writer.table is None  # nothing upserted yet

    assert visual_search(writer.table, migrated_sqlite_engine, gateway, "anything", top_k=5) == []


def test_hybrid_search_default_page_table_none_never_calls_visual_search(
    migrated_sqlite_engine: Engine, tmp_path: Path, monkeypatch
) -> None:
    """`page_table` defaults to `None`; when it is `None` (whether by
    omission or explicitly), `hybrid_search` must not even attempt a visual
    search -- this is the strict backward-compatibility requirement: every
    existing caller that doesn't pass `page_table` gets exactly the 2-way
    fusion this function has always done."""
    gateway = FakeInferenceGateway()
    _populate_fts(migrated_sqlite_engine)
    table = _populate_vector(tmp_path, migrated_sqlite_engine, gateway)

    def _boom(*args, **kwargs):
        raise AssertionError("visual_search must not be called when page_table is None")

    monkeypatch.setattr("docket.retrieval.hybrid.visual_search", _boom)

    query = _DOCS[2][1]
    fused_omitted = hybrid_search(engine=migrated_sqlite_engine, table=table, gateway=gateway, query=query, top_k=3)
    fused_explicit_none = hybrid_search(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, query=query, top_k=3, page_table=None
    )

    assert fused_omitted == fused_explicit_none
    assert fused_omitted[0].chunk_id == "chk_gamma"


def test_hybrid_search_with_page_table_surfaces_visual_only_chunk(
    migrated_sqlite_engine: Engine, tmp_path: Path
) -> None:
    """A chunk_id present ONLY via the visual retriever (never indexed into
    `fts_chunks` or the chunks vector table -- e.g. content only ever
    resolved from a page image) must still win a spot in the final fused
    output when `page_table` is supplied."""
    gateway = FakeInferenceGateway()
    _populate_fts(migrated_sqlite_engine)
    table = _populate_vector(tmp_path, migrated_sqlite_engine, gateway)

    visual_only_records = [
        ChunkRecord(
            chunk_id="chk_visual_only", source_id="src_visual", evidence_version_id="ev_visual",
            evidence_unit_id="eu_visual", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
            text="a chunk that only the visual retriever can find",
            content_hash="hash_visual_only", page_start=1, page_end=1,
        )
    ]
    _insert_metadata(migrated_sqlite_engine, visual_only_records)

    description = "a diagram on the page that only the visual retriever describes"
    page_table = _populate_pages(
        tmp_path,
        [PageRecord(evidence_version_id="ev_visual", source_id="src_visual", page_no=1, description=description)],
        [gateway.embed(description)],
    )

    # Confirm it's genuinely absent from both individual legs first.
    assert "chk_visual_only" not in fts_search(migrated_sqlite_engine, description, top_k=8)
    assert "chk_visual_only" not in vector_search(table, migrated_sqlite_engine, gateway, description, top_k=8)

    fused = hybrid_search(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, query=description,
        top_k=8, page_table=page_table,
    )

    assert "chk_visual_only" in [rc.chunk_id for rc in fused]
