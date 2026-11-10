"""`IndexManager` -- the single entrypoint the rest of the app should use to
keep the FTS5 and LanceDB indexes in sync, replacing the validation spike's
"re-embed and re-index the whole corpus on every run" behavior with true
incremental upsert/delete.

Design note (continues the one in `docket.infra.index.fts_index`): both
writers can enumerate rows by source and by evidence version (FTS rows carry
`source_id`/`evidence_version_id`, as do the LanceDB rows), so reconcile finds
stale entries in either index independently -- an FTS-only orphan is found
without the vector table. The optional visual `pages` writer is cleaned up
alongside, keyed by version.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Sequence

from docket.infra.index.base import ChunkRecord, IndexWriteStats, IndexWriter
from docket.infra.index.manifest import IndexManifestGuard
from docket.infra.index.visual_index import LancePageIndexWriter
from docket.infra.inference.gateway import InferenceGateway, embed_texts

logger = logging.getLogger(__name__)


class IndexManager:
    def __init__(
        self,
        fts_writer: IndexWriter,
        vector_writer: IndexWriter,
        gateway: InferenceGateway,
        page_writer: LancePageIndexWriter | None = None,
        manifest_guard: IndexManifestGuard | None = None,
        embed_batch_size: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._fts = fts_writer
        self._vector = vector_writer
        self._gateway = gateway
        self._pages = page_writer
        # `None` skips the embedding-space check (see `index.manifest`).
        self._manifest_guard = manifest_guard
        self._embed_batch_size = embed_batch_size
        self._sleep = sleep
        self._files_since_maintain = 0

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

        embeddings = embed_texts(
            self._gateway,
            [r.index_text or r.text for r in to_write],
            batch_size=self._embed_batch_size,
            sleep=self._sleep,
        )
        # After embedding (the dimension is only known now), before any write.
        if self._manifest_guard is not None:
            self._manifest_guard.check_write(len(embeddings[0]))

        self._fts.upsert(to_write, embeddings)
        self._vector.upsert(to_write, embeddings)

        return IndexWriteStats(upserted=len(to_write), skipped_unchanged=skipped, deleted=0)

    def upsert_cloned_chunks(
        self, records: Sequence[ChunkRecord], donors: dict[str, ChunkRecord]
    ) -> IndexWriteStats:
        """Index `records` that are copies of already-indexed `donors`
        (`{new chunk_id: donor record}`). A copy whose index text equals its
        donor's reuses the donor's stored vector (no embed call); the rest are
        embedded as usual. Falls back to a plain `upsert_chunks` when the
        vector writer cannot return stored vectors."""
        records = list(records)
        vectors_for = getattr(self._vector, "vectors_for", None)
        if not records or vectors_for is None:
            return self.upsert_chunks(records)
        same = [
            r for r in records
            if (r.index_text or r.text) == (donors[r.chunk_id].index_text or donors[r.chunk_id].text)
        ]
        stored = vectors_for([donors[r.chunk_id].chunk_id for r in same])
        reuse = {r.chunk_id: stored[donors[r.chunk_id].chunk_id] for r in same if donors[r.chunk_id].chunk_id in stored}
        to_embed = [r for r in records if r.chunk_id not in reuse]
        fresh: dict[str, list[float]] = {}
        if to_embed:
            vecs = embed_texts(
                self._gateway, [r.index_text or r.text for r in to_embed],
                batch_size=self._embed_batch_size, sleep=self._sleep,
            )
            fresh = {r.chunk_id: v for r, v in zip(to_embed, vecs)}
        embeddings = [reuse.get(r.chunk_id) or fresh[r.chunk_id] for r in records]
        if self._manifest_guard is not None:
            self._manifest_guard.check_write(len(embeddings[0]))
        self._fts.upsert(records, embeddings)
        self._vector.upsert(records, embeddings)
        return IndexWriteStats(upserted=len(records), skipped_unchanged=0, deleted=0)

    def reconcile_source(
        self,
        source_id: str,
        current_chunk_ids: set[str],
        current_version_ids: set[str] | None = None,
    ) -> IndexWriteStats:
        """Delete any chunk currently indexed for `source_id` (in either
        index) that is no longer present in `current_chunk_ids` (e.g. content
        removed/changed on re-ingestion). When `current_version_ids` is
        given, also delete `pages` rows of any other version of the source."""
        indexed_chunk_ids = self._fts.chunk_ids_for_source(
            source_id
        ) | self._vector.chunk_ids_for_source(source_id)
        stale = indexed_chunk_ids - current_chunk_ids
        if stale:
            self._fts.delete(stale)
            self._vector.delete(stale)

        if self._pages is not None and current_version_ids is not None:
            for version_id in self._pages.version_ids_for_source(source_id) - current_version_ids:
                self._pages.delete_by_version(version_id)

        return IndexWriteStats(upserted=0, skipped_unchanged=0, deleted=len(stale))

    def note_file_done(self) -> None:
        """Called by the pipeline after each file; runs `maintain()` every
        `DOCKET_COMPACT_EVERY_FILES` files (0 disables) to bound Lance
        fragment/version growth mid-run. Caller holds the writer lock."""
        from docket.infra.index import tuning

        every = tuning.compact_every_files()
        if every <= 0:
            return
        self._files_since_maintain += 1
        if self._files_since_maintain >= every:
            self._files_since_maintain = 0
            self.maintain(keep_hours=min(tuning.compact_keep_hours(), tuning.MID_RUN_KEEP_HOURS))

    def maintain(self, **kwargs) -> dict | None:
        """Post-ingest ANN index build/optimize + compaction of the vector
        table. Never raises (a failed optimization must not fail an ingest:
        search stays correct, just slower). Caller holds the writer lock."""
        maintain = getattr(self._vector, "maintain", None)
        if maintain is None:
            return None
        try:
            path = self._manifest_guard.path if self._manifest_guard is not None else None
            return maintain(manifest_path=path, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("vector index maintenance failed", exc_info=True)
            return None

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        """Remove exactly `chunk_ids` from both indexes (a no-op for ids
        that were never indexed)."""
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return
        self._fts.delete(chunk_ids)
        self._vector.delete(chunk_ids)

    def delete_version(self, evidence_version_id: str) -> None:
        """Remove every index entry (FTS, vector, visual pages) owned by one
        evidence version, e.g. when it has been superseded."""
        self._fts.delete_by_version(evidence_version_id)
        self._vector.delete_by_version(evidence_version_id)
        if self._pages is not None:
            self._pages.delete_by_version(evidence_version_id)

    def delete_source(self, source_id: str) -> None:
        """Remove all indexed chunks and pages for `source_id`."""
        self._fts.delete_by_source(source_id)
        self._vector.delete_by_source(source_id)
        if self._pages is not None:
            self._pages.delete_by_source(source_id)
