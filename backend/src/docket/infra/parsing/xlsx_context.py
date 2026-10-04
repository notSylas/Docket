"""Sheet/period context for spreadsheet chunks (Upgrade doc 05 step 3).

A spreadsheet row's own text (`Sheet: Monthly Revenue | Row: 7\\nMonth: Jul ...`)
lacks what a question is phrased with: the fiscal-year label lives in the file
name, the units on a separate Notes sheet, and the month is abbreviated. This
module builds a small, bounded list of context strings per row, stored in the
unit's `locator_json["context"]` and rendered ONLY into the indexed text
(`docket.infra.index.context.index_text_for_chunk`). `Chunk.text` is never
changed, so citations still quote the workbook verbatim.

Rules (deterministic, no model calls, doc 02 section 4):

* Only content the workbook itself states, or a plain surface expansion of it
  (`Jul` -> `July`; a date cell -> `<Month> <year>`, the year being in the
  cell). A fiscal-year label plus a month is NEVER turned into a calendar year:
  a fiscal calendar is not guessed.
* Fiscal-year labels (`FY2025-26`, `FY25-26`, `FY 2025/2026`) from the file
  name stem, the sheet name and title text, emitted as written plus a spaced
  form (`fiscal year 2025 2026`) so the bare year tokens exist for FTS.
  `Q2`/`H1` style period labels in the file or sheet name are kept verbatim.
* Unit/currency/scope lines (keyword filtered, truncated to 80 chars) from a
  notes-like sheet of the same workbook (applies to every other sheet) and
  from the sheet's title rows (the header row when it is one merged label,
  plus any single-cell text rows right after it).
* Whole sheet-level context is capped at 270 characters, deduplicated, in a
  stable order; the per-row month expansion adds at most ~30 more.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from docket.infra.parsing.xlsx_wrapper import ParsedWorkbook, RowData, SheetData

# Bump when the context rules above change: it is part of the xlsx chunk recipe,
# so already-ingested workbooks are reported stale (`docket ingest --rechunk`).
XLSX_CONTEXT_VERSION = 1

ITEM_MAX_CHARS = 80
SHEET_CONTEXT_MAX_CHARS = 270

_NOTES_SHEET = re.compile(r"notes?|readme|about|info|assumptions|legend", re.IGNORECASE)
_FY = re.compile(
    r"(?<![A-Za-z0-9])FY\s?(\d{4}|\d{2})\s?[-–/]\s?(\d{4}|\d{2})(?!\d)", re.IGNORECASE
)
_PERIOD = re.compile(r"(?<![A-Za-z0-9])(Q[1-4]|H[12])(?![A-Za-z0-9])")
_KEYWORDS = re.compile(
    r"unit|inr|usd|eur|gbp|rupee|thousand|million|lakh|crore|currency|fiscal|period|"
    r"actual|budget|forecast|\bfy\b|fy\d|hidden|excluded|\bscope\b",
    re.IGNORECASE,
)
_MONTHS = [
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
]
_MONTH_BY_KEY = {m[:3].lower(): m for m in _MONTHS} | {"sept": "September"} | {
    m.lower(): m for m in _MONTHS
}
_MONTH_HEADER = re.compile(r"^\s*(month|period|date|quarter)", re.IGNORECASE)
_MAX_TITLE_ROWS = 4


def _trim(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= ITEM_MAX_CHARS else text[: ITEM_MAX_CHARS - 1].rstrip() + "…"


def fiscal_year_items(text: str) -> list[str]:
    """`FY2025-26`-style labels in `text`: as written, plus a spaced form."""
    items: list[str] = []
    for m in _FY.finditer(text):
        start, end = m.group(1), m.group(2)
        label = m.group(0).strip()
        if len(end) == 2 and len(start) == 4:
            end_full = start[:2] + end if int(start[:2] + end) > int(start) else None
            spaced = f"fiscal year {start} {end_full or end}"
        else:
            spaced = f"fiscal year {start} {end}"
        items += [label, spaced]
    return items


def _stem(source_path: Path | None) -> str:
    return source_path.stem if source_path else ""


def _text_lines(sheet: SheetData, rows: list[RowData]) -> list[str]:
    lines = []
    for row in rows:
        values = [str(c.value).strip() for c in row.cells if c.value is not None and not c.merged_non_anchor]
        values = [v for v in values if v]
        if values:
            lines.append(": ".join(values))
    return lines


def _notes_lines(sheet: SheetData) -> list[str]:
    return [ln for ln in _text_lines(sheet, sheet.rows) if _KEYWORDS.search(ln)]


def _title_lines(sheet: SheetData) -> list[str]:
    """Title text above the data: the header row when it is a single (merged)
    label, plus single-cell text rows directly after it."""
    lines: list[str] = []
    labels = set(sheet.headers.values())
    if len(labels) == 1 and len(sheet.headers) > 1:
        lines.append(next(iter(labels)))
    for row in sheet.rows[:_MAX_TITLE_ROWS]:
        cells = [c for c in row.cells if c.value is not None and not c.merged_non_anchor]
        if len(cells) != 1 or not isinstance(cells[0].value, str):
            break
        lines.append(cells[0].value.strip())
    return [ln for ln in lines if ln and _KEYWORDS.search(ln)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _cap(items: list[str], limit: int) -> list[str]:
    out: list[str] = []
    used = 0
    for item in items:
        extra = len(item) + (2 if out else 0)
        if used + extra > limit:
            continue
        out.append(item)
        used += extra
    return out


def build_sheet_contexts(workbook: ParsedWorkbook) -> dict[str, list[str]]:
    """Sheet name -> sheet-level context items (see module docstring)."""
    stem = _stem(workbook.source_path)
    notes: list[str] = []
    for sheet in workbook.sheets:
        if _NOTES_SHEET.search(sheet.name):
            notes += _notes_lines(sheet)

    result: dict[str, list[str]] = {}
    for sheet in workbook.sheets:
        is_notes = bool(_NOTES_SHEET.search(sheet.name))
        titles = [] if is_notes else _title_lines(sheet)
        fy_sources = [stem, sheet.name, *titles]
        fy: list[str] = []
        for src in fy_sources:
            fy += fiscal_year_items(src)
        periods = [m.group(1) for src in (stem, sheet.name) for m in _PERIOD.finditer(src)]
        lines = [] if is_notes else notes
        items = _dedupe(fy + periods + [_trim(t) for t in (*lines, *titles)])
        result[sheet.name] = _cap(items, SHEET_CONTEXT_MAX_CHARS)
    return result


def _expand_cell(header: str | None, value: object, is_date: bool) -> str | None:
    if header is None or not _MONTH_HEADER.match(header):
        return None
    if isinstance(value, (datetime, date)) and not isinstance(value, bool):
        return f"{_MONTHS[value.month - 1]} {value.year}"
    if isinstance(value, str):
        return _MONTH_BY_KEY.get(value.strip().rstrip(".").lower())
    return None


def row_month_items(sheet: SheetData, row: RowData) -> list[str]:
    """Month-name (and `<Month> <year>` for date cells) expansions for a row."""
    items = []
    for cell in row.cells:
        if cell.merged_non_anchor or cell.is_error or cell.value is None:
            continue
        item = _expand_cell(sheet.headers.get(cell.column), cell.value, cell.is_date)
        if item:
            items.append(item)
    return _dedupe(items)


def row_context(sheet_items: list[str], sheet: SheetData, row: RowData) -> list[str]:
    """Sheet-level items followed by the row's own month expansions."""
    return _dedupe([*row_month_items(sheet, row), *sheet_items])


def sheet_scope_lines(workbook: ParsedWorkbook, sheet_name: str) -> dict[str, list[str]]:
    """Context for a read-time spreadsheet tool (doc 05 section 8): the same
    sources `build_sheet_contexts` uses, but NOT truncated, capped or
    deduplicated against each other, so a Notes-sheet unit line is returned
    verbatim. Read-only helper: it does not affect the stored chunk recipe
    (`XLSX_CONTEXT_VERSION`).

    Returns ``{"fiscal_year": [...], "period": [...], "units_and_scope": [...]}``:
    fiscal-year labels exactly as written in the file name, sheet name and title
    rows (never converted to calendar years); `Q2`/`H1` style labels from the
    file/sheet name; and the keyword-filtered lines from a notes-like sheet of
    the same workbook (unless `sheet_name` is itself that sheet) plus the sheet's
    own title rows."""
    sheet = next((s for s in workbook.sheets if s.name == sheet_name), None)
    if sheet is None:
        return {"fiscal_year": [], "period": [], "units_and_scope": []}
    stem = _stem(workbook.source_path)
    is_notes = bool(_NOTES_SHEET.search(sheet.name))
    notes: list[str] = []
    for other in workbook.sheets:
        if _NOTES_SHEET.search(other.name):
            notes += _notes_lines(other)
    titles = [] if is_notes else _title_lines(sheet)
    fiscal: list[str] = []
    for src in (stem, sheet.name, *titles):
        fiscal += fiscal_year_items(src)[::2]  # as written; skip the spaced twin
    periods = [m.group(1) for src in (stem, sheet.name) for m in _PERIOD.finditer(src)]
    scope = [] if is_notes else notes
    return {
        "fiscal_year": _dedupe(fiscal),
        "period": _dedupe(periods),
        "units_and_scope": _dedupe([*scope, *titles]),
    }
