"""Deterministic query signals (Upgrade doc 05 section 6): explicit file
scoping, "does the question state a period", and ambiguous-period detection.

No model calls. The decision recorded in doc 05 is "scope retrieval on explicit
names, boost the rest, and ask when ambiguous": a file is scoped ONLY when the
question explicitly names it (see `_explicit_matches`); topical words that merely
resemble a file name never scope (they stay ranking boosts through the indexed
text). Periods are soft signals: a question that states a fiscal year, a year or
a quarter is not treated as ambiguous; a bare month name is not a period.

Ambiguity (`detect_period_ambiguity`) is decided AFTER retrieval, from the chunks
that will actually be shown to the model.

Known gap (deliberately out of scope here): actual vs budget WITHIN the same
fiscal year (`Revenue-FY2025-26` vs `Revenue-FY2025-26-Budget`) is a different
ambiguity that this module does not detect: both workbooks carry the same
fiscal-year label, so `detect_period_ambiguity` treats them as one period.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from sqlalchemy import Engine, text

from docket.core.db.models import SourceStatus, VersionStatus
from docket.infra.parsing.xlsx_context import _FY, fiscal_year_items
from docket.infra.retrieval.hybrid import number_forms
from docket.infra.retrieval.resolver import ResolvedEvidence

# A scoping stem needs at least this many tokens ("budget.xlsx" never scopes).
MIN_STEM_TOKENS = 2

_FILE_NOUNS = (
    "file", "workbook", "spreadsheet", "sheet", "deck", "presentation", "memo",
    "document", "doc", "xlsx", "pptx", "docx",
)
_SEPARATORS = r"[-_.\s]+"
_QUOTE_OPEN = "\"'`“‘"
_QUOTE_CLOSE = "\"'`”’"
# A stem must not be a fragment of a longer name: not preceded or followed by an
# alphanumeric, nor by `-`/`_`/`.` + word character (so `Revenue-FY2025-26` does
# not match inside `Revenue-FY2025-26-Budget`).
_STEM_LEFT = r"(?<![A-Za-z0-9])(?<![-_.]\w)"
_STEM_RIGHT = r"(?![A-Za-z0-9])(?![-_.]\w)"

_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_PERIOD_RE = re.compile(r"(?<![A-Za-z0-9])(?:Q[1-4]|H[12])(?![A-Za-z0-9])", re.IGNORECASE)
_NOISE_CONTEXT_RE = re.compile(r"^(?:fiscal year \d{4} \d{2,4}|FY\s?\d{2,4}\s?[-–/]\s?\d{2,4})$", re.IGNORECASE)
_MAX_NOTE_CONTEXT_LINES = 3


@dataclass(frozen=True)
class EligibleFile:
    """A READY version of an ACTIVE source, by its file name."""

    name: str  # basename, e.g. "Revenue-FY2025-26.xlsx"
    version_id: str

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix


@dataclass(frozen=True)
class QuerySignals:
    scope_version_ids: list[str] = field(default_factory=list)
    scope_files: list[str] = field(default_factory=list)  # human description of the scope
    has_period: bool = False
    number_forms: list[str] = field(default_factory=list)

    @property
    def scoped(self) -> bool:
        return bool(self.scope_version_ids)


_ELIGIBLE_FILES_SQL = text(
    "SELECT evidence_versions.id, evidence_versions.file_path, sources.path "
    "FROM evidence_versions JOIN sources ON sources.id = evidence_versions.source_id "
    "WHERE sources.status = :active_status AND evidence_versions.status = :ready_status "
    "ORDER BY evidence_versions.id"
)


def load_eligible_files(engine: Engine) -> list[EligibleFile]:
    """READY versions of ACTIVE sources (revoked and non-READY files are never
    candidates), named like the resolver names them: the version's own file,
    else the source path."""
    with engine.connect() as conn:
        rows = conn.execute(
            _ELIGIBLE_FILES_SQL,
            {"active_status": SourceStatus.ACTIVE.name, "ready_status": VersionStatus.READY.name},
        ).all()
    return [EligibleFile(name=Path(fp or sp).name, version_id=vid) for vid, fp, sp in rows]


def _stem_tokens(stem: str) -> list[str]:
    return [t for t in re.split(_SEPARATORS, stem.lower()) if t]


def _stem_pattern(tokens: Sequence[str]) -> str:
    return _SEPARATORS.join(re.escape(t) for t in tokens)


def _explicit_matches(question: str, name: str) -> tuple[int, int] | None:
    """Span of the best EXPLICIT reference to file `name` in `question`, or
    None. Explicit means one of: the full file name with its extension; the full
    stem inside quotes/backticks; the full stem adjacent (at most one word away)
    to a file-type noun. Separators `- _ .` and space are interchangeable
    inside the stem; case is ignored."""
    stem, suffix = Path(name).stem, Path(name).suffix
    tokens = _stem_tokens(stem)
    if len(tokens) < MIN_STEM_TOKENS:
        return None
    pattern = _stem_pattern(tokens)
    flags = re.IGNORECASE

    if suffix:
        m = re.search(
            rf"(?<![A-Za-z0-9_-]){pattern}{re.escape(suffix)}(?![A-Za-z0-9])", question, flags
        )
        if m:
            return m.span()
    m = re.search(rf"[{_QUOTE_OPEN}]\s*{pattern}\s*[{_QUOTE_CLOSE}]", question, flags)
    if m:
        return m.span()
    nouns = "|".join(_FILE_NOUNS)
    for rx in (
        rf"(?<![A-Za-z0-9])(?:{nouns})s?\s+(?:\w+\s+)?{_STEM_LEFT}{pattern}{_STEM_RIGHT}",
        rf"{_STEM_LEFT}{pattern}{_STEM_RIGHT}\s+(?:\w+\s+)?(?:{nouns})s?(?![A-Za-z0-9])",
    ):
        m = re.search(rx, question, flags)
        if m:
            return m.span()
    return None


def has_period(question: str) -> bool:
    """True when the question states a period: a fiscal-year label, a four-digit
    year or year range, or Q1-Q4/H1/H2. A bare month name is NOT a period."""
    return bool(fiscal_year_items(question) or _YEAR_RE.search(question) or _PERIOD_RE.search(question))


def extract_signals(question: str, eligible_files: Sequence[EligibleFile]) -> QuerySignals:
    """All deterministic signals for `question` against the eligible files.

    Scope rule: collect every file with an explicit reference; drop a candidate
    whose matched text lies inside a longer candidate's (the longest stem wins);
    if exactly one file name remains (a name held by several versions scopes to
    all of them) scope to it, otherwise do NOT scope (several files named, or a
    tie): never guess."""
    spans: dict[str, tuple[int, int]] = {}
    versions: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    for f in eligible_files:
        key = f.name.lower()
        versions.setdefault(key, []).append(f.version_id)
        display.setdefault(key, f.name)
        if key not in spans:
            span = _explicit_matches(question, f.name)
            if span is not None:
                spans[key] = span

    def contained(a: tuple[int, int], b: tuple[int, int]) -> bool:
        return b[0] <= a[0] and a[1] <= b[1] and a != b

    remaining = [k for k, sp in spans.items() if not any(contained(sp, o) for o in spans.values())]
    scope_ids: list[str] = []
    scope_files: list[str] = []
    if len(remaining) == 1:
        scope_ids = list(versions[remaining[0]])
        scope_files = [display[remaining[0]]]
    return QuerySignals(
        scope_version_ids=scope_ids,
        scope_files=scope_files,
        has_period=has_period(question),
        number_forms=number_forms(question),
    )


# ---------------------------------------------------------------------------
# Ambiguous-period detection (after retrieval, on the included chunks)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AmbiguityOption:
    file: str
    fiscal_year: str
    context_lines: list[str]  # the workbook's own units/scope lines, verbatim


@dataclass(frozen=True)
class PeriodAmbiguity:
    options: list[AmbiguityOption]

    def to_dict(self) -> dict:
        return {
            "reason": "period",
            "options": [{"file": o.file, "fiscal_year": o.fiscal_year} for o in self.options],
        }


def normalize_fiscal_year(label: str) -> str | None:
    """`FY25-26` / `FY 2025/2026` / `FY2025-26` -> `FY2025-26` (a label
    normalisation only; the first year's century is taken as 20xx)."""
    m = _FY.search(label)
    if not m:
        return None
    start, end = m.group(1), m.group(2)
    start4 = start if len(start) == 4 else "20" + start
    return f"FY{start4}-{end[-2:]}"


def _fiscal_labels(context: Sequence[str]) -> list[str]:
    labels: list[str] = []
    for line in context:
        for item in fiscal_year_items(line)[::2]:  # as written; skip the spaced twin
            norm = normalize_fiscal_year(item)
            if norm and norm not in labels:
                labels.append(norm)
    return labels


def _note_context_lines(context: Sequence[str]) -> list[str]:
    """The workbook's own unit/scope lines, verbatim: stored context minus the
    bare fiscal-year labels, period labels and per-row month expansions."""
    keep: list[str] = []
    for line in context:
        stripped = line.strip()
        if (
            not stripped
            or _NOISE_CONTEXT_RE.match(stripped)
            or re.fullmatch(r"(?:Q[1-4]|H[12])", stripped)
            or re.fullmatch(r"[A-Z][a-z]+(?: \d{4})?", stripped) and not re.search(r"[:()]", stripped)
        ):
            continue
        if stripped not in keep:
            keep.append(stripped)
    return keep[:_MAX_NOTE_CONTEXT_LINES]


def detect_period_ambiguity(included: Sequence[ResolvedEvidence]) -> PeriodAmbiguity | None:
    """Ambiguous period: the INCLUDED chunks come from two or more evidence
    versions that share a sheet name but carry different fiscal-year labels
    (from the unit context the workbook itself states). Versions without any
    fiscal-year label never count. Same labels (e.g. actual vs budget of one
    fiscal year) are NOT detected: see the module docstring.

    Returns the options (file, fiscal year, verbatim context lines) of every
    version involved, in first-seen (rank) order; None when unambiguous, or when
    the top-ranked chunk is not part of the conflict. `included` is rank-ordered
    (best first)."""
    by_version: dict[str, list[ResolvedEvidence]] = {}
    for chunk in included:
        by_version.setdefault(chunk.evidence_version_id, []).append(chunk)

    info: dict[str, tuple[str, frozenset[str], list[str], set[str]]] = {}
    for version_id, chunks in by_version.items():
        context: list[str] = []
        sheets: set[str] = set()
        for chunk in chunks:
            if chunk.sheet:
                sheets.add(chunk.sheet.strip().lower())
            for line in chunk.context:
                if line not in context:
                    context.append(line)
        labels = _fiscal_labels(context)
        if not labels or not sheets:
            continue
        info[version_id] = (chunks[0].source_display_name, frozenset(labels), context, sheets)

    involved: list[str] = []
    ids = list(info)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if info[a][1] != info[b][1] and info[a][3] & info[b][3]:
                for v in (a, b):
                    if v not in involved:
                        involved.append(v)
    # The best-ranked chunk must itself belong to the conflict: a spreadsheet
    # pair that only appears as lower-ranked noise behind an unrelated top hit
    # (e.g. a warehouse question that also retrieved two revenue workbooks) is
    # not an ambiguity of THIS question.
    if not involved or included[0].evidence_version_id not in involved:
        return None
    involved.sort(key=ids.index)
    options = [
        AmbiguityOption(
            file=info[v][0],
            fiscal_year=" / ".join(sorted(info[v][1])),
            context_lines=_note_context_lines(info[v][2]),
        )
        for v in involved
    ]
    return PeriodAmbiguity(options=options)
