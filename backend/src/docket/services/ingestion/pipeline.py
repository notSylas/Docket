"""`IngestionPipeline` -- the incremental, per-source ingestion orchestrator.

Ties together, for one ``Source``: walking its folder, `EvidenceManager`
(store bytes + version tracking), `DoclingParser` (parse to markdown),
`chunk_document` (split into units/chunks), `ChunkWriter` (persist
`EvidenceUnit`/`Chunk` rows + the version-diff/reconcile-set lookups around
them), and `IndexManager` (embed + index). This replaces `spike/ingest.py`'s
"drop everything and rebuild from scratch every run" behavior with a truly
incremental run: unchanged files are detected and skipped before any
parsing/chunking/embedding happens, and one bad file never aborts the rest
of the batch.

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
   (`ChunkWriter.current_version_id`) is scoped the same way for the same
   reason.

2. Reconciliation timing: once per source, at the end of the run, NOT
   per-file. `IndexManager.reconcile_source(source_id, current_chunk_ids)`
   treats `current_chunk_ids` as the *complete* desired chunk_id set for the
   *entire* source (it diffs against `chunk_ids_for_source`, which has no
   per-file granularity -- see `docket.infra.index.vector_index`). Calling it
   per-file with only that one file's chunk_ids would therefore delete every
   *other* file's already-indexed chunks on each file's turn -- actively
   wrong, not just less efficient. So this pipeline processes every file
   first (each wrapped in its own try/except), then does exactly one
   reconcile pass over the union of all chunk_ids currently reachable from
   the source's *current* `EvidenceVersion`s (queried straight from the
   `chunks`/`evidence_versions` tables via `ChunkWriter.current_chunk_ids_for_source`,
   so it naturally includes unchanged files' untouched chunks and excludes
   stale chunks from now-superseded versions). The tradeoff: if the batch is
   interrupted mid-run, this final reconcile never happens and the index can
   keep serving some now-stale chunks until the next successful run. That's
   accepted deliberately -- never wrongly deleting a sibling file's good
   data is more important than always-freshest cleanup, and a subsequent
   run repairs it regardless.

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

3. Visual/formula-transcription passes are optional side passes layered on
   top of core ingestion, each gated by its own `Settings` flag and each
   delegated to its own collaborator: `settings.visual_index_enabled`
   (describes each page image via the VLM, embeds the description, and
   writes it to the `pages` LanceDB table -- `VisualIndexer.index_pages`)
   and `settings.formula_transcription_enabled` (VLM-transcribes each
   above-threshold detected formula region into an UNVERIFIED
   `EvidenceVersion.formula_transcriptions_json`, never promoted into
   searchable/citable evidence; see `Docs/accuracy-evaluation.md`'s "Formula
   evidence and experiments" section -- `FormulaTranscriber.transcribe`).
   Both flags default to False and are independent of each other. Formula
   regions and page images themselves
   (`EvidenceVersion.formula_regions_json`/`page_images_json`) are captured
   and stored unconditionally on every fresh ingest, regardless of either
   flag -- only the VLM description/transcription step is gated.

   Because a flag can be flipped on *after* a file has already been
   ingested with it off, the unchanged-file path (point 1 above) doesn't
   just skip re-processing outright: for each unchanged file it also checks
   whether formula regions, page images, or (if enabled) formula
   transcriptions are still missing on that file's current
   `EvidenceVersion`, and backfills exactly the missing piece(s) in a
   single pass -- re-parsing (re-rendering pages via Docling) only when
   regions or page images themselves are missing, never when only
   transcriptions are missing (which read already-stored page images
   instead of re-rendering; see `FormulaTranscriber.transcribe`'s own
   docstring). This keeps a later flag flip cheap: it never forces a full
   re-ingest of previously-processed files.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.config import Settings
from docket.core.config import settings as _default_settings
from docket.core.db.models import (
    Chunk,
    IngestionJob,
    IngestionJobStatus,
    Source,
    SourceStatus,
)
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.index.manager import IndexManager
from docket.infra.index.visual_index import LancePageIndexWriter
from docket.infra.inference.gateway import InferenceGateway
from docket.services.ingestion.chunk_writer import ChunkWriter
from docket.services.ingestion.formula_transcriber import FormulaTranscriber
from docket.services.ingestion.visual_indexer import VisualIndexer
from docket.infra.parsing.chunker import chunk_document
from docket.infra.parsing.docling_wrapper import DoclingParser
from docket.infra.parsing.recipes import ChunkRecipe
from docket.services.sources.manager import SourceNotFoundError

# Deliberately narrow, spike-validated set. Broadening this to more of
# Docling's supported formats is a one-line change (add to the set); each
# addition just needs its own real-file smoke test, which is out of scope
# for this checkpoint.
SUPPORTED_EXTENSIONS = {".docx", ".pdf"}


logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class ProgressEvent:
    """Emitted around each file during a run. `kind` is "start" (before the
    file is processed; `result` is None) or "done" (after; `result` set).
    `index` is 1-based; `total` is known because discovery precedes the loop."""

    kind: str
    index: int
    total: int
    path: Path
    result: FileIngestResult | None = None


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
        gateway: InferenceGateway | None = None,
        visual_index_writer: LancePageIndexWriter | None = None,
        settings: Settings | None = None,
        chunk_writer: ChunkWriter | None = None,
        formula_transcriber: FormulaTranscriber | None = None,
        visual_indexer: VisualIndexer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._evidence_manager = evidence_manager
        self._parser = parser
        self._index_manager = index_manager
        self._chunk_recipe = chunk_recipe
        # `gateway`/`visual_index_writer` are only exercised when
        # `settings.visual_index_enabled` is True (see `VisualIndexer`).
        # `settings` defaults to the process-wide singleton (whose
        # `visual_index_enabled` default is False) rather than requiring
        # every existing caller/test to pass one -- see `AppContext`'s own
        # note on why it builds a fresh `Settings()` instead for the real
        # CLI wiring path.
        self._gateway = gateway
        self._visual_index_writer = visual_index_writer
        self._settings = settings if settings is not None else _default_settings

        # Three collaborators, each built from these same params (matching
        # `IndexManager`'s dependency-injection precedent: real components
        # get constructed with already-built collaborators rather than raw
        # params). Each is also independently overridable, mainly so tests
        # can construct one directly without a full `IngestionPipeline`.
        # All three share this pipeline's own `session_factory` instance --
        # see the module docstring's point 2 on why each collaborator opens
        # its own session context rather than sharing one long-lived session.
        self._chunk_writer = (
            chunk_writer
            if chunk_writer is not None
            else ChunkWriter(session_factory=session_factory, chunk_recipe=chunk_recipe)
        )
        self._formula_transcriber = (
            formula_transcriber
            if formula_transcriber is not None
            else FormulaTranscriber(
                session_factory=session_factory,
                store=evidence_manager.store,
                gateway=gateway,
                settings=self._settings,
            )
        )
        self._visual_indexer = (
            visual_indexer
            if visual_indexer is not None
            else VisualIndexer(
                session_factory=session_factory,
                store=evidence_manager.store,
                gateway=gateway,
                visual_index_writer=visual_index_writer,
                settings=self._settings,
            )
        )

    # -- internal helpers ---------------------------------------------------

    def _load_active_source(self, source_id: str) -> Source:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status != SourceStatus.ACTIVE:
                raise SourceNotActiveError(source_id, source.status)
            return source

    def _discover_files(self, root: Path) -> list[Path]:
        """Recursively walk `root` for ingestable files. Recursive (not just
        the top level) because a "local_folder" source is meant to cover the
        whole tree under that folder, not just its immediate children."""
        return sorted(
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    def _ingest_one_file(self, source_id: str, path: Path) -> FileIngestResult:
        before_id = self._chunk_writer.current_version_id(source_id, str(path))

        evidence_version = self._evidence_manager.ingest_file(
            source_id,
            path,
            parser_name=self._parser.parser_name,
            parser_version=self._parser.parser_version,
        )

        if evidence_version.id == before_id:
            with self._session_factory() as session:
                has_chunks = session.execute(
                    select(Chunk.id).where(Chunk.evidence_version_id == evidence_version.id).limit(1)
                ).first() is not None
            if has_chunks:
                needs_formula_backfill = evidence_version.formula_regions_json is None
                needs_page_image_backfill = evidence_version.page_images_json is None
                needs_transcription_backfill = (
                    self._settings.formula_transcription_enabled
                    and evidence_version.formula_transcriptions_json is None
                )
                if needs_formula_backfill or needs_page_image_backfill or needs_transcription_backfill:
                    # Backfill coordinates/images/transcriptions on legacy
                    # versions without replacing their chunks.
                    if needs_formula_backfill or needs_page_image_backfill:
                        # Only this branch re-parses (and so re-renders
                        # pages) -- something is genuinely missing that can
                        # only come from Docling.
                        parsed = self._parser.parse(source_id, path)
                        if needs_formula_backfill:
                            self._formula_transcriber.save_regions(
                                evidence_version.id, parsed.formula_regions
                            )
                        formula_regions = parsed.formula_regions
                        if needs_page_image_backfill:
                            page_image_hashes = self._visual_indexer.save_page_images(
                                evidence_version.id, parsed.page_images
                            )
                            if self._settings.visual_index_enabled:
                                self._visual_indexer.index_pages(
                                    evidence_version_id=evidence_version.id,
                                    source_id=source_id,
                                    page_images=parsed.page_images,
                                )
                        else:
                            page_image_hashes = json.loads(evidence_version.page_images_json)
                    else:
                        # Regions + page images are already stored;
                        # transcription is the only thing missing (e.g. the
                        # flag was just turned on). Load both straight from
                        # the DB/store rather than re-parsing, which would
                        # re-render every page for nothing -- transcription
                        # only ever depends on page images already existing.
                        formula_regions = json.loads(evidence_version.formula_regions_json)
                        page_image_hashes = json.loads(evidence_version.page_images_json)

                    if needs_transcription_backfill:
                        self._formula_transcriber.transcribe(
                            evidence_version_id=evidence_version.id,
                            formula_regions=formula_regions,
                            page_image_hashes=page_image_hashes,
                        )
                return FileIngestResult(path=path, status="unchanged")
            # An earlier parse failed after raw bytes were stored; retry derivation.

        parsed = self._parser.parse(source_id, path)
        self._formula_transcriber.save_regions(evidence_version.id, parsed.formula_regions)
        page_image_hashes = self._visual_indexer.save_page_images(
            evidence_version.id, parsed.page_images
        )
        if self._settings.visual_index_enabled:
            self._visual_indexer.index_pages(
                evidence_version_id=evidence_version.id,
                source_id=source_id,
                page_images=parsed.page_images,
            )
        if self._settings.formula_transcription_enabled:
            self._formula_transcriber.transcribe(
                evidence_version_id=evidence_version.id,
                formula_regions=parsed.formula_regions,
                page_image_hashes=page_image_hashes,
            )
        # Prefer the page-marker-annotated text so chunks/units get real
        # page_start/page_end provenance; fall back to the plain text for a
        # `ParsedDocument` built without marker info (e.g. some fixtures) --
        # chunk_document handles marker-free text fine, yielding
        # page_start=page_end=None throughout, exactly as before this
        # checkpoint.
        chunking_text = (
            parsed.text_with_page_markers
            if parsed.text_with_page_markers is not None
            else parsed.text
        )
        units, chunks = chunk_document(chunking_text, self._chunk_recipe)

        records = self._chunk_writer.persist_units_and_chunks(
            source_id=source_id,
            evidence_version_id=evidence_version.id,
            units=units,
            chunks=chunks,
        )
        self._index_manager.upsert_chunks(records)

        return FileIngestResult(path=path, status="ingested", chunks_written=len(records))

    def _finalize_job(
        self, job_id: str, status: IngestionJobStatus, error: str | None, stats: dict
    ) -> None:
        with self._session_factory() as session:
            job = session.get(IngestionJob, job_id)
            job.status = status
            job.finished_at = _utcnow()
            job.error = error
            job.stats_json = json.dumps(stats)
            session.add(job)
            session.commit()

    # -- entrypoint -----------------------------------------------------

    def run_ingestion_for_source(
        self,
        source_id: str,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> IngestionJobResult:
        source = self._load_active_source(source_id)
        self._chunk_writer.ensure_recipe_row()

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

        def emit(event: ProgressEvent) -> None:
            # A misbehaving callback must never break ingestion.
            if progress is None:
                return
            try:
                progress(event)
            except Exception:  # noqa: BLE001
                logger.debug("progress callback raised; ignored", exc_info=True)

        file_results: list[FileIngestResult] = []
        try:
            files = self._discover_files(Path(source.path))
            total = len(files)
            for index, path in enumerate(files, start=1):
                emit(ProgressEvent("start", index, total, path))
                try:
                    result = self._ingest_one_file(source_id, path)
                except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
                    result = FileIngestResult(path=path, status="failed", error=str(exc))
                file_results.append(result)
                emit(ProgressEvent("done", index, total, path, result))

            # Single end-of-run reconcile pass -- see module docstring for why
            # this must be whole-source, not per-file.
            current_chunk_ids = self._chunk_writer.current_chunk_ids_for_source(source_id)
            self._index_manager.reconcile_source(source_id, current_chunk_ids=current_chunk_ids)
        except BaseException as exc:
            # Interrupted (Ctrl-C etc.): don't leave the job row RUNNING forever.
            self._finalize_job(
                job_id,
                IngestionJobStatus.FAILED,
                "interrupted" if not isinstance(exc, Exception) else str(exc),
                {
                    "files_processed": len(file_results),
                    "files_failed": sum(1 for r in file_results if r.status == "failed"),
                    "chunks_written": sum(r.chunks_written for r in file_results),
                },
            )
            raise

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

        self._finalize_job(job_id, job_status, error, stats)

        return IngestionJobResult(
            source_id=source_id,
            job_id=job_id,
            status=job_status.value,
            files_processed=files_processed,
            files_failed=files_failed,
            file_results=file_results,
        )
