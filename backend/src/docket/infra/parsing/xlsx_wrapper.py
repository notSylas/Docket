"""Thin wrapper around openpyxl used by ingestion for native `.xlsx` parsing.

Mirrors `docling_wrapper.py`'s shape: callers get a plain `ParsedWorkbook`
back, or a `ParseError` they can catch to skip a single bad file without
crashing a batch ingestion run. See Upgrade doc 02 section 4 ("Excel:
preserve values and meaning") for the requirements this module implements.

openpyxl does not evaluate formulas: a workbook is loaded twice, once with
`data_only=False` (cell.value is the formula text, e.g. "=SUM(B2:B4)") and
once with `data_only=True` (cell.value is the last-saved *cached* result --
"stale" if the workbook wasn't recalculated/saved by Excel since the formula
or its inputs last changed). Both are retained per cell so callers can label
the result as "cached as of this workbook version" rather than implying a
live recalculation (doc 02 section 4, case 2).

Read-only (streaming) mode is deliberately NOT used: it can't expose merged
cell ranges, row/column hidden state, or auto-filter range reliably, all of
which doc 02 section 4 requires ("merged ranges, and hidden/filter state").
Its one advantage (lower memory for huge sheets, and omitting charts/images)
isn't worth losing that metadata for the workbook sizes this checkpoint
targets; charts/images are out of scope regardless of read mode (see module
docstring note below).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path

# Literal cell values Excel itself writes into a cell when a formula's cached
# result is an error. openpyxl has no distinct "error" value type for this --
# with `data_only=True` these come back as plain strings indistinguishable
# from a literal text value unless checked against this set. Doc 02 section 4
# is explicit that these must be rejected/flagged, never treated as valid
# evidence of a number.
FORMULA_ERROR_LITERALS = {
    "#DIV/0!",
    "#N/A",
    "#NAME?",
    "#NULL!",
    "#NUM!",
    "#REF!",
    "#VALUE!",
    "#SPILL!",
    "#CALC!",
    "#GETTING_DATA",
}

# Message used when a formula cell's cached result wasn't available at all
# (data_only=True returns None for a formula whose cache was never computed
# -- e.g. the formula was inserted by a tool other than Excel and the
# workbook was never opened/recalculated/saved in Excel). Doc 02 section 4:
# "Missing or unverifiable formula results must be reported." Public (not
# underscore-prefixed): `docket.infra.parsing.xlsx_chunker` compares against it
# directly when rendering an explanatory message for this specific case.
MISSING_FORMULA_CACHE_ERROR = "#CACHE_MISSING"


@dataclass
class CellData:
    sheet: str
    coordinate: str  # e.g. "B14"
    row: int
    column: int  # 1-indexed
    # The value to report as evidence: the literal stored value for a plain
    # cell, or the cached formula result for a formula cell. `None` means
    # genuinely empty -- never coerced to 0 or "" (doc 02 section 4: "A
    # missing cell is not zero").
    value: object | None
    is_formula: bool
    formula_text: str | None  # e.g. "=SUM(B2:B4)"; only set when is_formula
    is_error: bool  # `value` (or the missing-cache case) is a flagged error
    error_text: str | None  # the error literal, or _MISSING_CACHE_ERROR
    number_format: str | None
    is_percentage: bool
    is_date: bool
    scaling_hint: str | None  # e.g. "thousands", detected from number_format
    merged_range: str | None  # e.g. "B2:D2" if this cell is a merge anchor
    merged_non_anchor: bool  # True for a cell swallowed into someone else's merge


@dataclass
class RowData:
    row: int
    cells: list[CellData]
    hidden: bool


@dataclass
class SheetData:
    name: str
    # 1-indexed column -> header label, read from the sheet's first non-empty
    # row, with merged header cells propagated across their full column span
    # (doc 02 section 7 lists "merged headers" as a required validation
    # case). Empty dict if the sheet has no rows at all.
    headers: dict[int, str]
    header_row: int | None
    rows: list[RowData]  # excludes the header row itself
    merged_ranges: list[str]
    hidden_columns: list[str]  # column letters
    filtered: bool


@dataclass
class ParsedWorkbook:
    sheets: list[SheetData]
    source_path: Path
    parser_name: str
    parser_version: str


class ParseError(Exception):
    """Raised for any failure parsing ``path`` for ``source_id``.

    Wraps the underlying openpyxl (or filesystem) exception as ``cause`` so
    callers never need to catch openpyxl's own exception types directly --
    this also covers corrupt files and password-protected workbooks, which
    openpyxl cannot open at all (it raises deep in its zip/XML parsing
    rather than with a single dedicated exception type worth special-casing
    here; doc 02 section 3 requires these produce an explicit error rather
    than an empty successful result, which re-raising as `ParseError` gives
    the caller).
    """

    def __init__(self, source_id: str, path: Path, cause: Exception):
        self.source_id = source_id
        self.path = path
        self.cause = cause
        super().__init__(f"failed to parse {path} for source {source_id}: {cause}")


class UnsupportedSpreadsheetFormatError(Exception):
    """Raised for a recognized-but-not-implemented spreadsheet extension.

    `.xls` (legacy binary BIFF format) cannot be read by openpyxl at all.
    `.xlsm` (macro-enabled OOXML) is structurally readable by the same
    openpyxl code path as `.xlsx`, but hasn't been validated against a real
    file for this checkpoint -- doc 02 section 3: "Exact extensions and
    legacy-format conversion support must be tested before advertising
    them." Both get an explicit, named rejection rather than an untested
    silent attempt or a silent skip.
    """

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            f"unsupported spreadsheet format for {path.suffix}: only .xlsx is "
            "implemented in this checkpoint (legacy .xls and macro-enabled "
            ".xlsm are not supported)"
        )


def _scaling_hint(number_format: str | None) -> str | None:
    """Detect Excel's "scale by 1000 per trailing comma" convention, e.g.
    ``#,##0,`` displays a value divided by 1,000 ("amounts in thousands"),
    ``#,##0,,`` by 1,000,000. Doc 02 section 4: "scaling such as 'amounts in
    thousands'" must be preserved when detectable from the number format.

    Heuristic, not a full number-format parser: strips quoted literal
    sections (e.g. a custom `"K"` suffix) so they can't be mistaken for -- or
    hide -- a trailing scaling comma, then looks for one or more commas
    immediately after the last digit/`#` placeholder in the first
    format-code section (before any `;`-separated positive/negative/zero/
    text sections, which can differ).
    """
    if not number_format:
        return None
    first_section = number_format.split(";")[0]
    stripped = re.sub(r'"[^"]*"', "", first_section)
    match = re.search(r"[0#](,+)\s*$", stripped)
    if not match:
        return None
    n_commas = len(match.group(1))
    if n_commas == 1:
        return "thousands"
    if n_commas == 2:
        return "millions"
    return f"scaled_by_1000_pow_{n_commas}"


def _merge_lookup(worksheet) -> dict[tuple[int, int], tuple[str, bool]]:
    """Map every (row, column) covered by a merged range to
    ``(range_string, is_anchor)`` -- `is_anchor` True only for the merge's
    top-left cell, the only one openpyxl actually stores a value on."""
    lookup: dict[tuple[int, int], tuple[str, bool]] = {}
    for merged_range in worksheet.merged_cells.ranges:
        range_str = str(merged_range)
        anchor = (merged_range.min_row, merged_range.min_col)
        for row in range(merged_range.min_row, merged_range.max_row + 1):
            for col in range(merged_range.min_col, merged_range.max_col + 1):
                lookup[(row, col)] = (range_str, (row, col) == anchor)
    return lookup


def _hidden_columns(worksheet) -> list[str]:
    return sorted(
        letter
        for letter, dim in worksheet.column_dimensions.items()
        if dim.hidden
    )


def _is_filtered(worksheet) -> bool:
    auto_filter = getattr(worksheet, "auto_filter", None)
    return bool(getattr(auto_filter, "ref", None))


def _cell_value_and_format(value_cell, formula_cell) -> tuple[
    object | None, bool, str | None, bool, str | None
]:
    """Resolve one cell's `(value, is_formula, formula_text, is_error, error_text)`.

    `value_cell` comes from the `data_only=True` load (cached results),
    `formula_cell` from the `data_only=False` load (formula text) -- same
    coordinate, two separately loaded workbooks (see module docstring).
    """
    is_formula = formula_cell.data_type == "f"
    formula_text = formula_cell.value if is_formula else None

    if is_formula:
        cached = value_cell.value
        if cached is None:
            # Formula cell but no cached result at all -- doc 02 section 4:
            # "Missing or unverifiable formula results must be reported."
            return None, True, formula_text, True, MISSING_FORMULA_CACHE_ERROR
        if isinstance(cached, str) and cached in FORMULA_ERROR_LITERALS:
            return cached, True, formula_text, True, cached
        return cached, True, formula_text, False, None

    literal = value_cell.value
    if isinstance(literal, str) and literal in FORMULA_ERROR_LITERALS:
        # Rare, but a literal (non-formula) cell can itself contain an error
        # string, e.g. pasted as values from a formula that errored.
        return literal, False, None, True, literal
    return literal, False, None, False, None


def _build_sheet(sheet_name: str, value_ws, formula_ws) -> SheetData:
    merge_lookup = _merge_lookup(value_ws)
    merged_ranges = sorted({str(r) for r in value_ws.merged_cells.ranges})
    hidden_columns = _hidden_columns(value_ws)
    filtered = _is_filtered(value_ws)

    max_row = value_ws.max_row or 0
    max_col = value_ws.max_column or 0

    def row_has_content(row_idx: int) -> bool:
        return any(
            value_ws.cell(row=row_idx, column=c).value is not None
            for c in range(1, max_col + 1)
        )

    header_row_idx: int | None = None
    for row_idx in range(1, max_row + 1):
        if row_has_content(row_idx):
            header_row_idx = row_idx
            break

    headers: dict[int, str] = {}
    if header_row_idx is not None:
        raw_header_values: dict[int, object] = {}
        for col in range(1, max_col + 1):
            anchor_info = merge_lookup.get((header_row_idx, col))
            if anchor_info is not None:
                range_str, is_anchor = anchor_info
                if is_anchor:
                    raw_header_values[col] = value_ws.cell(row=header_row_idx, column=col).value
                else:
                    # Propagate the merged header's anchor value across every
                    # column it spans (doc 02 section 7's "merged headers"
                    # validation case) -- resolved below once all anchors in
                    # this row are known.
                    raw_header_values[col] = ("__merged__", range_str)
            else:
                raw_header_values[col] = value_ws.cell(row=header_row_idx, column=col).value

        # Resolve merged placeholders now that every anchor value in the row
        # has been read.
        anchor_values_by_range: dict[str, object] = {}
        for col, val in raw_header_values.items():
            if not (isinstance(val, tuple) and len(val) == 2 and val[0] == "__merged__"):
                range_str_for_anchor = merge_lookup.get((header_row_idx, col))
                if range_str_for_anchor is not None:
                    anchor_values_by_range[range_str_for_anchor[0]] = val

        for col, val in raw_header_values.items():
            if isinstance(val, tuple) and len(val) == 2 and val[0] == "__merged__":
                resolved = anchor_values_by_range.get(val[1])
            else:
                resolved = val
            if resolved is not None and str(resolved).strip():
                headers[col] = str(resolved).strip()

    rows: list[RowData] = []
    for row_idx in range(1, max_row + 1):
        if row_idx == header_row_idx:
            continue
        if not row_has_content(row_idx):
            continue

        row_dim = value_ws.row_dimensions.get(row_idx)
        hidden = bool(row_dim.hidden) if row_dim is not None else False

        cells: list[CellData] = []
        for col in range(1, max_col + 1):
            value_cell = value_ws.cell(row=row_idx, column=col)
            formula_cell = formula_ws.cell(row=row_idx, column=col)

            anchor_info = merge_lookup.get((row_idx, col))
            merged_range_str: str | None = None
            merged_non_anchor = False
            if anchor_info is not None:
                merged_range_str, is_anchor = anchor_info
                merged_non_anchor = not is_anchor

            if merged_non_anchor:
                # openpyxl represents every non-anchor cell in a merge as a
                # `MergedCell` with value None -- not independently populated
                # data, so it's recorded as metadata (which merge it belongs
                # to) rather than as its own empty/zero value.
                cells.append(
                    CellData(
                        sheet=sheet_name,
                        coordinate=value_cell.coordinate,
                        row=row_idx,
                        column=col,
                        value=None,
                        is_formula=False,
                        formula_text=None,
                        is_error=False,
                        error_text=None,
                        number_format=None,
                        is_percentage=False,
                        is_date=False,
                        scaling_hint=None,
                        merged_range=merged_range_str,
                        merged_non_anchor=True,
                    )
                )
                continue

            value, is_formula, formula_text, is_error, error_text = _cell_value_and_format(
                value_cell, formula_cell
            )
            if value is None and not is_formula:
                continue  # genuinely empty, non-merged cell: not evidence

            number_format = value_cell.number_format
            is_percentage = bool(number_format and "%" in number_format)
            is_date_value = isinstance(value, (datetime, date)) and not isinstance(value, bool)

            cells.append(
                CellData(
                    sheet=sheet_name,
                    coordinate=value_cell.coordinate,
                    row=row_idx,
                    column=col,
                    value=value,
                    is_formula=is_formula,
                    formula_text=formula_text,
                    is_error=is_error,
                    error_text=error_text,
                    number_format=number_format,
                    is_percentage=is_percentage,
                    is_date=is_date_value,
                    scaling_hint=_scaling_hint(number_format),
                    merged_range=merged_range_str,
                    merged_non_anchor=False,
                )
            )

        if cells:
            rows.append(RowData(row=row_idx, cells=cells, hidden=hidden))

    return SheetData(
        name=sheet_name,
        headers=headers,
        header_row=header_row_idx,
        rows=rows,
        merged_ranges=merged_ranges,
        hidden_columns=hidden_columns,
        filtered=filtered,
    )


class XlsxParser:
    """Parses `.xlsx` workbooks via openpyxl into a plain `ParsedWorkbook`.

    Cheap to construct (no model loading, unlike `DoclingParser`) -- a fresh
    instance per call is fine, but the ingestion pipeline still holds one
    instance for parity with how `DoclingParser` is wired in.
    """

    def __init__(self) -> None:
        self._parser_name = "openpyxl"
        self._parser_version = self._detect_version()

    @staticmethod
    def _detect_version() -> str:
        return version("openpyxl")

    @property
    def parser_name(self) -> str:
        return self._parser_name

    @property
    def parser_version(self) -> str:
        return self._parser_version

    def parse(self, source_id: str, path: Path) -> ParsedWorkbook:
        """Load `path` twice (cached-values pass, formula-text pass) and
        build a `ParsedWorkbook`. Any failure (corrupt file, password
        protection, not actually a zip/xlsx at all) is caught and re-raised
        as `ParseError` -- never propagated raw, matching `DoclingParser`.
        """
        import openpyxl

        if path.suffix.lower() != ".xlsx":
            # Defense in depth: `docket.services.ingestion.pipeline` already
            # routes `.xls`/`.xlsm` to an explicit per-file failure before
            # ever calling this parser, but a direct caller (script, future
            # CLI command, test) bypassing that dispatch still gets a named
            # rejection instead of openpyxl silently misreading -- or
            # outright failing to open -- an unsupported format.
            raise UnsupportedSpreadsheetFormatError(path)

        try:
            value_wb = openpyxl.load_workbook(str(path), data_only=True, read_only=False)
            formula_wb = openpyxl.load_workbook(str(path), data_only=False, read_only=False)
        except Exception as exc:  # noqa: BLE001 -- intentionally broad, see ParseError docstring
            raise ParseError(source_id, path, exc) from exc

        try:
            sheets = [
                _build_sheet(name, value_wb[name], formula_wb[name])
                for name in value_wb.sheetnames
            ]
        except Exception as exc:  # noqa: BLE001
            raise ParseError(source_id, path, exc) from exc
        finally:
            value_wb.close()
            formula_wb.close()

        return ParsedWorkbook(
            sheets=sheets,
            source_path=path,
            parser_name=self._parser_name,
            parser_version=self._parser_version,
        )
