"""Shared types and the `IndexWriter` protocol for Docket's index backends.

Both the lexical index (SQLite FTS5, see `docket.infra.index.fts_index`) and the
vector index (LanceDB, see `docket.infra.index.vector_index`) implement
`IndexWriter`. `docket.infra.index.manager.IndexManager` orchestrates both writers
plus embedding via an `InferenceGateway` so callers never talk to either
backend directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ChunkRecord:
    """The subset of a `Chunk` ORM row that the index backends need.

    Deliberately decoupled from the `Chunk` ORM model so index writers don't
    depend on SQLAlchemy mapped instances -- callers (a later checkpoint's
    ingestion pipeline) build these from `Chunk` rows or `ChunkDraft`s.
    """

    chunk_id: str
    source_id: str
    evidence_version_id: str
    evidence_unit_id: str
    chunk_recipe_id: str
    ordinal: int
    heading: str | None
    text: str
    content_hash: str
    # 1-indexed page numbers this chunk spans, or None when no page marker
    # was ever matched before/within it (see docket.infra.parsing.chunker). Not
    # consumed by any index backend yet (checkpoint 1 of the visual
    # retrieval plan) -- carried here so a later checkpoint can map a page
    # image back to the chunk_ids on that page without a DB round trip.
    page_start: int | None = None
    page_end: int | None = None


@dataclass
class IndexWriteStats:
    upserted: int
    skipped_unchanged: int
    deleted: int


class IndexWriter(Protocol):
    """Interface implemented by each concrete index backend (FTS5, LanceDB).

    `IndexManager` is the only caller that should depend on this directly;
    everything else in the app talks to `IndexManager`.
    """

    def upsert(
        self, records: Sequence[ChunkRecord], embeddings: Sequence[list[float]] | None
    ) -> None:
        """Insert or update `records` (keyed by `chunk_id`). `embeddings`,
        when required by the backend (vector index), must be the same length
        as `records` and in the same order; backends that don't need vectors
        (FTS5) accept and ignore it."""
        ...

    def delete(self, chunk_ids: Sequence[str]) -> None:
        """Remove rows for the given `chunk_ids`. Missing ids are ignored."""
        ...

    def delete_by_source(self, source_id: str) -> None:
        """Remove all rows belonging to `source_id`."""
        ...

    def delete_by_version(self, evidence_version_id: str) -> None:
        """Remove all rows belonging to `evidence_version_id`."""
        ...

    def existing_chunk_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        """Return the subset of `chunk_ids` already present in this index."""
        ...

    def chunk_ids_for_source(self, source_id: str) -> set[str]:
        """Return all chunk_ids currently indexed for `source_id`."""
        ...
