"""Library self-maintenance: what is out of date, and bringing it up to date.

`LibraryMaintenance.plan()` looks (read only) for four kinds of work and
returns a `MaintenancePlan` of plain-language steps with counts and a time
estimate; `run(plan)` carries it out in the one order that is always right:

    prune -> refresh -> rechunk -> reindex

* **prune**    indexed files that the current rules no longer read (an ignored
               folder, a left-out subfolder, a file the user ignored, a "not a
               real document"): `SourcePruneService`.
* **refresh**  a folder that was registered but never read:
               `IngestionPipeline.run_ingestion_for_source`.
* **rechunk**  files whose chunks were made with an older recipe, re-made from
               the STORED original bytes: `IngestionPipeline.rechunk_source`
               (stale versions only; a file that failed is never retried by an
               update, it stays listed with its reason).
* **reindex**  the search index was built with another embedding model or an
               older text format: `infra/index/reindex.reindex`, which builds
               the new index in scratch tables and swaps it in last, so a
               failure leaves the old index serving. Global, so it is only
               planned when no source filter is given.

Prune and refresh are "cheap": they are cheap to ask for and safe to run
unasked. Rechunk and reindex cost minutes; callers ask once, with the
estimate, before running them (`MaintenancePlan.costly_steps`).

Nothing here reimplements those services, and nothing here ever writes to or
deletes a user's original file: only Docket's own index, rows and stored
copies change. `run` is idempotent: a second `plan()` is empty.

Cooperative stop: `stop()` is consulted between steps and between files (and
before each file of a refresh or rechunk). A stop in the middle of a refresh
ends that run the way the UI's Stop button always did (the job is recorded as
interrupted; finished files are kept).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from docket.core.writer_lock import WriterLock, lock_for_sessions, writer_operation
from docket.core.db.models import (
    Chunk,
    EvidenceVersion,
    IngestionJob,
    SourceStatus,
    VersionStatus,
)
from docket.infra.index.context import INDEX_TEXT_VERSION
from docket.infra.index.manifest import read_manifest
from docket.services.sources.scan import (
    estimate_seconds,
    format_duration,
    recent_seconds_per_file,
    scan_folder,
)

STEP_PRUNE = "prune"
STEP_REFRESH = "refresh"
STEP_RECHUNK = "rechunk"
STEP_REINDEX = "reindex"
STEP_ORDER = (STEP_PRUNE, STEP_REFRESH, STEP_RECHUNK, STEP_REINDEX)
CHEAP_STEPS = frozenset({STEP_PRUNE, STEP_REFRESH})

STAGE_LABELS = {
    STEP_PRUNE: "Removing files that are no longer read",
    STEP_REFRESH: "Reading new folders",
    STEP_RECHUNK: "Re-preparing documents",
    STEP_REINDEX: "Rebuilding the search index",
}

# Rough cost model for work that has no measured rate (seconds).
_PRUNE_SECONDS_PER_FILE = 0.05
_REINDEX_SECONDS_PER_CHUNK = 0.02
_REINDEX_FIXED_SECONDS = 5.0
_MAX_DETAIL = 6
_SCAN_BUDGET_S = 1.0


class MaintenanceStopped(BaseException):
    """Raised from a pipeline progress callback to stop after the current file.

    A `BaseException` for the same reason the UI's stop is: the pipeline
    swallows every `Exception` a progress callback raises."""


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


@dataclass(frozen=True)
class MaintenanceStep:
    kind: str
    count: int  # files (prune/refresh/rechunk) or chunks (reindex)
    label: str  # plain language, one sentence
    seconds: float = 0.0
    source_id: str = ""  # "" for the global reindex
    source_name: str = ""
    detail: tuple[str, ...] = ()  # a few example paths, relative to the source

    @property
    def cheap(self) -> bool:
        return self.kind in CHEAP_STEPS


@dataclass(frozen=True)
class MaintenancePlan:
    steps: tuple[MaintenanceStep, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.steps

    @property
    def updates(self) -> int:
        return len(self.steps)

    @property
    def estimated_seconds(self) -> float:
        return float(sum(s.seconds for s in self.steps))

    @property
    def cheap_steps(self) -> tuple[MaintenanceStep, ...]:
        return tuple(s for s in self.steps if s.cheap)

    @property
    def costly_steps(self) -> tuple[MaintenanceStep, ...]:
        return tuple(s for s in self.steps if not s.cheap)

    def only(self, kinds: set[str] | frozenset[str]) -> "MaintenancePlan":
        return MaintenancePlan(tuple(s for s in self.steps if s.kind in kinds))

    def kinds(self) -> set[str]:
        return {s.kind for s in self.steps}

    @property
    def duration_text(self) -> str:
        return format_duration(self.estimated_seconds)

    @property
    def headline(self) -> str:
        """'Everything is up to date' / '3 updates available (about 2 minutes)'."""
        if self.empty:
            return "Everything is up to date"
        return f"{_plural(self.updates, 'update')} available ({self.duration_text})"

    def lines(self) -> list[str]:
        return [s.label for s in self.steps]


@dataclass(frozen=True)
class MaintenanceProgress:
    """One event of the combined progress stream.

    `done`/`total` count work over the whole plan (files for prune, refresh
    and rechunk; the index rebuild counts as one), so a single bar is honest.
    `total` can be revised upward once a step knows its real file count."""

    stage: str  # one of STEP_ORDER
    done: int
    total: int
    label: str = ""  # plain language for the stage
    current: str = ""  # the file (or folder) being worked on, when known
    source_name: str = ""


@dataclass
class MaintenanceResult:
    pruned: int = 0
    files_read: int = 0  # refresh: files ingested or confirmed unchanged
    files_failed: int = 0  # refresh + rechunk
    rechunked: int = 0
    reindexed: bool = False
    reindex_error: str = ""
    stopped: bool = False
    errors: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    @property
    def ok(self) -> bool:
        return not (self.errors or self.reindex_error or self.stopped)

    def summary(self) -> str:
        parts: list[str] = []
        if self.pruned:
            parts.append(f"{_plural(self.pruned, 'file')} removed from the index")
        if self.files_read:
            parts.append(f"{_plural(self.files_read, 'file')} read")
        if self.rechunked:
            parts.append(f"{_plural(self.rechunked, 'document')} re-prepared")
        if self.reindexed:
            parts.append("search index rebuilt")
        if self.files_failed:
            parts.append(f"{_plural(self.files_failed, 'file')} could not be read")
        if self.reindex_error:
            parts.append("the search index could not be rebuilt and was left as it was")
        if self.stopped:
            parts.append("stopped early; what finished was kept")
        for err in self.errors:
            parts.append(err)
        return "; ".join(parts).capitalize() + "." if parts else "Nothing needed doing."


class LibraryMaintenance:
    def __init__(
        self,
        *,
        session_factory: Any,
        source_manager: Any,
        prune_service: Any,
        pipeline: Any,
        reindex: Callable[[], Any] | None = None,
        manifest_path: Path | str | None = None,
        embed_model: str = "",
        scan: Callable[..., Any] = scan_folder,
        seconds_per_file: Callable[[], float | None] | None = None,
        writer_lock: WriterLock | None = None,
    ) -> None:
        """`pipeline` is the ingestion pipeline or a zero-argument callable
        returning it (building the document parser is slow, so it is only
        asked for when a source actually has indexed files to inspect or work
        to do). `reindex` performs the whole-index rebuild and returns what
        `infra/index/reindex.reindex` returns."""
        self._writer_lock = writer_lock or lock_for_sessions(session_factory)
        self._session_factory = session_factory
        self._sources = source_manager
        self._prune = prune_service
        self._pipeline = pipeline
        self._reindex = reindex
        self._manifest_path = Path(manifest_path) if manifest_path is not None else None
        self._embed_model = embed_model
        self._scan = scan
        self._rate_fn = seconds_per_file

    # -- plumbing -----------------------------------------------------------
    def _pipeline_obj(self) -> Any:
        p = self._pipeline
        if callable(p) and not hasattr(p, "run_ingestion_for_source"):
            return p()
        return p

    def _rate(self) -> float | None:
        if self._rate_fn is not None:
            return self._rate_fn()
        return recent_seconds_per_file(self._session_factory)

    def _local_sources(self, source_ids: list[str] | None, *, active_only: bool = False) -> list[Any]:
        wanted = set(source_ids) if source_ids is not None else None
        allowed = (SourceStatus.ACTIVE,) if active_only else (SourceStatus.ACTIVE, SourceStatus.MISSING)
        out = []
        for s in self._sources.list_sources():
            if getattr(s, "source_type", "local_folder") != "local_folder":
                continue
            if s.status not in allowed:
                continue
            if wanted is not None and s.id not in wanted:
                continue
            out.append(s)
        return out

    def _names(self, sources: list[Any]) -> dict[str, str]:
        from docket.services.sources.resolve import display_names

        try:
            return display_names(self._sources.list_sources())
        except Exception:  # noqa: BLE001 -- a label must never break a plan
            return {s.id: Path(s.path).name or s.path for s in sources}

    def _has_ready_versions(self, source_id: str) -> bool:
        with self._session_factory() as session:
            n = session.execute(
                select(func.count(EvidenceVersion.id)).where(
                    EvidenceVersion.source_id == source_id,
                    EvidenceVersion.status == VersionStatus.READY,
                )
            ).scalar_one()
        return int(n) > 0

    def _never_read(self, source_id: str) -> bool:
        """No ingestion job has ever run for this source and nothing of it is
        indexed (a source an interrupted first run left behind has a job row,
        so it is not "never read")."""
        with self._session_factory() as session:
            jobs = session.execute(
                select(func.count(IngestionJob.id)).where(IngestionJob.source_id == source_id)
            ).scalar_one()
            if int(jobs) > 0:
                return False
            versions = session.execute(
                select(func.count(EvidenceVersion.id)).where(
                    EvidenceVersion.source_id == source_id,
                    EvidenceVersion.status != VersionStatus.SUPERSEDED,
                )
            ).scalar_one()
        return int(versions) == 0

    def _chunk_count(self) -> int:
        with self._session_factory() as session:
            return int(session.execute(select(func.count(Chunk.id))).scalar_one())

    # -- plan ---------------------------------------------------------------
    def plan(self, source_ids: list[str] | None = None) -> MaintenancePlan:
        """What needs doing, read only. `source_ids` limits prune, refresh and
        rechunk to those sources and leaves the global reindex out."""
        rate = self._rate()
        sources = self._local_sources(source_ids)
        names = self._names(sources)
        steps: dict[str, list[MaintenanceStep]] = {k: [] for k in STEP_ORDER}

        for source in sources:
            name = names.get(source.id, Path(source.path).name or source.path)
            try:
                steps[STEP_PRUNE].extend(self._prune_steps(source, name))
            except Exception:  # noqa: BLE001 -- one unreadable source must not hide the rest
                pass
            if source.status == SourceStatus.ACTIVE:
                try:
                    step = self._refresh_step(source, name, rate)
                except Exception:  # noqa: BLE001
                    step = None
                if step is not None:
                    steps[STEP_REFRESH].append(step)
            try:
                step = self._rechunk_step(source, name, rate)
            except Exception:  # noqa: BLE001
                step = None
            if step is not None:
                steps[STEP_RECHUNK].append(step)

        if source_ids is None:
            try:
                step = self._reindex_step()
            except Exception:  # noqa: BLE001
                step = None
            if step is not None:
                steps[STEP_REINDEX].append(step)

        ordered = tuple(s for kind in STEP_ORDER for s in steps[kind])
        return MaintenancePlan(ordered)

    def _prune_steps(self, source: Any, name: str) -> list[MaintenanceStep]:
        candidates = self._prune.find(source.id)
        if not candidates:
            return []
        n = len(candidates)
        return [
            MaintenanceStep(
                STEP_PRUNE,
                n,
                f"{name}: {_plural(n, 'indexed file')} no longer read (in ignored or "
                "left-out folders) will be removed from the index.",
                seconds=n * _PRUNE_SECONDS_PER_FILE,
                source_id=source.id,
                source_name=name,
                detail=tuple(c.relative_path.as_posix() for c in candidates[:_MAX_DETAIL]),
            )
        ]

    def _refresh_step(self, source: Any, name: str, rate: float | None) -> MaintenanceStep | None:
        if not self._never_read(source.id):
            return None
        if not Path(source.path).is_dir():
            return None
        result = self._scan(
            Path(source.path),
            time_budget_s=_SCAN_BUDGET_S,
            extra_rules=getattr(source, "rules_json", None),
            seconds_per_file=rate,
        )
        n = int(getattr(result, "documents", 0))
        if n <= 0:
            return None
        more = " or more" if getattr(result, "incomplete", False) else ""
        return MaintenanceStep(
            STEP_REFRESH,
            n,
            f"{name}: {_plural(n, 'document')}{more} not read yet.",
            seconds=float(getattr(result, "estimated_seconds", 0.0)),
            source_id=source.id,
            source_name=name,
        )

    def _rechunk_step(self, source: Any, name: str, rate: float | None) -> MaintenanceStep | None:
        if not self._has_ready_versions(source.id):
            return None
        stale = self._pipeline_obj().find_stale_versions(source.id)
        if not stale:
            return None
        n = len(stale)
        by_type = Counter(Path(c.path).suffix.lower() for c in stale)
        seconds = estimate_seconds(dict(by_type), rate)
        root = Path(source.path)
        detail = []
        for c in stale[:_MAX_DETAIL]:
            try:
                detail.append(Path(c.path).relative_to(root).as_posix())
            except ValueError:
                detail.append(str(c.path))
        return MaintenanceStep(
            STEP_RECHUNK,
            n,
            f"{name}: {_plural(n, 'document')} prepared with an older recipe will be re-prepared "
            "for better search.",
            seconds=seconds,
            source_id=source.id,
            source_name=name,
            detail=tuple(detail),
        )

    def _reindex_step(self) -> MaintenanceStep | None:
        if self._reindex is None or self._manifest_path is None:
            return None
        manifest = read_manifest(self._manifest_path)
        if manifest is None:
            return None
        if self._embed_model and manifest.embed_model != self._embed_model:
            why = "the embedding model changed"
        elif manifest.index_text_version < INDEX_TEXT_VERSION:
            why = "a newer search format is available"
        else:
            return None
        chunks = self._chunk_count()
        if chunks <= 0:
            return None
        return MaintenanceStep(
            STEP_REINDEX,
            chunks,
            f"The search index will be rebuilt ({why}). The current index keeps working "
            "until the new one is ready.",
            seconds=_REINDEX_FIXED_SECONDS + chunks * _REINDEX_SECONDS_PER_CHUNK,
        )

    # -- run ----------------------------------------------------------------
    @writer_operation("updating the library")
    def run(
        self,
        plan: MaintenancePlan,
        progress: Callable[[MaintenanceProgress], None] | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> MaintenanceResult:
        """Carry out `plan` in prune -> refresh -> rechunk -> reindex order."""
        stopped = stop or (lambda: False)
        result = MaintenanceResult()
        steps = sorted(plan.steps, key=lambda s: STEP_ORDER.index(s.kind))
        state = {"total": max(sum(max(s.count, 1) if s.kind != STEP_REINDEX else 1 for s in steps), 1), "done": 0}

        def emit(stage: str, current: str = "", name: str = "") -> None:
            if progress is None:
                return
            done = min(state["done"], state["total"])
            try:
                progress(
                    MaintenanceProgress(
                        stage, done, state["total"], STAGE_LABELS.get(stage, stage), current, name
                    )
                )
            except Exception:  # noqa: BLE001 -- a UI callback must never break maintenance
                pass

        def revise_total(planned: int, actual: int) -> None:
            state["total"] = max(state["total"] - max(planned, 1) + max(actual, 1), 1)

        for step in steps:
            if stopped():
                result.stopped = True
                break
            try:
                if step.kind == STEP_PRUNE:
                    self._run_prune(step, state, emit, stopped, result)
                elif step.kind == STEP_REFRESH:
                    self._run_refresh(step, state, emit, stopped, result, revise_total)
                elif step.kind == STEP_RECHUNK:
                    self._run_rechunk(step, state, emit, stopped, result, revise_total)
                elif step.kind == STEP_REINDEX:
                    self._run_reindex(state, emit, result)
            except MaintenanceStopped:
                result.stopped = True
                break
            except Exception as exc:  # noqa: BLE001 -- recorded, the next step still runs
                label = step.source_name or "Library"
                result.errors.append(f"{label}: {_one_line(exc)}")
            if result.stopped:
                break
        return result

    def _run_prune(self, step, state, emit, stopped, result) -> None:
        candidates = self._prune.find(step.source_id)
        revise = len(candidates)
        state["total"] = max(state["total"] - max(step.count, 1) + max(revise, 1), 1)
        for c in candidates:
            if stopped():
                result.stopped = True
                return
            emit(STEP_PRUNE, c.relative_path.as_posix(), step.source_name)
            self._prune.apply_candidates([c])
            result.pruned += 1
            state["done"] += 1
        emit(STEP_PRUNE, "", step.source_name)

    def _run_refresh(self, step, state, emit, stopped, result, revise_total) -> None:
        pipeline = self._pipeline_obj()
        base = state["done"]
        planned = step.count
        known = {"total": 0}

        def on_event(event: Any) -> None:
            if known["total"] != event.total:
                revise_total(planned if known["total"] == 0 else known["total"], event.total)
                known["total"] = event.total
            name = event.path.name if hasattr(event.path, "name") else str(event.path)
            if event.kind == "start":
                if stopped():
                    raise MaintenanceStopped()
                state["done"] = base + event.index - 1
                emit(STEP_REFRESH, name, step.source_name)
            else:
                state["done"] = base + event.index
                r = event.result
                if r is not None and r.status == "failed":
                    result.files_failed += 1
                    result.failures.append((str(event.path), str(r.error or "")))
                elif r is not None:
                    result.files_read += 1
                emit(STEP_REFRESH, name, step.source_name)

        job = pipeline.run_ingestion_for_source(step.source_id, progress=on_event)
        state["done"] = base + (known["total"] or planned)
        if job.files_processed == 0 and known["total"] == 0:
            revise_total(planned, 0)
            state["done"] = base
        emit(STEP_REFRESH, "", step.source_name)

    def _run_rechunk(self, step, state, emit, stopped, result, revise_total) -> None:
        pipeline = self._pipeline_obj()
        base = state["done"]
        known = {"total": 0}

        def on_file(index: int, total: int, candidate: Any) -> None:
            if known["total"] != total:
                revise_total(step.count if known["total"] == 0 else known["total"], total)
                known["total"] = total
            state["done"] = base + index - 1
            emit(STEP_RECHUNK, Path(candidate.path).name, step.source_name)

        report = pipeline.rechunk_source(
            step.source_id, progress=on_file, only_stale=True, should_stop=stopped
        )
        for r in report.results:
            if r.status == "failed":
                result.files_failed += 1
                result.failures.append((str(r.path), str(r.error or "")))
            else:
                result.rechunked += 1
        state["done"] = base + len(report.results)
        if report.candidates and len(report.results) < len(report.candidates):
            result.stopped = True
            return
        if not report.candidates:
            revise_total(step.count, 0)
        emit(STEP_RECHUNK, "", step.source_name)

    def _run_reindex(self, state, emit, result) -> None:
        if self._reindex is None:
            return
        emit(STEP_REINDEX, "")
        try:
            self._reindex()
        except Exception as exc:  # noqa: BLE001 -- the old index is intact (reindex swaps last)
            result.reindex_error = _one_line(exc)
            return
        result.reindexed = True
        state["done"] += 1
        emit(STEP_REINDEX, "")

    # -- single-file and narrowing helpers ----------------------------------
    @writer_operation("removing a file from the index")
    def ignore_file(self, source_id: str, relative_path: str) -> int:
        """Stop reading one file: append it to the source's `ignored_files`
        rule and take its indexed versions (a FAILED one included) out of the
        counts and the index. The file on disk is untouched. Returns how many
        versions were removed."""
        rel = relative_path.replace("\\", "/").lstrip("/")
        self._sources.add_ignored_file(source_id, rel)
        matches = [
            c for c in self._prune.find(source_id) if c.relative_path.as_posix() == rel
        ]
        self._prune.apply_candidates(matches)
        return len(matches)

    @writer_operation("updating folder coverage")
    def narrow(
        self,
        source_id: str,
        keep_subfolders: list[str],
        *,
        all_subfolders: list[str] | None = None,
    ) -> int:
        """Keep only `keep_subfolders` of a broad source: save the rules (no
        re-parse), then prune what is no longer read. Returns files removed."""
        self._sources.narrow_source(
            source_id, keep_subfolders, all_subfolders=all_subfolders
        )
        return len(self._prune.apply(source_id))

    def retry_file(
        self,
        source_id: str,
        relative_path: str,
        progress: Callable[[Any], None] | None = None,
    ) -> Any:
        """Re-ingest one file of a source (the pipeline's per-paths entry)."""
        root = Path(self._sources.get_source(source_id).path)
        target = root / relative_path
        return self._pipeline_obj().run_ingestion_for_paths(source_id, [target], progress)


def _one_line(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text.strip().splitlines()[0][:200] if text.strip() else type(exc).__name__


def pending_updates_statement(plan: MaintenancePlan) -> str:
    """A plain sentence for places that only report (no command names)."""
    if plan.empty:
        return "Everything is up to date."
    return f"{_plural(plan.updates, 'update')} available ({plan.duration_text})."


__all__ = [
    "CHEAP_STEPS",
    "LibraryMaintenance",
    "MaintenancePlan",
    "MaintenanceProgress",
    "MaintenanceResult",
    "MaintenanceStep",
    "MaintenanceStopped",
    "STEP_ORDER",
    "pending_updates_statement",
]
