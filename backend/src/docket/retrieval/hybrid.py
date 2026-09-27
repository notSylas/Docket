"""Hybrid retrieval: lexical (FTS5) + semantic (LanceDB) search fused via
Reciprocal Rank Fusion (RRF).

This replaces `spike/query.py`'s CLI-only, hardcoded-path version with a
library-shaped one: callers pass in an already-open SQLAlchemy `Engine`
(pointed at the `fts_chunks` FTS5 table), an already-open LanceDB `table`
(the `chunks` vector table -- see `docket.index.vector_index`), and an
`InferenceGateway` for embedding the query.

`reciprocal_rank_fusion` itself is a pure function with no I/O: it operates
on plain lists of chunk_id strings, which keeps it trivially unit-testable
independent of any real search backend. `fts_search`/`vector_search`/
`hybrid_search` are the thin I/O-touching orchestration layer built on top.

Visual retrieval checkpoint 3 adds a third, optional leg: `visual_search`
queries the `pages` LanceDB table (per-page VLM descriptions -- see
`docket.index.visual_index`) for nearest-neighbor pages, then resolves each
surviving page down to real chunk_ids via `Chunk.page_start`/`page_end`
(checkpoint 1's page provenance) before handing them to RRF, which never
operates on anything but chunk_id lists. This leg only runs when a caller
explicitly passes a `page_table` into `hybrid_search` -- gated end-to-end by
`settings.visual_index_enabled` at the wiring layer (`QueryService`/
`cli/context.py`), not in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, bindparam, text

from docket.core.config import settings
from docket.core.db.models import SourceStatus
from docket.inference.gateway import InferenceGateway

_DEFAULT_RRF_K = 60

# A short, defensible English stopword list -- articles, forms of "be",
# wh-words, and a handful of common prepositions/conjunctions. Deliberately
# NOT exhaustive: over-stripping risks discarding a term that's actually
# informative for a niche question, and the goal here is just to stop a
# natural-language question's function words from starving out its content
# words when every remaining term is OR'd together (see
# `_sanitize_fts_query`). Includes "and"/"or"/"not" -- these double as FTS5
# boolean operators, so dropping them (on top of quoting every surviving
# term below) means a question that happens to contain them as plain English
# words never risks being parsed as FTS5 syntax.
_FTS_STOPWORDS = frozenset(
    {
        "a", "an", "the",
        "is", "are", "was", "were", "be", "been", "being",
        "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
        "of", "to", "in", "on", "at", "by", "for", "with", "from", "as", "into",
        "and", "or", "not",
        "do", "does", "did",
        "this", "that", "these", "those",
    }
)

_WORD_RE = re.compile(r"\w+")


@dataclass(frozen=True)
class RankedChunk:
    chunk_id: str
    score: float  # the fused RRF score


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]], k: int = _DEFAULT_RRF_K
) -> list[RankedChunk]:
    """Fuse multiple ranked lists of chunk_ids via Reciprocal Rank Fusion.

    score(chunk_id) = sum over lists containing it of 1 / (k + rank + 1),
    where `rank` is the chunk_id's 0-based position in that list. A chunk_id
    appearing in only one list still gets a (smaller) score; chunk_ids not
    present in any list don't appear in the output. Pure function -- no I/O,
    no dependency on any search backend -- so it's fast and easy to unit test
    with hand-crafted rank lists.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    ranked_chunks = [RankedChunk(chunk_id=cid, score=score) for cid, score in scores.items()]
    ranked_chunks.sort(key=lambda rc: rc.score, reverse=True)
    return ranked_chunks


def _sanitize_fts_query(query: str) -> str:
    """Turn a natural-language question into a safe FTS5 MATCH query.

    The naive approach (AND-ing every alphanumeric token together) fails
    badly on real questions: function words like "what"/"the"/"is" are
    ANDed in right alongside the content words, so a question like "What
    are the six user journeys defined in the PRD" requires "what" AND
    "are" AND "the" AND ... AND "journeys" AND ... to all appear in one
    chunk -- which usually returns zero rows, silently collapsing hybrid
    retrieval to vector-only (this was measured: 11/11 classifiable
    failures in the first accuracy-milestone baseline were retrieval
    misses). Bare `and`/`or`/`not` are also FTS5 boolean operators, so an
    unquoted question containing them as ordinary English words risked a
    syntax error or a mis-parsed query.

    This version: lowercases, tokenizes on `\\w+` (matching the ingest-side
    tokenizer's own word-splitting, including on ids like "J-01" ->
    "j"/"01"), drops a short stopword list (`_FTS_STOPWORDS`, which
    includes and/or/not), double-quotes each remaining term (a quoted FTS5
    phrase can never be parsed as an operator, belt-and-suspenders even
    for terms the stopword list doesn't catch), and ORs them together --
    any one content word matching is enough to surface a candidate chunk,
    with the vector side and RRF fusion in `hybrid_search` supplying the
    precision. If every token is a stopword (or the query is empty/query
    is all punctuation), falls back to the unfiltered token list rather
    than emit an empty MATCH query (which FTS5 would reject) or silently
    return nothing.
    """
    words = _WORD_RE.findall(query.lower())
    terms = [w for w in words if w not in _FTS_STOPWORDS]
    if not terms:
        terms = words
    return " OR ".join(f'"{term}"' for term in terms)


# M3 -- revoked/current-version correctness: a source can be revoked (its
# `Source.status` flips away from ACTIVE via `SourceManager.deactivate_source`)
# and re-ingesting a changed file supersedes an old `EvidenceVersion`
# (`is_current` flips to False). Neither leg of hybrid search is allowed to
# surface a chunk from a non-ACTIVE source or a non-current evidence version,
# regardless of how well it ranks lexically or semantically -- the CLI tells
# the user a revoked source's "evidence is no longer searched", and that has
# to actually be true. `fts_chunks` has no `source_id`/`evidence_version_id`
# columns of its own (see `docket.index.fts_index`'s module docstring), but it
# lives in the same SQLite database as the `chunks`/`sources`/
# `evidence_versions` ORM tables (both are reached through the same `Engine`),
# so the FTS query below joins straight through to them. `SourceStatus.ACTIVE`
# is intentionally the only status treated as searchable (not e.g. MISSING --
# see the M3 plan) and its `.name` ("ACTIVE") is what SQLAlchemy's `Enum` type
# actually persists in the `sources.status` column (verified against a real
# migrated DB), not `.value` ("active").
_FTS_SEARCH_SQL = text(
    "SELECT fts_chunks.chunk_id FROM fts_chunks "
    "JOIN chunks ON chunks.id = fts_chunks.chunk_id "
    "JOIN sources ON sources.id = chunks.source_id "
    "JOIN evidence_versions ON evidence_versions.id = chunks.evidence_version_id "
    "WHERE fts_chunks MATCH :query "
    "AND sources.status = :active_status "
    "AND evidence_versions.is_current = :is_current "
    "ORDER BY bm25(fts_chunks) LIMIT :limit"
)


def fts_search(engine: Engine, query: str, top_k: int) -> list[str]:
    """Run an FTS5 MATCH query against `fts_chunks`, returning chunk_ids
    ranked by `bm25()` (best/lowest-scoring match first).

    Joins through to `chunks`/`sources`/`evidence_versions` so a revoked
    source's chunks or a superseded evidence version's chunks are excluded
    *before* `ORDER BY .. LIMIT ..` runs -- they never occupy one of the
    `top_k` slots that an active, current chunk could otherwise take (see
    the module-level comment above `_FTS_SEARCH_SQL`).
    """
    sanitized = _sanitize_fts_query(query).strip()
    if not sanitized:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            _FTS_SEARCH_SQL,
            {
                "query": sanitized,
                "limit": top_k,
                "active_status": SourceStatus.ACTIVE.name,
                "is_current": True,
            },
        )
        return [row[0] for row in rows]


_ALLOWED_CHUNK_IDS_SQL = text(
    "SELECT chunks.id FROM chunks "
    "JOIN sources ON sources.id = chunks.source_id "
    "JOIN evidence_versions ON evidence_versions.id = chunks.evidence_version_id "
    "WHERE chunks.id IN :chunk_ids "
    "AND sources.status = :active_status "
    "AND evidence_versions.is_current = :is_current"
).bindparams(bindparam("chunk_ids", expanding=True))


def _filter_active_and_current(engine: Engine, chunk_ids: list[str]) -> list[str]:
    """Filter `chunk_ids` (in-place order preserved) down to those whose
    source is ACTIVE and whose evidence version is current.

    The LanceDB vector table has no `evidence_version_id`/status columns of
    its own (see `docket.index.vector_index`'s schema note -- it only carries
    `chunk_id`/`source_id`/`text`/`vector`), so unlike `fts_search` (which can
    join and filter inside the SQL query itself), vector search's result has
    to be post-filtered against the real `chunks`/`sources`/`evidence_versions`
    tables through `engine` after the nearest-neighbor search runs. This can
    return fewer than `top_k` results when a revoked/superseded chunk would
    otherwise have ranked in the top `top_k` -- correct filtering matters more
    here than backfilling the slot it leaves (M5's candidate-pool widening is
    the place to do that, not this fix).
    """
    if not chunk_ids:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            _ALLOWED_CHUNK_IDS_SQL,
            {
                "chunk_ids": chunk_ids,
                "active_status": SourceStatus.ACTIVE.name,
                "is_current": True,
            },
        )
        allowed = {row[0] for row in rows}
    return [chunk_id for chunk_id in chunk_ids if chunk_id in allowed]


def vector_search(
    table: Any, engine: Engine, gateway: InferenceGateway, query: str, top_k: int
) -> list[str]:
    """Embed `query` via `gateway` and run a nearest-neighbor search against
    `table` (a LanceDB `chunks` table), returning chunk_ids ranked by vector
    similarity (best match first), filtered to active/current chunks via
    `engine` (see `_filter_active_and_current`)."""
    query_vector = gateway.embed(query)
    results = table.search(query_vector).limit(top_k).to_list()
    chunk_ids = [row["chunk_id"] for row in results]
    return _filter_active_and_current(engine, chunk_ids)


_ALLOWED_EVIDENCE_VERSION_IDS_SQL = text(
    "SELECT evidence_versions.id FROM evidence_versions "
    "JOIN sources ON sources.id = evidence_versions.source_id "
    "WHERE evidence_versions.id IN :evidence_version_ids "
    "AND sources.status = :active_status "
    "AND evidence_versions.is_current = :is_current"
).bindparams(bindparam("evidence_version_ids", expanding=True))


def _filter_active_and_current_versions(
    engine: Engine, evidence_version_ids: list[str]
) -> list[str]:
    """Filter `evidence_version_ids` (order preserved) down to those whose
    source is ACTIVE and which are themselves the current evidence version --
    the same non-negotiable invariant `_filter_active_and_current` enforces
    for chunk_ids, applied here directly to evidence_version_ids.

    `visual_search`'s page results come back keyed by
    `(evidence_version_id, page_no)`, not chunk_id -- there's no chunk to
    filter yet at that point, so a revoked/superseded evidence_version_id
    has to be dropped here, *before* it's ever used to look up chunks (a
    superseded version's chunks would otherwise be found and returned as if
    they were current, defeating the point of filtering at all).
    """
    if not evidence_version_ids:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            _ALLOWED_EVIDENCE_VERSION_IDS_SQL,
            {
                "evidence_version_ids": evidence_version_ids,
                "active_status": SourceStatus.ACTIVE.name,
                "is_current": True,
            },
        )
        allowed = {row[0] for row in rows}
    return [ev_id for ev_id in evidence_version_ids if ev_id in allowed]


# A page maps to zero, one, or several chunks -- whichever chunks'
# page_start/page_end span (inclusive) covers that page number. `page_start
# IS NOT NULL` excludes pre-checkpoint-1 chunks (page_start/page_end both
# NULL) rather than matching them spuriously; `ORDER BY ordinal` returns them
# in document order, which is also the natural "best first" order for a
# single page's chunks (there's no independent per-chunk visual-relevance
# score to rank by).
_CHUNKS_FOR_PAGE_SQL = text(
    "SELECT chunks.id FROM chunks "
    "WHERE chunks.evidence_version_id = :evidence_version_id "
    "AND chunks.page_start IS NOT NULL "
    "AND chunks.page_start <= :page_no "
    "AND chunks.page_end >= :page_no "
    "ORDER BY chunks.ordinal"
)


def visual_search(
    page_table: Any, engine: Engine, gateway: InferenceGateway, query: str, top_k: int
) -> list[str]:
    """Embed `query` via `gateway` and run a nearest-neighbor search against
    `page_table` (a LanceDB `pages` table -- see `docket.index.visual_index`),
    resolving each surviving page down to real chunk_ids and returning them
    ranked best-page-first (a page's own chunks, when it has several, are
    ordered by `ordinal` within that page -- there's no finer-grained score).

    `page_table` may be `None` (the `pages` table hasn't been created yet,
    e.g. `visual_index_enabled` was just turned on but nothing has been
    re-ingested with it since) -- returns `[]` gracefully in that case,
    mirroring `LanceIndexWriter`/`LancePageIndexWriter`'s own "table not
    found" -> `None` behavior rather than raising.

    Filtering happens twice, matching the paranoia level `fts_search`'s SQL
    join already has: first at the evidence_version level (via
    `_filter_active_and_current_versions`), *before* any page is resolved to
    chunks -- a revoked/superseded evidence_version_id must never be used to
    look up chunks at all -- then again at the chunk level (via
    `_filter_active_and_current`) on the resolved chunk_ids themselves,
    belt-and-suspenders in case a chunk row's own status has since diverged
    from its evidence_version's.

    Chunk_ids are deduped across pages (a chunk spanning two visually
    retrieved pages would otherwise appear twice), preserving first-seen
    (best) rank order -- `reciprocal_rank_fusion` scores a ranked list by
    position, so a duplicate would double-count that chunk's rank contribution
    if left in.
    """
    if page_table is None:
        return []

    query_vector = gateway.embed(query)
    results = page_table.search(query_vector).limit(top_k).to_list()
    pairs = [(row["evidence_version_id"], row["page_no"]) for row in results]
    if not pairs:
        return []

    allowed_versions = set(
        _filter_active_and_current_versions(engine, [ev_id for ev_id, _ in pairs])
    )
    surviving_pairs = [pair for pair in pairs if pair[0] in allowed_versions]
    if not surviving_pairs:
        return []

    resolved: list[str] = []
    with engine.connect() as conn:
        for evidence_version_id, page_no in surviving_pairs:
            rows = conn.execute(
                _CHUNKS_FOR_PAGE_SQL,
                {"evidence_version_id": evidence_version_id, "page_no": page_no},
            )
            resolved.extend(row[0] for row in rows)

    seen: set[str] = set()
    deduped: list[str] = []
    for chunk_id in resolved:
        if chunk_id not in seen:
            seen.add(chunk_id)
            deduped.append(chunk_id)

    return _filter_active_and_current(engine, deduped)


def hybrid_search(
    *,
    engine: Engine,
    table: Any,
    gateway: InferenceGateway,
    query: str,
    top_k: int = settings.default_top_k,
    page_table: Any | None = None,
) -> list[RankedChunk]:
    """Run lexical and semantic search (each requesting `top_k` results) and
    fuse them via Reciprocal Rank Fusion, returning the top `top_k` fused
    results.

    Both legs already exclude revoked-source/non-current-version chunks
    (`fts_search`'s SQL join, `vector_search`'s post-filter), so the fused
    result inherits that guarantee for free -- RRF only ever combines
    chunk_ids that were present in one of the input lists.

    `page_table` is optional and defaults to `None`, in which case this is
    exactly the 2-way (FTS + vector) fusion this function has always done --
    `visual_search` isn't even called, so passing nothing here is
    byte-for-byte identical to this checkpoint never having existed. When a
    `page_table` (the `pages` LanceDB table) is given, `visual_search` also
    runs against it and its resolved chunk_ids are fused in as a third
    ranked list, so a chunk_id that only the visual retriever surfaced can
    still win a spot in the final result."""
    fts_ranked = fts_search(engine, query, top_k)
    vector_ranked = vector_search(table, engine, gateway, query, top_k)
    ranked_lists = [fts_ranked, vector_ranked]
    if page_table is not None:
        visual_ranked = visual_search(page_table, engine, gateway, query, top_k)
        ranked_lists.append(visual_ranked)
    fused = reciprocal_rank_fusion(ranked_lists)
    return fused[:top_k]
