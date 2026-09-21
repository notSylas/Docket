"""Hybrid retrieval: lexical (FTS5) + semantic (LanceDB) search fused via
Reciprocal Rank Fusion (RRF).

This replaces `spike/query.py`'s CLI-only, hardcoded-path version with a
library-shaped one: callers pass in an already-open SQLAlchemy `Engine`
(pointed at the `fts_chunks` FTS5 table), an already-open LanceDB `table`
(the `chunks` vector table -- see `attest.index.vector_index`), and an
`InferenceGateway` for embedding the query.

`reciprocal_rank_fusion` itself is a pure function with no I/O: it operates
on plain lists of chunk_id strings, which keeps it trivially unit-testable
independent of any real search backend. `fts_search`/`vector_search`/
`hybrid_search` are the thin I/O-touching orchestration layer built on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from attest.inference.gateway import InferenceGateway

_DEFAULT_RRF_K = 60


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
    """Strip characters that aren't alphanumeric/whitespace before building
    an FTS5 MATCH query.

    Raw FTS5 query syntax treats characters like `"`, `-`, `:`, `(`, `)`, `*`
    specially and can raise a syntax error on arbitrary user input (e.g. a
    question containing a colon or a quote). Reducing the query to its
    alphanumeric tokens avoids that entirely, at the cost of losing FTS5's
    phrase/boolean operators -- an acceptable tradeoff for this use case
    (searching with a natural-language question, not a crafted query).
    Same approach as `spike/query.py`'s `fts_search`.
    """
    return "".join(c if c.isalnum() or c.isspace() else " " for c in query)


def fts_search(engine: Engine, query: str, top_k: int) -> list[str]:
    """Run an FTS5 MATCH query against `fts_chunks`, returning chunk_ids
    ranked by FTS5's own `rank` (best match first)."""
    sanitized = _sanitize_fts_query(query).strip()
    if not sanitized:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH :query ORDER BY rank LIMIT :limit"),
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
