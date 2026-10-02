"""Tests for docket.infra.parsing.xlsx_chunker.chunk_workbook.

Pure-function tests against hand-built `ParsedWorkbook`/`SheetData`/`RowData`/
`CellData` fixtures (mirrors `test_chunker.py`'s precedent of testing the
chunker in isolation from its upstream parser) -- `test_xlsx_wrapper.py`
separately covers that real `.xlsx` bytes produce this same shape via
`XlsxParser`.
"""

from __future__ import annotations

import json

from docket.infra.parsing.xlsx_chunker import chunk_workbook
from docket.infra.parsing.xlsx_wrapper import (
    MISSING_FORMULA_CACHE_ERROR,
    CellData,
    ParsedWorkbook,
    RowData,
    SheetData,
)


def _cell(
    sheet: str,
    coord: str,
    row: int,
    col: int,
    value,
    *,
    is_formula: bool = False,
    formula_text: str | None = None,
    is_error: bool = False,
    error_text: str | None = None,
    number_format: str | None = "General",
    is_percentage: bool = False,
    is_date: bool = False,
    scaling_hint: str | None = None,
    merged_range: str | None = None,
    merged_non_anchor: bool = False,
) -> CellData:
    return CellData(
        sheet=sheet,
        coordinate=coord,
        row=row,
        column=col,
        value=value,
        is_formula=is_formula,
        formula_text=formula_text,
        is_error=is_error,
        error_text=error_text,
        number_format=number_format,
        is_percentage=is_percentage,
        is_date=is_date,
        scaling_hint=scaling_hint,
        merged_range=merged_range,
        merged_non_anchor=merged_non_anchor,
    )


def _workbook(sheets: list[SheetData]) -> ParsedWorkbook:
    return ParsedWorkbook(
        sheets=sheets, source_path=None, parser_name="openpyxl", parser_version="test"
    )


def test_row_becomes_one_unit_with_header_labels_attached() -> None:
    sheet = SheetData(
        name="Revenue",
        headers={1: "Month", 2: "Amount"},
        header_row=1,
        rows=[
            RowData(
                row=2,
                hidden=False,
                cells=[
                    _cell("Revenue", "A2", 2, 1, "January"),
                    _cell("Revenue", "B2", 2, 2, 120.5),
                ],
            )
        ],
        merged_ranges=[],
        hidden_columns=[],
        filtered=False,
    )
    units, chunks = chunk_workbook(_workbook([sheet]))

    assert len(units) == 1 and len(chunks) == 1
    unit = units[0]
    assert unit.unit_kind == "range"
    assert unit.heading == "Revenue"
    locator = json.loads(unit.locator_json)
    assert locator == {"sheet": "Revenue", "range": "A2:B2"}
    assert "Month: January" in unit.text
    assert "Amount: 120.5" in unit.text
    assert "Sheet: Revenue" in unit.text and "Row: 2" in unit.text

    chunk = chunks[0]
    assert chunk.provenance == "extracted"
    assert chunk.evidence_unit_index == 0
    assert chunk.text == unit.text


def test_formula_result_labeled_as_cached_not_live() -> None:
    sheet = SheetData(
        name="Sheet1",
        headers={1: "Base", 2: "Scaled"},
        header_row=1,
        rows=[
            RowData(
                row=2,
                hidden=False,
                cells=[
                    _cell("Sheet1", "A2", 2, 1, 100),
                    _cell(
                        "Sheet1",
                        "B2",
                        2,
                        2,
                        120,
                        is_formula=True,
                        formula_text="=A2*1.2",
                    ),
                ],
            )
        ],
        merged_ranges=[],
        hidden_columns=[],
        filtered=False,
    )
    [unit], [chunk] = chunk_workbook(_workbook([sheet]))

    assert "Scaled: 120" in unit.text
    assert "formula: =A2*1.2" in unit.text
    assert "cached result as of this workbook version" in unit.text
    assert chunk.provenance == "extracted"


def test_formula_error_is_flagged_and_not_rendered_as_a_value() -> None:
    sheet = SheetData(
        name="Sheet1",
        headers={1: "Numerator", 2: "Result"},
        header_row=1,
        rows=[
            RowData(
                row=2,
                hidden=False,
                cells=[
                    _cell("Sheet1", "A2", 2, 1, 10),
                    _cell(
                        "Sheet1",
                        "B2",
                        2,
                        2,
                        "#DIV/0!",
                        is_formula=True,
                        formula_text="=A2/0",
                        is_error=True,
                        error_text="#DIV/0!",
                    ),
                ],
            )
        ],
        merged_ranges=[],
        hidden_columns=[],
        filtered=False,
    )
    [unit], [chunk] = chunk_workbook(_workbook([sheet]))

    assert "[UNRESOLVED -- #DIV/0!]" in unit.text
    assert "=A2/0" in unit.text
    # Must never silently present the error as a usable number.
    assert "Result: #DIV/0!" not in unit.text
    locator = json.loads(unit.locator_json)
    assert locator["error_cells"] == ["B2"]
    assert chunk.provenance == "extracted"  # still "what the workbook states"


def test_missing_formula_cache_is_flagged() -> None:
    sheet = SheetData(
        name="Sheet1",
        headers={1: "Base", 2: "Derived"},
        header_row=1,
        rows=[
            RowData(
                row=2,
                hidden=False,
                cells=[
                    _cell("Sheet1", "A2", 2, 1, 5),
                    _cell(
                        "Sheet1",
                        "B2",
                        2,
                        2,
                        None,
                        is_formula=True,
                        formula_text="=A2*2",
                        is_error=True,
                        error_text=MISSING_FORMULA_CACHE_ERROR,
                    ),
                ],
            )
        ],
        merged_ranges=[],
        hidden_columns=[],
        filtered=False,
    )
    [unit], _ = chunk_workbook(_workbook([sheet]))
    assert f"[UNRESOLVED -- {MISSING_FORMULA_CACHE_ERROR}]" in unit.text
    assert "no cached result" in unit.text


def test_merged_cell_noted_and_non_anchor_not_rendered_as_separate_cell() -> None:
    sheet = SheetData(
        name="Sheet1",
        headers={1: "Title", 2: "Title"},
        header_row=1,
        rows=[
            RowData(
                row=2,
                hidden=False,
                cells=[
                    _cell(
                        "Sheet1", "A2", 2, 1, "Q1 Totals", merged_range="A2:B2"
                    ),
                    _cell(
                        "Sheet1",
                        "B2",
                        2,
                        2,
                        None,
                        merged_range="A2:B2",
                        merged_non_anchor=True,
                    ),
                ],
            )
        ],
        merged_ranges=["A2:B2"],
        hidden_columns=[],
        filtered=False,
    )
    [unit], _ = chunk_workbook(_workbook([sheet]))

    assert unit.text.count("Title:") == 1  # the non-anchor cell adds no fragment
    assert "merged A2:B2" in unit.text
    locator = json.loads(unit.locator_json)
    assert locator["range"] == "A2:B2"
    assert locator["merged_ranges"] == ["A2:B2"]


def test_hidden_row_and_filtered_sheet_surfaced_as_metadata_not_excluded() -> None:
    sheet = SheetData(
        name="Sheet1",
        headers={1: "Label"},
        header_row=1,
        rows=[
            RowData(row=2, hidden=True, cells=[_cell("Sheet1", "A2", 2, 1, "secret")]),
        ],
        merged_ranges=[],
        hidden_columns=[],
        filtered=True,
    )
    [unit], _ = chunk_workbook(_workbook([sheet]))

    # Still produced as evidence -- hidden/filtered state is metadata, not a
    # reason to drop the row (doc 02 section 4: don't silently decide
    # whether hidden rows belong in a total).
    assert "Label: secret" in unit.text
    assert "hidden row" in unit.text
    assert "active filter" in unit.text
    locator = json.loads(unit.locator_json)
    assert locator["hidden"] is True
    assert locator["filtered"] is True


def test_multiple_sheets_produce_independent_ordered_units() -> None:
    sheet1 = SheetData(
        name="Alpha",
        headers={1: "X"},
        header_row=1,
        rows=[RowData(row=2, hidden=False, cells=[_cell("Alpha", "A2", 2, 1, 1)])],
        merged_ranges=[],
        hidden_columns=[],
        filtered=False,
    )
    sheet2 = SheetData(
        name="Beta",
        headers={1: "Y"},
        header_row=1,
        rows=[RowData(row=2, hidden=False, cells=[_cell("Beta", "A2", 2, 1, "two")])],
        merged_ranges=[],
        hidden_columns=[],
        filtered=False,
    )
    units, chunks = chunk_workbook(_workbook([sheet1, sheet2]))

    assert [u.heading for u in units] == ["Alpha", "Beta"]
    assert [u.unit_index for u in units] == [0, 1]
    assert [c.ordinal for c in chunks] == [0, 1]
    locators = [json.loads(u.locator_json) for u in units]
    assert locators[0]["sheet"] == "Alpha"
    assert locators[1]["sheet"] == "Beta"


def test_row_with_no_renderable_fragments_produces_no_unit() -> None:
    # A row consisting only of a merge's non-anchor cell (shouldn't occur in
    # practice since `_build_sheet` always keeps the anchor too, but the
    # chunker must not crash or emit a blank unit if it ever does).
    sheet = SheetData(
        name="Sheet1",
        headers={},
        header_row=None,
        rows=[
            RowData(
                row=2,
                hidden=False,
                cells=[
                    _cell(
                        "Sheet1", "B2", 2, 2, None, merged_range="A2:B2", merged_non_anchor=True
                    )
                ],
            )
        ],
        merged_ranges=["A2:B2"],
        hidden_columns=[],
        filtered=False,
    )
    units, chunks = chunk_workbook(_workbook([sheet]))
    assert units == []
    assert chunks == []
