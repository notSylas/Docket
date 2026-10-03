"""`ChunkWriter` -- persists parsed content as `EvidenceUnit`/`Chunk` rows and
answers the two lookups `IngestionPipeline` needs around that: the id of a
source+file's *current* `EvidenceVersion` (used for the before/after
unchanged-vs-changed snapshot -- see `docket.services.ingestion.pipeline`'s module
docstring, point 1) and the full set of chunk_ids currently reachable from a
source's current versions (used for the single end-of-run reconcile pass --
see that same docstring, point 2). Extracted from `IngestionPipeline` (Phase
3 of the restructuring) purely to shrink that file and make this slice of
persistence logic independently testable; no behavior changed in the move.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from docket.core.db.identity import compute_chunk_id
from docket.core.db.models import (
    Chunk,
    ChunkRecipe as ChunkRecipeRow,
    EvidenceUnit,
    EvidenceVersion,
    VersionStatus,
)
from docket.infra.index.base import ChunkRecord
from docket.infra.parsing.recipes import ChunkRecipe


class ChunkWriter:
    def __init__(self, *, session_factory: sessionmaker, chunk_recipe: ChunkRecipe) -> None:
        self._session_factory = session_factory
        self._chunk_recipe = chunk_recipe

    def ensure_recipe_row(self) -> None:
        """Get-or-create the `ChunkRecipe` row for `self._chunk_recipe.id`."""
        with self._session_factory() as session:
            existing = session.get(ChunkRecipeRow, self._chunk_recipe.id)
            if existing is not None:
                return
            session.add(
                ChunkRecipeRow(
                    id=self._chunk_recipe.id,
                    chunk_size=self._chunk_recipe.chunk_size,
                    overlap=self._chunk_recipe.overlap,
                    splitter=self._chunk_recipe.splitter,
                    parser_name=self._chunk_recipe.parser_name,
                    parser_version=self._chunk_recipe.parser_version,
                )
            )
            session.commit()

    def current_version_id(self, source_id: str, file_path: str) -> str | None:
        # Scoped by (source_id, file_path): a "local_folder" source can hold
        # many files, each with its own independent version lineage -- see
        # `EvidenceManager._current_version` and migration 0002's docstring
        # for why `source_id` alone isn't enough to identify "the" current
        # version once a source has more than one file.
        #
        # "Current" = latest lineage slot (status != SUPERSEDED), matching
        # `EvidenceManager._current_version`'s own posture -- this is the
        # before/after snapshot `IngestionPipeline` compares its post-ingest
        # id against, so it has to use the same notion of "current" or the
        # unchanged-vs-changed detection breaks. A PENDING or FAILED row is
        # still the current lineage slot even though it isn't servable yet.
        with self._session_factory() as session:
            stmt = select(EvidenceVersion.id).where(
                EvidenceVersion.source_id == source_id,
                EvidenceVersion.status != VersionStatus.SUPERSEDED,
                EvidenceVersion.file_path == file_path,
            )
            return session.execute(stmt).scalar_one_or_none()

    def chunk_ids_for_version(self, evidence_version_id: str) -> list[str]:
        with self._session_factory() as session:
            stmt = select(Chunk.id).where(Chunk.evidence_version_id == evidence_version_id)
            return list(session.execute(stmt).scalars().all())

    def delete_units_and_chunks(self, evidence_version_id: str) -> None:
        """Remove one version's `Chunk` and `EvidenceUnit` rows in a single
        transaction (chunks first: they reference units). Used before a
        retry rebuilds them -- chunk ids are deterministic, so leftover rows
        from a failed attempt would collide on `chunks.id`, and unit ids are
        random, so they would otherwise be duplicated. Blob references are
        untouched: none are keyed by chunk/unit rows (they belong to the
        version's content and page images)."""
        with self._session_factory() as session:
            session.execute(delete(Chunk).where(Chunk.evidence_version_id == evidence_version_id))
            session.execute(
                delete(EvidenceUnit).where(EvidenceUnit.evidence_version_id == evidence_version_id)
            )
            session.commit()

    def persist_units_and_chunks(
        self, *, source_id: str, evidence_version_id: str, units, chunks
    ) -> list[ChunkRecord]:
        with self._session_factory() as session:
            unit_objs = [
                EvidenceUnit(
                    evidence_version_id=evidence_version_id,
                    unit_index=u.unit_index,
                    heading=u.heading,
                    content_hash=u.content_hash,
                    unit_kind=u.unit_kind,
                    locator_json=u.locator_json,
                )
                for u in units
            ]
            session.add_all(unit_objs)
            session.flush()
            unit_id_by_index = {
                u.unit_index: obj.id for u, obj in zip(units, unit_objs)
            }

            chunk_objs = []
            for c in chunks:
                chunk_id = compute_chunk_id(
                    evidence_version_id, self._chunk_recipe.id, c.ordinal, c.content_hash
                )
                chunk_objs.append(
                    Chunk(
                        id=chunk_id,
                        source_id=source_id,
                        evidence_version_id=evidence_version_id,
                        evidence_unit_id=unit_id_by_index[c.evidence_unit_index],
                        chunk_recipe_id=self._chunk_recipe.id,
                        ordinal=c.ordinal,
                        heading=c.heading,
                        text=c.text,
                        content_hash=c.content_hash,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        provenance=c.provenance,
                    )
                )
            session.add_all(chunk_objs)
            session.commit()

            return [
                ChunkRecord(
                    chunk_id=c.id,
                    source_id=c.source_id,
                    evidence_version_id=c.evidence_version_id,
                    evidence_unit_id=c.evidence_unit_id,
                    chunk_recipe_id=c.chunk_recipe_id,
                    ordinal=c.ordinal,
                    heading=c.heading,
                    text=c.text,
                    content_hash=c.content_hash,
                    page_start=c.page_start,
                    page_end=c.page_end,
                )
                for c in chunk_objs
            ]

    def current_chunk_ids_for_source(self, source_id: str) -> set[str]:
        """All chunk_ids reachable from `source_id`'s *servable* (`READY`)
        EvidenceVersions -- the "should still be indexed" set used for the
        single end-of-run reconcile pass.

        Deliberately `status == READY` here, not just "not superseded": a
        version whose chunking succeeded but whose index upsert then raised
        ends up `FAILED` with real `Chunk` rows already committed (chunk
        persistence commits before the index upsert that can fail) -- those
        orphaned rows must not be treated as "desired" index state, or
        reconcile would leave stale/partial index entries for a version
        nothing should be able to retrieve. A `PENDING`/`FAILED` version
        never legitimately owns chunks that *should* be indexed, so this is
        equivalent to "not superseded" in the common case and strictly safer
        in that one.
        """
        with self._session_factory() as session:
            stmt = (
                select(Chunk.id)
                .join(EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id)
                .where(
                    EvidenceVersion.source_id == source_id,
                    EvidenceVersion.status == VersionStatus.READY,
                )
            )
            return set(session.execute(stmt).scalars().all())
