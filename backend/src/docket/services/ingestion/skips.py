"""Skipped-files report: every file that is not indexed carries a reason.

Reasons are short codes with one plain sentence each. Nothing here ever holds
document contents -- only paths, codes and counts.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

UNSUPPORTED_TYPE = "unsupported_type"
UNSUPPORTED_OTHER = "unsupported_other"  # counted only, never listed per file
CONVERTER_MISSING = "converter_missing"
CONVERSION_FAILED = "conversion_failed"
CONVERSION_TIMEOUT = "conversion_timeout"
TOO_LARGE = "too_large"
PASSWORD_PROTECTED = "password_protected"
INVALID_FILE = "invalid_file"
IGNORED = "ignored"  # in an ignored folder or matched by an ignore rule
READ_FAILED = "read_failed"

REASON_TEXT: dict[str, str] = {
    UNSUPPORTED_TYPE: "Unsupported file type",
    UNSUPPORTED_OTHER: "Other file types Docket does not read",
    CONVERTER_MISSING: "LibreOffice is not available to convert this old Office format",
    CONVERSION_FAILED: "Conversion of this old Office format failed",
    CONVERSION_TIMEOUT: "Conversion of this old Office format timed out",
    TOO_LARGE: "File too large",
    PASSWORD_PROTECTED: "Password-protected",
    INVALID_FILE: "Contents do not match the file extension",
    IGNORED: "In an ignored folder or matched by an ignore rule",
    READ_FAILED: "Could not be read",
}
_CODE_BY_TEXT = {v: k for k, v in REASON_TEXT.items()}

# Extensions people expect to be searchable that Docket does not read: listed
# per file. Any other unread extension is only counted (UNSUPPORTED_OTHER).
NAMED_UNSUPPORTED_EXTENSIONS = frozenset({
    ".msg", ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic",
    ".xlsm", ".xlsb", ".odt", ".ods", ".odp", ".pages", ".numbers", ".key",
    ".pst", ".ost",
})

MAX_RECORDED_PER_RUN = 5000

_PASSWORD = re.compile(r"password|encrypt|decrypt|protected", re.I)


def reason_text(code: str) -> str:
    return REASON_TEXT.get(code, code)


def code_for_text(text: str | None) -> str | None:
    """Reverse of `reason_text` for rows that stored the sentence."""
    return _CODE_BY_TEXT.get(text or "")


def classify_exception(exc: BaseException) -> str | None:
    """Skip code for a per-file ingestion exception, or None when it is an
    ordinary failure (still reported as failed, with READ_FAILED in reports)."""
    from docket.infra.parsing.legacy_office import (
        ConversionError,
        ConversionTimeout,
        ConverterUnavailable,
    )

    if isinstance(exc, ConverterUnavailable):
        return CONVERTER_MISSING
    if isinstance(exc, ConversionTimeout):
        return CONVERSION_TIMEOUT
    # ParseError wraps its cause; look through it.
    cause = getattr(exc, "cause", None) or exc.__cause__
    for candidate in (exc, cause):
        if isinstance(candidate, ConversionError):
            return CONVERSION_FAILED
    text = str(exc)
    if text.startswith("file too large"):
        return TOO_LARGE
    if exc.__class__.__name__ == "UnsupportedFormatError":
        return UNSUPPORTED_TYPE
    if _PASSWORD.search(text):
        return PASSWORD_PROTECTED
    return None


@dataclass(frozen=True)
class SkippedFile:
    path: str  # relative to the source root when known, else as given
    reason: str  # one of the codes above


def relative_path(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            pass
    return str(path)


def count_by_reason(items: Iterable[SkippedFile]) -> dict[str, int]:
    return dict(Counter(i.reason for i in items))


def summary_line(counts: dict[str, int]) -> str | None:
    """One dim line: `Not indexed: 7 files (3 unsupported file type, ...)`."""
    total = sum(counts.values())
    if total <= 0:
        return None
    parts = [
        f"{n} {reason_text(code).lower()}"
        for code, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if n > 0
    ]
    noun = "file" if total == 1 else "files"
    return f"Not indexed: {total} {noun} ({'; '.join(parts)})"


def classify_text(text: str | None) -> str:
    """Code for a stored reason sentence of a skipped/failed file row."""
    exact = code_for_text(text)
    if exact:
        return exact
    low = (text or "").lower()
    if "libreoffice" in low:
        return CONVERSION_TIMEOUT if "timed out" in low else CONVERSION_FAILED
    if "too large" in low:
        return TOO_LARGE
    if _PASSWORD.search(low):
        return PASSWORD_PROTECTED
    if "unsupported file format" in low or "docket cannot read" in low:
        return UNSUPPORTED_TYPE
    return READ_FAILED


@dataclass
class SkippedReport:
    job_id: str | None
    files: list[SkippedFile]
    counts: dict[str, int]  # includes files that are only counted (not listed)


def latest_report(session_factory, source_id: str, root: Path | None = None) -> SkippedReport | None:
    """Skipped-files report of the source's latest whole-source ingest job
    (jobs from before this report existed have no `skipped_counts` and are
    ignored). Paths and reason codes only; never document contents."""
    import json

    from sqlalchemy import select

    from docket.core.db.models import IngestionFileResult, IngestionJob

    with session_factory() as db:
        jobs = db.execute(
            select(IngestionJob).where(IngestionJob.source_id == source_id)
            .order_by(IngestionJob.started_at.desc()).limit(25)
        ).scalars().all()
        job = None
        stats: dict = {}
        for candidate in jobs:
            try:
                parsed = json.loads(candidate.stats_json or "{}")
            except ValueError:
                continue
            if "skipped_counts" in parsed:
                job, stats = candidate, parsed
                break
        if job is None:
            return None
        rows = db.execute(
            select(IngestionFileResult.path, IngestionFileResult.outcome, IngestionFileResult.failure_reason)
            .where(IngestionFileResult.job_id == job.id,
                   IngestionFileResult.outcome.in_(("skipped", "failed")))
            .order_by(IngestionFileResult.path)
        ).all()
    files = [SkippedFile(relative_path(Path(p), root), classify_text(reason)) for p, _o, reason in rows]
    counts = count_by_reason(files)
    # Counted-only reasons live in the job's stats (not listed per file).
    for code in (UNSUPPORTED_OTHER, IGNORED):
        extra = int((stats.get("skipped_counts") or {}).get(code, 0))
        if extra:
            counts[code] = counts.get(code, 0) + extra
    return SkippedReport(job.id, files, counts)
