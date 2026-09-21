"""`IngestionPipeline` -- the incremental, per-source ingestion orchestrator.

Ties together, for one ``Source``: walking its folder, `EvidenceManager`
(store bytes + version tracking), `DoclingParser` (parse to markdown),
`chunk_document` (split into units/chunks), the ORM (persist `EvidenceUnit`/
`Chunk` rows), and `IndexManager` (embed + index). This replaces
`spike/ingest.py`'s "drop everything and rebuild from scratch every run"
behavior with a truly incremental run: unchanged files are detected and skipped
before any parsing/chunking/embedding happens, and one bad file never aborts
the rest of the batch.

Two design decisions worth calling out explicitly (both requested by the
CP8 brief to be documented in code, not just in a report):

1. Unchanged-vs-changed detection. `EvidenceManager.ingest_file` returns an
   `EvidenceVersion` in both the "new/changed content" case (a freshly
   inserted row) and the "unchanged content" case (the existing current
   row, untouched) -- the return value alone doesn't say which happened.
   Rather than re-deriving the file's content hash ourselves (a second file
   read + hash, duplicating `EvidenceManager`'s own work), we snapshot the
   id of the source's *current* `EvidenceVersion` immediately before calling
   `ingest_file`, then compare it to the id of the version `ingest_file`
   returns: same id => unchanged (no-op), different id (or no prior current
   version) => new/changed. This is a single cheap indexed lookup
   (`evidence_versions.source_id`), not a re-read of the file.

   Deviation worth flagging: `EvidenceManager`'s "current version" lookup, as
   originally written, was scoped only by `source_id` -- correct for a
   source that maps to one file, but wrong for a multi-file "local_folder"
   source (this checkpoint's whole premise): ingesting file B under the same
   source_id would flip file A's still-current, still-unchanged version to
   `is_current=False` (only one row can be "the" current version per
   source_id), so re-ingesting file A afterwards would look like a false
   "changed" every time -- breaking the exact idempotency this pipeline
   needs to prove. Fixed at the source: `EvidenceVersion` gained a nullable
   `file_path` column (migration `0002_evidence_version_file_path`), and
   `EvidenceManager._current_version`/`ingest_file` now scope "current" by
   (source_id, file_path). This pipeline's own before/after snapshot
   (`_current_version_id`) is scoped the same way for the same reason.

2. Reconciliation timing: once per source, at the end of the run, NOT
   per-file. `IndexManager.reconcile_source(source_id, current_chunk_ids)`
   treats `current_chunk_ids` as the *complete* desired chunk_id set for the
   *entire* source (it diffs against `chunk_ids_for_source`, which has no
   per-file granularity -- see `attest.index.vector_index`). Calling it
   per-file with only that one file's chunk_ids would therefore delete every
   *other* file's already-indexed chunks on each file's turn -- actively
   wrong, not just less efficient. So this pipeline processes every file
   first (each wrapped in its own try/except), then does exactly one
   reconcile pass over the union of all chunk_ids currently reachable from
   the source's *current* `EvidenceVersion`s (queried straight from the
   `chunks`/`evidence_versions` tables, so it naturally includes unchanged
   files' untouched chunks and excludes stale chunks from now-superseded
   versions). The tradeoff: if the batch is interrupted mid-run, this final
   reconcile never happens and the index can keep serving some now-stale
   chunks until the next successful run. That's accepted deliberately --
   never wrongly deleting a sibling file's good data is more important than
   always-freshest cleanup, and a subsequent run repairs it regardless.

   One knock-on consequence: if a previously-ingested file's content changes
   to something Docling can no longer parse, its `EvidenceVersion` still
   advances to the new (unparseable) content -- `EvidenceManager.ingest_file`
   stores raw bytes unconditionally, before parsing is attempted -- but no
   new `Chunk` rows get written for it. Because reconciliation only keeps
   chunks reachable from each source's *current* version, that file's
   previously-good chunks then fall out of the "current" set and get
   reconciled away. We keep this behavior rather than special-casing it:
   once content has demonstrably changed, continuing to serve chunks derived
   from the old content would misrepresent what the file currently contains.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from attest.db.identity import compute_chunk_id
from attest.db.models import (
    Chunk,
    ChunkRecipe as ChunkRecipeRow,
    EvidenceUnit,
    EvidenceVersion,
    IngestionJob,
    IngestionJobStatus,
    Source,
    SourceStatus,
)
from attest.evidence.manager import EvidenceManager
from attest.index.base import ChunkRecord
from attest.index.manager import IndexManager
from attest.parsing.chunker import chunk_document
from attest.parsing.docling_wrapper import DoclingParser
from attest.parsing.recipes import ChunkRecipe
from attest.sources.manager import SourceNotFoundError

# Deliberately narrow, spike-validated set. Broadening this to more of
# Docling's supported formats is a one-line change (add to the set); each
# addition just needs its own real-file smoke test, which is out of scope
# for this checkpoint.
SUPPORTED_EXTENSIONS = {".docx", ".pdf"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceNotActiveError(Exception):
    """Raised when `run_ingestion_for_source` is asked to ingest a source
    that isn't ACTIVE (e.g. revoked). No `IngestionJob` row is created for a
    call we refuse outright -- there's nothing to report a job status for."""

    def __init__(self, source_id: str, status: SourceStatus):
        self.source_id = source_id
        self.status = status
        super().__init__(
            f"source {source_id} is not active (status={status.value}); refusing to ingest"
        )


@dataclass
class FileIngestResult:
    path: Path
    status: str  # "ingested" | "unchanged" | "failed"
    error: str | None = None
    chunks_written: int = 0


@dataclass
class IngestionJobResult:
    source_id: str
    job_id: str
    status: str  # matches IngestionJobStatus
    files_processed: int
    files_failed: int
    file_results: list[FileIngestResult] = field(default_factory=list)


class IngestionPipeline:
    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        evidence_manager: EvidenceManager,
        parser: DoclingParser,
        index_manager: IndexManager,
        chunk_recipe: ChunkRecipe,
    ) -> None:
        self._session_factory = session_factory
        self._evidence_manager = evidence_manager
        self._parser = parser
        self._index_manager = index_manager
        self._chunk_recipe = chunk_recipe

    # -- internal helpers ---------------------------------------------------

    def _load_active_source(self, source_id: str) -> Source:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status != SourceStatus.ACTIVE:
                raise SourceNotActiveError(source_id, source.status)
            return source

    def _ensure_chunk_recipe_row(self) -> None:
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

    def _discover_files(self, root: Path) -> list[Path]:
        """Recursively walk `root` for ingestable files. Recursive (not just
        the top level) because a "local_folder" source is meant to cover the
        whole tree under that folder, not just its immediate children."""
        return sorted(
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    def _current_version_id(self, source_id: str, file_path: str) -> str | None:
        # Scoped by (source_id, file_path): a "local_folder" source can hold
        # many files, each with its own independent `is_current` version --
        # see `EvidenceManager._current_version` and migration 0002's
        # docstring for why `source_id` alone isn't enough to identify "the"
        # current version once a source has more than one file.
        with self._session_factory() as session:
            stmt = select(EvidenceVersion.id).where(
                EvidenceVersion.source_id == source_id,
                EvidenceVersion.is_current.is_(True),
                EvidenceVersion.file_path == file_path,
            )
            return session.execute(stmt).scalar_one_or_none()

    def _persist_units_and_chunks(
        self, *, source_id: str, evidence_version_id: str, units, chunks
    ) -> list[ChunkRecord]:
        with self._session_factory() as session:
            unit_objs = [
                EvidenceUnit(
                    evidence_version_id=evidence_version_id,
                    unit_index=u.unit_index,
                    heading=u.heading,
                    content_hash=u.content_hash,
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
                )
                for c in chunk_objs
            ]

    def _current_chunk_ids_for_source(self, source_id: str) -> set[str]:
        """All chunk_ids reachable from `source_id`'s *current*
        EvidenceVersions -- the "should still be indexed" set used for the
        single end-of-run reconcile pass."""
        with self._session_factory() as session:
            stmt = (
                select(Chunk.id)
                .join(EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id)
                .where(
                    EvidenceVersion.source_id == source_id,
                    EvidenceVersion.is_current.is_(True),
                )
            )
            return set(session.execute(stmt).scalars().all())

    def _ingest_one_file(self, source_id: str, path: Path) -> FileIngestResult:
        before_id = self._current_version_id(source_id, str(path))

        evidence_version = self._evidence_manager.ingest_file(
            source_id,
            path,
            parser_name=self._parser.parser_name,
            parser_version=self._parser.parser_version,
        )

        if evidence_version.id == before_id:
            # Same current EvidenceVersion as before ingest_file was called
            # => it was a no-op (unchanged content). Skip parse/chunk/index
            # entirely for this file.
            return FileIngestResult(path=path, status="unchanged")

        parsed = self._parser.parse(source_id, path)
        units, chunks = chunk_document(parsed.text, self._chunk_recipe)

        records = self._persist_units_and_chunks(
            source_id=source_id,
            evidence_version_id=evidence_version.id,
            units=units,
            chunks=chunks,
        )
        self._index_manager.upsert_chunks(records)

        return FileIngestResult(path=path, status="ingested", chunks_written=len(records))

    # -- entrypoint -----------------------------------------------------

    def run_ingestion_for_source(self, source_id: str) -> IngestionJobResult:
        source = self._load_active_source(source_id)
        self._ensure_chunk_recipe_row()

        with self._session_factory() as session:
            job = IngestionJob(
                source_id=source_id,
                status=IngestionJobStatus.RUNNING,
                started_at=_utcnow(),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        files = self._discover_files(Path(source.path))

        file_results: list[FileIngestResult] = []
        for path in files:
            try:
                result = self._ingest_one_file(source_id, path)
            except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
                result = FileIngestResult(path=path, status="failed", error=str(exc))
            file_results.append(result)

        # Single end-of-run reconcile pass -- see module docstring for why
        # this must be whole-source, not per-file.
        current_chunk_ids = self._current_chunk_ids_for_source(source_id)
        self._index_manager.reconcile_source(source_id, current_chunk_ids=current_chunk_ids)

        files_failed = sum(1 for r in file_results if r.status == "failed")
        files_processed = len(file_results)

        error: str | None = None
        if files_processed == 0:
            # No ingestable files found under the source's path. Treated as
            # FAILED rather than a vacuous SUCCEEDED: this almost always
            # means a misconfigured source (empty/wrong folder, or a folder
            # containing only unsupported file types) that the operator
            # should notice, not a quietly-successful no-op.
            job_status = IngestionJobStatus.FAILED
            error = f"no ingestable files found under {source.path}"
        elif files_failed == 0:
            job_status = IngestionJobStatus.SUCCEEDED
        elif files_failed == files_processed:
            job_status = IngestionJobStatus.FAILED
            error = "all files failed to ingest"
        else:
            job_status = IngestionJobStatus.PARTIAL

        stats = {
            "files_processed": files_processed,
            "files_failed": files_failed,
            "chunks_written": sum(r.chunks_written for r in file_results),
        }

        with self._session_factory() as session:
            job = session.get(IngestionJob, job_id)
            job.status = job_status
            job.finished_at = _utcnow()
            job.error = error
            job.stats_json = json.dumps(stats)
            session.add(job)
            session.commit()

        return IngestionJobResult(
            source_id=source_id,
            job_id=job_id,
            status=job_status.value,
            files_processed=files_processed,
            files_failed=files_failed,
            file_results=file_results,
        )
