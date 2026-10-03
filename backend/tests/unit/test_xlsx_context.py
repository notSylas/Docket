"""Sheet/period context for spreadsheet chunks (doc 05 step 3)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from docket.core.config import settings
from docket.infra.index.context import index_text_for_chunk
from docket.infra.parsing.xlsx_chunker import chunk_workbook
from docket.infra.parsing.xlsx_context import ITEM_MAX_CHARS, fiscal_year_items
from docket.infra.parsing.xlsx_wrapper import CellData, ParsedWorkbook, RowData, SheetData


def _cell(sheet, row, col, value, *, is_date=False, merged_non_anchor=False) -> CellData:
    return CellData(
        sheet=sheet, coordinate=f"{chr(64 + col)}{row}", row=row, column=col, value=value,
        is_formula=False, formula_text=None, is_error=False, error_text=None,
        number_format="General", is_percentage=False, is_date=is_date, scaling_hint=None,
        merged_range=None, merged_non_anchor=merged_non_anchor,
    )


def _sheet(name, headers, data_rows, first_row=2) -> SheetData:
    rows = []
    for i, values in enumerate(data_rows):
        r = first_row + i
        cells = [
            _cell(name, r, c, v, is_date=isinstance(v, datetime))
            for c, v in enumerate(values, start=1)
            if v is not None
        ]
        rows.append(RowData(row=r, cells=cells, hidden=False))
    return SheetData(
        name=name, headers=headers, header_row=1, rows=rows, merged_ranges=[],
        hidden_columns=[], filtered=False,
    )


def _wb(sheets, path="/d/Revenue-FY2025-26.xlsx") -> ParsedWorkbook:
    return ParsedWorkbook(
        sheets=sheets, source_path=Path(path) if path else None, parser_name="o", parser_version="t"
    )


def _revenue(name="Monthly Revenue"):
    return _sheet(name, {1: "Month", 2: "Tea"}, [["Jul", 4725], ["Aug", 4905]])


def _notes():
    return _sheet(
        "Notes", {1: "Note", 2: "Detail"},
        [["Units", "INR thousands (actuals)"], ["Fiscal year", "April to March"], ["Owner", "Asha"]],
    )


def _contexts(wb):
    units, chunks = chunk_workbook(wb)
    return [json.loads(u.locator_json).get("context") for u in units], units, chunks


def test_fiscal_year_label_forms() -> None:
    assert fiscal_year_items("Revenue-FY2025-26") == ["FY2025-26", "fiscal year 2025 2026"]
    assert fiscal_year_items("FY 2024/2025 plan") == ["FY 2024/2025", "fiscal year 2024 2025"]
    assert fiscal_year_items("fy25-26")[1] == "fiscal year 25 26"
    assert fiscal_year_items("FY2025 only") == []


def test_fy_label_from_file_name_gives_bare_year_tokens() -> None:
    ctx, _, _ = _contexts(_wb([_revenue()]))
    assert "FY2025-26" in ctx[0] and "fiscal year 2025 2026" in ctx[0]


def test_fy_label_from_sheet_name_and_period_label() -> None:
    ctx, _, _ = _contexts(_wb([_revenue("Q2 FY2024-25")], path="/d/book.xlsx"))
    assert "FY2024-25" in ctx[0] and "Q2" in ctx[0]


def test_notes_sheet_units_apply_to_other_sheets_only() -> None:
    ctx, units, _ = _contexts(_wb([_revenue(), _notes()]))
    assert "Units: INR thousands (actuals)" in ctx[0]
    assert "Fiscal year: April to March" in ctx[0]
    assert not any("Asha" in c for c in ctx[0])  # no unit-like keyword
    notes_ctx = [c for c, u in zip(ctx, units) if u.heading == "Notes"]
    assert all("Units: INR thousands (actuals)" not in c for c in notes_ctx)


def test_title_rows_above_data_are_used() -> None:
    sheet = _sheet(
        "Operating Costs",
        {1: "Acme - Expenses", 2: "Acme - Expenses"},
        [["All figures in INR thousands"], ["Jul", 2950]],
        first_row=2,
    )
    ctx, _, _ = _contexts(_wb([sheet], path="/d/x.xlsx"))
    assert "All figures in INR thousands" in ctx[0]
    assert "Jul" not in ctx[0]


def test_month_expansion_and_no_calendar_year_guess() -> None:
    ctx, _, _ = _contexts(_wb([_revenue()]))
    assert "July" in ctx[0] and "August" in ctx[1]
    joined = " ".join(" ".join(c) for c in ctx)
    assert "July 2025" not in joined and "July 2026" not in joined


def test_date_cell_gets_month_and_stated_year() -> None:
    sheet = _sheet("S", {1: "Date", 2: "V"}, [[datetime(2025, 9, 14), 1]])
    ctx, _, _ = _contexts(_wb([sheet], path=None))
    assert ctx == [["September 2025"]]


def test_month_only_under_month_like_header() -> None:
    sheet = _sheet("S", {1: "Region", 2: "V"}, [["Jul", 1]])
    ctx, _, _ = _contexts(_wb([sheet], path=None))
    assert ctx == [None]


def test_cap_dedupe_and_truncation() -> None:
    long_line = "Units: " + "INR thousand " * 30
    notes = _sheet("Notes", {1: "A", 2: "B"}, [[long_line.split(":")[0], long_line.split(":", 1)[1]]] * 2
                   + [[f"Currency {i}", "x" * 60] for i in range(10)])
    ctx, _, _ = _contexts(_wb([_revenue(), notes]))
    items = ctx[0]
    assert len(items) == len({i.lower() for i in items})
    assert all(len(i) <= ITEM_MAX_CHARS for i in items)
    assert sum(len(i) + 2 for i in items) <= 270 + 40


def test_flag_off_is_identical_to_no_context(monkeypatch) -> None:
    wb = _wb([_revenue(), _notes()])
    monkeypatch.setattr(settings, "xlsx_period_context_enabled", False)
    off_units, off_chunks = chunk_workbook(wb)
    assert all("context" not in json.loads(u.locator_json) for u in off_units)
    monkeypatch.setattr(settings, "xlsx_period_context_enabled", True)
    on_units, on_chunks = chunk_workbook(wb)
    assert [c.text for c in on_chunks] == [c.text for c in off_chunks]
    assert [c.content_hash for c in on_chunks] == [c.content_hash for c in off_chunks]
    assert [u.text for u in on_units] == [u.text for u in off_units]


def test_chunk_text_never_contains_context() -> None:
    _, _, chunks = _contexts(_wb([_revenue(), _notes()]))
    assert all("fiscal year" not in c.text and "July" not in c.text for c in chunks)


def test_rendered_index_text_is_deterministic_and_has_context_line() -> None:
    _, units, chunks = _contexts(_wb([_revenue(), _notes()]))
    rendered = index_text_for_chunk(
        "/d/Revenue-FY2025-26.xlsx", units[0].locator_json, units[0].heading, chunks[0].text
    )
    first, second, rest = rendered.split("\n", 2)
    assert first == "Revenue-FY2025-26.xlsx > Monthly Revenue"
    assert second.startswith("Context: July; FY2025-26; fiscal year 2025 2026")
    assert rest == chunks[0].text
    assert rendered == index_text_for_chunk(
        "/d/Revenue-FY2025-26.xlsx", units[0].locator_json, units[0].heading, chunks[0].text
    )


@pytest.mark.parametrize("flag", [True, False])
def test_part_split_rows_share_context(monkeypatch, flag) -> None:
    monkeypatch.setattr(settings, "xlsx_period_context_enabled", flag)
    wb = _wb([_sheet("S", {1: "Month", 2: "A"}, [["Jul", "word " * 400]])])
    from docket.infra.parsing.tokens import get_token_counter

    units, _ = chunk_workbook(wb, get_token_counter(), max_tokens=60)
    loc = [json.loads(u.locator_json) for u in units]
    assert len(units) > 1 and all(("context" in x) == flag for x in loc)
