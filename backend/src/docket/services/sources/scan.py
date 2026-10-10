"""Bounded folder scan: what would Docket read here, and is it a lot?

Built on `services/ingestion/ignore.discover` (same ignore rules, same
"not a real document" signature check as ingestion) so a scan can never
disagree with what indexing would actually read. Nothing is parsed, nothing is
written; the walk stops at a time or entry budget and says so.

Used by the folder-first launch (`services/sources/session.py`) to decide
between "just index it", "show a preview" and "this is too broad".
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from docket.services.ingestion.ignore import discover

# Document types a scan counts (the formats ingestion can read). Kept here as
# a literal so a scan never has to import the parser stack.
SCAN_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})

# Per-file indexing cost in seconds, used only when no recent ingestion job
# gives a measured rate. Rough single-machine figures for a typical file, not
# a benchmark: parsing a PDF is the slow part, a workbook the fastest.
SECONDS_PER_TYPE: dict[str, float] = {
    ".pdf": 4.0,
    ".docx": 2.0,
    ".xlsx": 1.0,
    ".pptx": 3.0,
}

# A folder is "broad" above this many documents, or this many entries directly
# inside it (a dump folder), or when the scan ran out of budget.
BROAD_DOCUMENT_LIMIT = 400
BROAD_FIRST_LEVEL_ENTRIES = 2000

DEFAULT_TIME_BUDGET_S = 2.0
DEFAULT_MAX_ENTRIES = 20000


@dataclass(frozen=True)
class ScanResult:
    root: Path
    documents: int = 0
    by_type: dict[str, int] = field(default_factory=dict)  # ".pdf" -> 3
    by_subfolder: dict[str, int] = field(default_factory=dict)  # top-level subfolder -> documents
    root_documents: int = 0  # documents directly in the root folder
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    total_bytes: int = 0
    incomplete: bool = False  # a time or entry budget stopped the scan
    entries_scanned: int = 0
    first_level_entries: int = 0
    estimated_seconds: float = 0.0
    broad: bool = False
    broad_reason: str = ""
    suggested_subfolders: tuple[tuple[str, int], ...] = ()  # (name, documents), most first

    @property
    def subfolders(self) -> list[tuple[str, int]]:
        """Every top-level subfolder that holds documents, most documents first."""
        return sorted(self.by_subfolder.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def estimate_seconds(
    by_type: dict[str, int], seconds_per_file: float | None = None
) -> float:
    """Indexing time estimate: a measured per-file rate when one is given,
    otherwise the per-type constants in `SECONDS_PER_TYPE`."""
    if seconds_per_file is not None and seconds_per_file > 0:
        return round(sum(by_type.values()) * seconds_per_file, 1)
    return float(sum(SECONDS_PER_TYPE.get(ext, 2.0) * n for ext, n in by_type.items()))


def recent_seconds_per_file(session_factory, *, limit: int = 10) -> float | None:
    """Measured seconds per file from the last few finished ingestion jobs that
    actually wrote chunks (jobs that only confirmed unchanged files say
    nothing about parsing cost). None when there is no usable history."""
    from sqlalchemy import select

    from docket.core.db.models import IngestionJob, IngestionJobStatus

    try:
        with session_factory() as session:
            jobs = session.execute(
                select(IngestionJob)
                .where(
                    IngestionJob.status.in_(
                        (IngestionJobStatus.SUCCEEDED, IngestionJobStatus.PARTIAL)
                    ),
                    IngestionJob.finished_at.is_not(None),
                )
                .order_by(IngestionJob.finished_at.desc())
                .limit(limit * 3)
            ).scalars().all()
            rows = [(j.started_at, j.finished_at, j.stats_json) for j in jobs]
    except Exception:  # noqa: BLE001 -- an estimate must never block a launch
        return None
    seconds = 0.0
    files = 0
    used = 0
    for started, finished, raw in rows:
        try:
            stats = json.loads(raw or "{}")
            n = int(stats.get("files_processed", 0)) - int(stats.get("files_failed", 0))
            wrote = int(stats.get("chunks_written", 0))
            span = (finished - started).total_seconds()
        except (ValueError, TypeError, AttributeError):
            continue
        if n <= 0 or wrote <= 0 or span <= 0:
            continue
        seconds += span
        files += n
        used += 1
        if used >= limit:
            break
    if files <= 0:
        return None
    return seconds / files


def scan_folder(
    root: Path,
    *,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    extra_rules: object = None,
    seconds_per_file: float | None = None,
    well_known_broad: Iterable[Path] = (),
    enabled: bool = True,
) -> ScanResult:
    """Walk `root` (bounded) and summarise what indexing would read.

    `extra_rules` is a source's stored `rules_json` (or the excludes a parent
    registration would add), so the numbers match the real run.
    `well_known_broad` lists paths that are broad by name (home, Desktop, ...);
    the session layer owns that list and passes it in.
    """
    root = Path(root)
    report = discover(
        root,
        extensions=SCAN_EXTENSIONS,
        enabled=enabled,
        extra_rules=extra_rules,
        max_entries=max_entries,
        time_budget_s=time_budget_s,
        count_ignored=False,
    )
    by_type: Counter[str] = Counter()
    by_sub: Counter[str] = Counter()
    root_docs = 0
    total_bytes = 0
    for path in report.files:
        by_type[path.suffix.lower()] += 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = (path.name,)
        if len(parts) <= 1:
            root_docs += 1
        else:
            by_sub[parts[0]] += 1

    first_level = 0
    try:
        with os.scandir(root) as it:
            for _ in it:
                first_level += 1
                if first_level > BROAD_FIRST_LEVEL_ENTRIES:
                    break
    except OSError:
        pass

    skipped: dict[str, int] = {}
    if report.dirs_skipped:
        skipped["ignored_folders"] = report.dirs_skipped
    if report.files_skipped:
        skipped["ignored_files"] = report.files_skipped
    if report.invalid_skipped:
        skipped["invalid_files"] = report.invalid_skipped

    documents = len(report.files)
    broad, why = False, ""
    broad_path = _is_in(root, well_known_broad)
    if broad_path:
        broad, why = True, broad_path
    elif documents > BROAD_DOCUMENT_LIMIT:
        broad, why = True, f"more than {BROAD_DOCUMENT_LIMIT} documents"
    elif first_level > BROAD_FIRST_LEVEL_ENTRIES:
        broad, why = True, f"more than {BROAD_FIRST_LEVEL_ENTRIES} items in the top level"
    elif report.incomplete:
        broad, why = True, "too large to look through quickly"

    suggestions = tuple(
        sorted(by_sub.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    )
    return ScanResult(
        root=root,
        documents=documents,
        by_type=dict(by_type),
        by_subfolder=dict(by_sub),
        root_documents=root_docs,
        skipped=skipped,
        total_bytes=total_bytes,
        incomplete=report.incomplete,
        entries_scanned=report.entries_scanned,
        first_level_entries=first_level,
        estimated_seconds=estimate_seconds(dict(by_type), seconds_per_file),
        broad=broad,
        broad_reason=why,
        suggested_subfolders=suggestions,
    )


def _is_in(root: Path, broad_paths: Iterable[Path]) -> str:
    """Reason text when `root` IS one of the well-known broad locations."""
    try:
        real = Path(os.path.realpath(root))
    except OSError:
        return ""
    for candidate in broad_paths:
        try:
            if Path(os.path.realpath(candidate)) == real:
                return "a whole-folder location"
        except OSError:
            continue
    return ""


def format_duration(seconds: float) -> str:
    """'about 2 minutes' / 'under a minute' for the preview line."""
    if seconds < 45:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds / 3600
    return f"about {hours:.1f} hours"


__all__ = [
    "BROAD_DOCUMENT_LIMIT",
    "SCAN_EXTENSIONS",
    "SECONDS_PER_TYPE",
    "ScanResult",
    "estimate_seconds",
    "format_duration",
    "recent_seconds_per_file",
    "scan_folder",
]
