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
   returns: same id => unchanged, different id (or no prior current
   version) => new/changed. This is a single cheap indexed lookup
   (`evidence_versions.source_id`), not a re-read of the file.

   Same id doesn't automatically mean "no-op" any more (Upgrade doc 03
   section 4): it only does when that row's `status` is already `READY`
   (chunking + indexing already succeeded -- nothing useful to redo). If the
   bytes are unchanged but the existing row is `PENDING` (never got
   processed, e.g. an interrupted earlier run) or `FAILED` (processing
   raised last time), this pipeline still (re)runs parse/chunk/index against
   that same row instead of silently treating it as settled -- that's the
   `Failed -> Ready` transition. See `_ingest_one_file`.

   Deviation worth flagging: `EvidenceManager`'s "current version" lookup, as
   originally written, was scoped only by `source_id` -- correct for a
   source that maps to one file, but wrong for a multi-file "local_folder"
   source (this checkpoint's whole premise): ingesting file B under the same
   source_id would flip file A's still-current, still-unchanged version to
   `SUPERSEDED` (only one row can be "the" current version per source_id),
   so re-ingesting file A afterwards would look like a false "changed" every
   time -- breaking the exact idempotency this pipeline needs to prove.
   Fixed at the source: `EvidenceVersion` gained a nullable
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

from sqlalchemy.orm import sessionmaker

from docket.core.config import Settings
from docket.core.config import settings as _default_settings
from docket.core.db.models import (
    EvidenceVersion,
    IngestionJob,
    IngestionJobStatus,
    Source,
    SourceStatus,
    VersionStatus,
)
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.index.manager import IndexManager
from docket.infra.index.visual_index import LancePageIndexWriter
from docket.infra.inference.gateway import InferenceGateway
from docket.services.ingestion.chunk_writer import ChunkWriter
from docket.services.ingestion.formula_transcriber import FormulaTranscriber
from docket.infra.index.manifest import IndexManifestGuard
from docket.services.ingestion.visual_indexer import VisualIndexer
from docket.infra.parsing.chunker import chunk_document
from docket.infra.parsing.docling_wrapper import DoclingParser
from docket.infra.parsing.pptx_chunker import chunk_presentation
from docket.infra.parsing.pptx_wrapper import PptxParser
from docket.infra.parsing.recipes import ChunkRecipe
from docket.infra.parsing.xlsx_chunker import chunk_workbook
from docket.infra.parsing.xlsx_wrapper import XlsxParser
from docket.services.sources.manager import SourceManager, SourceNotFoundError

# Deliberately narrow, spike-validated set. Broadening this to more of
# Docling's supported formats is a one-line change (add to the set); each
# addition just needs its own real-file smoke test, which is out of scope
# for this checkpoint.
DOCLING_EXTENSIONS = {".docx", ".pdf"}

# Native openpyxl path (see `docket.infra.parsing.xlsx_wrapper`/`xlsx_chunker`),
# first non-Docling format this pipeline ingests.
XLSX_EXTENSION = ".xlsx"

# Native python-pptx path (see
# `docket.infra.parsing.pptx_wrapper`/`pptx_chunker`).
PPTX_EXTENSION = ".pptx"

SUPPORTED_EXTENSIONS = DOCLING_EXTENSIONS | {XLSX_EXTENSION, PPTX_EXTENSION}

# Recognized Excel-family extensions this pipeline explicitly refuses rather
# than silently never discovering (doc 02 section 3: "Unsupported ... inputs
# must produce explicit coverage/errors instead of empty successful
# results"). `.xls` is the legacy binary format openpyxl cannot read at all;
# `.xlsm` is structurally readable by the same openpyxl code path as
# `.xlsx` but untested for this checkpoint (doc 02 section 3: "Exact
# extensions ... must be tested before advertising them") -- both get a
# named, explicit per-file failure instead of an untested silent attempt.
UNSUPPORTED_EXCEL_EXTENSIONS = {".xls", ".xlsm"}

# `.ppt` (legacy binary PowerPoint) cannot be read by python-pptx at all --
# same explicit-rejection treatment as `.xls`/`.xlsm` above.
UNSUPPORTED_POWERPOINT_EXTENSIONS = {".ppt"}

UNSUPPORTED_EXTENSIONS = UNSUPPORTED_EXCEL_EXTENSIONS | UNSUPPORTED_POWERPOINT_EXTENSIONS

# Everything `_discover_files` walks the source folder for -- broader than
# `SUPPORTED_EXTENSIONS` so an unsupported format shows up as an explicit
# failed `FileIngestResult` instead of being invisible to the batch.
DISCOVERABLE_EXTENSIONS = SUPPORTED_EXTENSIONS | UNSUPPORTED_EXTENSIONS


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UnsupportedFormatError(Exception):
    """Raised for a file whose extension is recognized as an Excel-family or
    PowerPoint-family format but not implemented (see
    `UNSUPPORTED_EXCEL_EXTENSIONS`/`UNSUPPORTED_POWERPOINT_EXTENSIONS`).
    Caught by the same per-file try/except as any other ingestion failure in
    `run_ingestion_for_source`, so it surfaces as one failed
    `FileIngestResult` rather than aborting the batch or silently vanishing."""

    def __init__(self, path: Path):
        self.path = path
        suffix = path.suffix.lower()
        if suffix in UNSUPPORTED_POWERPOINT_EXTENSIONS:
            detail = (
                "only .pptx is implemented in this checkpoint (legacy .ppt is "
                "not supported -- python-pptx cannot read the binary "
                "PowerPoint format at all)"
            )
        else:
            detail = (
                "only .xlsx is implemented in this checkpoint (legacy .xls "
                "and macro-enabled .xlsm are not supported)"
            )
        super().__init__(f"unsupported file format {path.suffix!r} for {path}: {detail}")


class SourceNotActiveError(Exception):
    """Raised when `run_ingestion_for_source` is asked to ingest a source
    that isn't ACTIVE or MISSING (e.g. revoked). No `IngestionJob` row is
    created for a call we refuse outright -- there's nothing to report a job
    status for.

    MISSING is allowed through (not just ACTIVE) so a source whose root was
    previously unreachable gets a chance to recover back to ACTIVE on its
    next scan -- see `run_ingestion_for_source`'s root-reachability check
    (Upgrade doc 03 section 6)."""

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
        xlsx_parser: XlsxParser | None = None,
        pptx_parser: PptxParser | None = None,
        source_manager: SourceManager | None = None,
        manifest_guard: IndexManifestGuard | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._evidence_manager = evidence_manager
        self._parser = parser
        self._index_manager = index_manager
        self._chunk_recipe = chunk_recipe
        # Unlike `DoclingParser` (expensive model loading, so every existing
        # caller builds and passes one explicitly), `XlsxParser` just reads
        # openpyxl's version -- cheap enough to default-construct here so no
        # existing caller needs to change.
        self._xlsx_parser = xlsx_parser if xlsx_parser is not None else XlsxParser()
        # Same reasoning as `_xlsx_parser` above: python-pptx does no model
        # loading, so a default instance is cheap enough to build here
        # without requiring every existing caller to pass one explicitly.
        self._pptx_parser = pptx_parser if pptx_parser is not None else PptxParser()
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
                manifest_guard=manifest_guard,
            )
        )
        # Cheap to default-construct (just wraps session_factory), same
        # reasoning as `_xlsx_parser`/`_pptx_parser` above -- used only for
        # the ACTIVE<->MISSING root-reachability hook below (Upgrade doc 03
        # section 6).
        self._source_manager = (
            source_manager if source_manager is not None else SourceManager(session_factory)
        )

    # -- internal helpers ---------------------------------------------------

    def _load_active_source(self, source_id: str) -> Source:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            # MISSING is allowed through alongside ACTIVE (not just ACTIVE):
            # a source whose root was previously unreachable needs to keep
            # attempting runs so `_check_root_reachable` below has a chance
            # to flip it back to ACTIVE. REVOKED/TOMBSTONED/
            # HARD_DELETE_PENDING/DELETED all still refuse outright.
            if source.status not in (SourceStatus.ACTIVE, SourceStatus.MISSING):
                raise SourceNotActiveError(source_id, source.status)
            return source

    def _check_root_reachable(self, source: Source) -> bool:
        """Local-folder root-reachability check (Upgrade doc 03 section 6):
        distinct from a single file going missing mid-batch (an ordinary
        per-file failure, already handled by `_ingest_one_file`'s own
        try/except) -- this is "the source's root itself is currently
        inaccessible", e.g. an unmounted/renamed/deleted folder.

        Drives the `ACTIVE <-> MISSING` transition directly: flips to
        MISSING on first finding the root unreachable, flips back to ACTIVE
        on the next run that finds it reachable again. Never escalates to
        TOMBSTONED itself -- that requires a separate, explicit, confirmed-
        deletion signal (`SourceManager.mark_tombstoned`), which no
        local-folder signal today can distinguish from "still trying".
        """
        root = Path(source.path)
        reachable = root.exists() and root.is_dir()
        if reachable:
            if source.status == SourceStatus.MISSING:
                self._source_manager.mark_reachable(source.id)
        else:
            if source.status == SourceStatus.ACTIVE:
                self._source_manager.mark_unreachable(source.id)
        return reachable

    def _clear_version_chunks(self, evidence_version_id: str) -> None:
        """Drop a version's prior units/chunks and their index entries before
        rebuilding (retry of a PENDING/FAILED version that already persisted
        rows). Index entries go first, so a failure here leaves the SQLite
        rows as the record of what still needs removing."""
        self._index_manager.delete_chunks(
            self._chunk_writer.chunk_ids_for_version(evidence_version_id)
        )
        self._chunk_writer.delete_units_and_chunks(evidence_version_id)

    def _drop_superseded_index_entries(
        self, before_id: str | None, evidence_version: EvidenceVersion
    ) -> None:
        """`EvidenceManager.ingest_file` flips the previous current version
        to SUPERSEDED when content changed; remove its index entries now
        rather than waiting for the end-of-run reconcile (which stays as the
        repair path)."""
        if before_id is not None and before_id != evidence_version.id:
            self._index_manager.delete_version(before_id)

    def _discover_files(self, root: Path) -> list[Path]:
        """Recursively walk `root` for ingestable files. Recursive (not just
        the top level) because a "local_folder" source is meant to cover the
        whole tree under that folder, not just its immediate children."""
        return sorted(
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in DISCOVERABLE_EXTENSIONS
        )

    def _ingest_one_file(self, source_id: str, path: Path) -> FileIngestResult:
        suffix = path.suffix.lower()

        if suffix in UNSUPPORTED_EXTENSIONS:
            # Discovered deliberately (see DISCOVERABLE_EXTENSIONS) so this
            # is an explicit per-file failure, not a silent omission from
            # the batch.
            raise UnsupportedFormatError(path)

        if suffix == XLSX_EXTENSION:
            return self._ingest_one_xlsx_file(source_id, path)

        if suffix == PPTX_EXTENSION:
            return self._ingest_one_pptx_file(source_id, path)

        before_id = self._chunk_writer.current_version_id(source_id, str(path))

        evidence_version = self._evidence_manager.ingest_file(
            source_id,
            path,
            parser_name=self._parser.parser_name,
            parser_version=self._parser.parser_version,
        )
        self._drop_superseded_index_entries(before_id, evidence_version)

        if evidence_version.id == before_id and evidence_version.status == VersionStatus.READY:
            # Unchanged bytes AND already fully processed: a genuine no-op
            # for parsing/chunking/indexing. The only thing left to check is
            # whether a flag flipped on *after* this version was last
            # processed (visual index / formula transcription), which is
            # handled by a narrower backfill pass below, not a full re-ingest.
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

        # Either genuinely new/changed content (different id), or unchanged
        # content whose current row is PENDING (never processed) or FAILED
        # (processing raised last time) -- either way, (re)run the full
        # parse/chunk/index pipeline against `evidence_version`, tracking its
        # outcome via `status` (Upgrade doc 03 section 4). A corrupt/
        # unparseable file raises here; the caller (`run_ingestion_for_source`)
        # catches it per-file so one bad file never aborts the batch -- this
        # try/except exists only to record FAILED before letting that
        # exception propagate, not to change how it's handled.
        try:
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
            # page_start/page_end provenance; fall back to the plain text for
            # a `ParsedDocument` built without marker info (e.g. some
            # fixtures) -- chunk_document handles marker-free text fine,
            # yielding page_start=page_end=None throughout, exactly as before
            # this checkpoint.
            chunking_text = (
                parsed.text_with_page_markers
                if parsed.text_with_page_markers is not None
                else parsed.text
            )
            units, chunks = chunk_document(chunking_text, self._chunk_recipe)

            self._clear_version_chunks(evidence_version.id)
            records = self._chunk_writer.persist_units_and_chunks(
                source_id=source_id,
                evidence_version_id=evidence_version.id,
                units=units,
                chunks=chunks,
            )
            self._index_manager.upsert_chunks(records)
        except Exception:
            self._evidence_manager.mark_version_status(evidence_version.id, VersionStatus.FAILED)
            raise

        self._evidence_manager.mark_version_status(evidence_version.id, VersionStatus.READY)
        return FileIngestResult(path=path, status="ingested", chunks_written=len(records))

    def _ingest_one_xlsx_file(self, source_id: str, path: Path) -> FileIngestResult:
        """Native `.xlsx` counterpart to the Docling branch in
        `_ingest_one_file` above -- same before/after unchanged-detection and
        PENDING/READY/FAILED lifecycle, via `XlsxParser`/`chunk_workbook`
        instead of `DoclingParser`/`chunk_document`. No formula-transcription
        or visual-index side passes apply here: those are Docling-specific
        (page images and cropped formula regions don't exist for a
        spreadsheet) -- doc 02 section 4/6.
        """
        before_id = self._chunk_writer.current_version_id(source_id, str(path))

        evidence_version = self._evidence_manager.ingest_file(
            source_id,
            path,
            parser_name=self._xlsx_parser.parser_name,
            parser_version=self._xlsx_parser.parser_version,
        )
        self._drop_superseded_index_entries(before_id, evidence_version)

        if evidence_version.id == before_id and evidence_version.status == VersionStatus.READY:
            # Genuine no-op: unchanged bytes, already fully processed. There
            # is no spreadsheet equivalent of the Docling branch's formula-
            # region/page-image backfill -- nothing further to check.
            return FileIngestResult(path=path, status="unchanged")

        try:
            workbook = self._xlsx_parser.parse(source_id, path)
            units, chunks = chunk_workbook(workbook)
            self._clear_version_chunks(evidence_version.id)
            records = self._chunk_writer.persist_units_and_chunks(
                source_id=source_id,
                evidence_version_id=evidence_version.id,
                units=units,
                chunks=chunks,
            )
            self._index_manager.upsert_chunks(records)
        except Exception:
            self._evidence_manager.mark_version_status(evidence_version.id, VersionStatus.FAILED)
            raise

        self._evidence_manager.mark_version_status(evidence_version.id, VersionStatus.READY)
        return FileIngestResult(path=path, status="ingested", chunks_written=len(records))

    def _ingest_one_pptx_file(self, source_id: str, path: Path) -> FileIngestResult:
        """Native `.pptx` counterpart to `_ingest_one_xlsx_file` above --
        same before/after unchanged-detection and PENDING/READY/FAILED
        lifecycle, via `PptxParser`/`chunk_presentation` instead of
        `XlsxParser`/`chunk_workbook`. No formula-transcription or
        visual-index side passes apply here: those are Docling-specific
        (page images don't exist for a presentation) -- doc 02 section 3/6.
        """
        before_id = self._chunk_writer.current_version_id(source_id, str(path))

        evidence_version = self._evidence_manager.ingest_file(
            source_id,
            path,
            parser_name=self._pptx_parser.parser_name,
            parser_version=self._pptx_parser.parser_version,
        )
        self._drop_superseded_index_entries(before_id, evidence_version)

        if evidence_version.id == before_id and evidence_version.status == VersionStatus.READY:
            # Genuine no-op: unchanged bytes, already fully processed. There
            # is no presentation equivalent of the Docling branch's formula-
            # region/page-image backfill -- nothing further to check.
            return FileIngestResult(path=path, status="unchanged")

        try:
            presentation = self._pptx_parser.parse(source_id, path)
            units, chunks = chunk_presentation(presentation)
            self._clear_version_chunks(evidence_version.id)
            records = self._chunk_writer.persist_units_and_chunks(
                source_id=source_id,
                evidence_version_id=evidence_version.id,
                units=units,
                chunks=chunks,
            )
            self._index_manager.upsert_chunks(records)
        except Exception:
            self._evidence_manager.mark_version_status(evidence_version.id, VersionStatus.FAILED)
            raise

        self._evidence_manager.mark_version_status(evidence_version.id, VersionStatus.READY)
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
            if not self._check_root_reachable(source):
                # Root is unreachable: already flipped ACTIVE->MISSING above
                # (or already MISSING) -- this run has nothing to discover,
                # and per section 6 must NOT automatically escalate to
                # TOMBSTONED, so it just reports the run as failed, same as
                # the long-standing "no ingestable files found" case below.
                self._finalize_job(
                    job_id,
                    IngestionJobStatus.FAILED,
                    f"source root unreachable: {source.path}",
                    {"files_processed": 0, "files_failed": 0, "chunks_written": 0},
                )
                return IngestionJobResult(
                    source_id=source_id,
                    job_id=job_id,
                    status=IngestionJobStatus.FAILED.value,
                    files_processed=0,
                    files_failed=0,
                    file_results=[],
                )

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
            self._index_manager.reconcile_source(
                source_id,
                current_chunk_ids=current_chunk_ids,
                current_version_ids=self._chunk_writer.ready_version_ids_for_source(source_id),
            )
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
