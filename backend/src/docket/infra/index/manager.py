"""`IndexManager` -- the single entrypoint the rest of the app should use to
keep the FTS5 and LanceDB indexes in sync, replacing the validation spike's
"re-embed and re-index the whole corpus on every run" behavior with true
incremental upsert/delete.

Design note (continues the one in `docket.infra.index.fts_index`): `FtsIndexWriter`
cannot resolve "which chunk_ids belong to source X" on its own (the FTS5
table has no `source_id` column), so `IndexManager` resolves that via the
vector writer -- whose table *does* carry `source_id` -- and then drives both
writers off the resolved chunk_id set. This keeps the "what belongs to a
source" logic in exactly one place instead of duplicated per-backend joins.
"""

from __future__ import annotations

from typing import Sequence

from docket.infra.index.base import ChunkRecord, IndexWriteStats, IndexWriter
from docket.infra.inference.gateway import InferenceGateway


class IndexManager:
    def __init__(
        self, fts_writer: IndexWriter, vector_writer: IndexWriter, gateway: InferenceGateway
    ):
        self._fts = fts_writer
        self._vector = vector_writer
        self._gateway = gateway

    def upsert_chunks(self, records: Sequence[ChunkRecord]) -> IndexWriteStats:
        """Upsert `records` into both indexes, embedding only what's missing
        from either index (the fix for the spike's re-embed-everything
        behavior: unchanged chunks already present in both indexes are
        skipped entirely -- no embed call, no write)."""
        records = list(records)
        if not records:
            return IndexWriteStats(upserted=0, skipped_unchanged=0, deleted=0)

        chunk_ids = [r.chunk_id for r in records]
        in_fts = self._fts.existing_chunk_ids(chunk_ids)
        in_vector = self._vector.existing_chunk_ids(chunk_ids)
        fully_indexed = in_fts & in_vector

        to_write = [r for r in records if r.chunk_id not in fully_indexed]
        skipped = len(records) - len(to_write)

        if not to_write:
            return IndexWriteStats(upserted=0, skipped_unchanged=skipped, deleted=0)

        embeddings = [self._gateway.embed(r.text) for r in to_write]

        self._fts.upsert(to_write, embeddings)
        self._vector.upsert(to_write, embeddings)

        return IndexWriteStats(upserted=len(to_write), skipped_unchanged=skipped, deleted=0)

    def reconcile_source(self, source_id: str, current_chunk_ids: set[str]) -> IndexWriteStats:
        """Delete any chunk currently indexed for `source_id` that is no
        longer present in `current_chunk_ids` (e.g. content removed/changed
        on re-ingestion)."""
        indexed_chunk_ids = self._vector.chunk_ids_for_source(source_id)
        stale = indexed_chunk_ids - current_chunk_ids
        if not stale:
            return IndexWriteStats(upserted=0, skipped_unchanged=0, deleted=0)

        self._fts.delete(stale)
        self._vector.delete(stale)

        return IndexWriteStats(upserted=0, skipped_unchanged=0, deleted=len(stale))

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        """Remove exactly `chunk_ids` from both indexes (a no-op for ids
        that were never indexed)."""
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return
        self._fts.delete(chunk_ids)
        self._vector.delete(chunk_ids)

    def delete_source(self, source_id: str) -> None:
        """Remove all indexed chunks for `source_id` from both indexes."""
        chunk_ids = self._vector.chunk_ids_for_source(source_id)
        if chunk_ids:
            self._fts.delete(chunk_ids)
        self._vector.delete_by_source(source_id)
