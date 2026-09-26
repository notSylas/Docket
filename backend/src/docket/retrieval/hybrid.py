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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

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


def fts_search(engine: Engine, query: str, top_k: int) -> list[str]:
    """Run an FTS5 MATCH query against `fts_chunks`, returning chunk_ids
    ranked by `bm25()` (best/lowest-scoring match first)."""
    sanitized = _sanitize_fts_query(query).strip()
    if not sanitized:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH :query "
                "ORDER BY bm25(fts_chunks) LIMIT :limit"
            ),
            {"query": sanitized, "limit": top_k},
        )
        return [row[0] for row in rows]


def vector_search(table: Any, gateway: InferenceGateway, query: str, top_k: int) -> list[str]:
    """Embed `query` via `gateway` and run a nearest-neighbor search against
    `table` (a LanceDB `chunks` table), returning chunk_ids ranked by vector
    similarity (best match first)."""
    query_vector = gateway.embed(query)
    results = table.search(query_vector).limit(top_k).to_list()
    return [row["chunk_id"] for row in results]


def hybrid_search(
    *,
    engine: Engine,
    table: Any,
    gateway: InferenceGateway,
    query: str,
    top_k: int = 8,
) -> list[RankedChunk]:
    """Run lexical and semantic search (each requesting `top_k` results) and
    fuse them via Reciprocal Rank Fusion, returning the top `top_k` fused
    results."""
    fts_ranked = fts_search(engine, query, top_k)
    vector_ranked = vector_search(table, gateway, query, top_k)
    fused = reciprocal_rank_fusion([fts_ranked, vector_ranked])
    return fused[:top_k]
