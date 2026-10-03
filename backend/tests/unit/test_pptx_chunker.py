"""Tests for docket.infra.parsing.pptx_chunker.chunk_presentation.

Pure-function tests against hand-built `ParsedPresentation`/`SlideData`/
`TableData`/`ChartData` fixtures (mirrors `test_xlsx_chunker.py`'s precedent
of testing the chunker in isolation from its upstream parser) --
`test_pptx_wrapper.py` separately covers that real `.pptx` bytes produce
this same shape via `PptxParser`.
"""

from __future__ import annotations

import json

from docket.infra.parsing.pptx_chunker import chunk_presentation
from docket.infra.parsing.pptx_wrapper import (
    ChartData,
    ChartSeriesData,
    ParsedPresentation,
    SlideData,
    TableData,
    TextShapeData,
    UnextractedShapeData,
)


def _slide(
    slide_number: int,
    *,
    text_shapes: list[TextShapeData] | None = None,
    tables: list[TableData] | None = None,
    charts: list[ChartData] | None = None,
    notes_text: str | None = None,
    unextracted_shapes: list[UnextractedShapeData] | None = None,
) -> SlideData:
    return SlideData(
        slide_number=slide_number,
        text_shapes=text_shapes or [],
        tables=tables or [],
        charts=charts or [],
        notes_text=notes_text,
        unextracted_shapes=unextracted_shapes or [],
    )


def _presentation(slides: list[SlideData]) -> ParsedPresentation:
    return ParsedPresentation(
        slides=slides, source_path=None, parser_name="python-pptx", parser_version="test"
    )


# ---------------------------------------------------------------------------
# Slide text
# ---------------------------------------------------------------------------


def test_text_shape_becomes_one_unit_with_shape_locator() -> None:
    slide = _slide(
        1, text_shapes=[TextShapeData(shape_id=7, shape_name="TextBox 1", text="Hello there")]
    )
    units, chunks = chunk_presentation(_presentation([slide]))

    assert len(units) == 1 and len(chunks) == 1
    unit = units[0]
    assert unit.unit_kind == "slide_text"
    assert unit.heading == "Slide 1"
    assert "Hello there" in unit.text
    assert "Slide: 1" in unit.text and "Shape: TextBox 1" in unit.text
    locator = json.loads(unit.locator_json)
    assert locator == {"slide": 1, "shape_id": 7, "shape_name": "TextBox 1"}

    chunk = chunks[0]
    assert chunk.provenance == "extracted"
    assert chunk.evidence_unit_index == 0
    assert chunk.text == unit.text


def test_multiple_text_shapes_on_one_slide_each_get_their_own_unit() -> None:
    slide = _slide(
        1,
        text_shapes=[
            TextShapeData(shape_id=1, shape_name="Title", text="Q1 Results"),
            TextShapeData(shape_id=2, shape_name="Subtitle", text="Internal review"),
        ],
    )
    units, chunks = chunk_presentation(_presentation([slide]))
    assert len(units) == 2
    assert units[0].locator_json != units[1].locator_json  # distinct locators
    assert [json.loads(u.locator_json)["shape_id"] for u in units] == [1, 2]


# ---------------------------------------------------------------------------
# Table rows -- header propagated, blank rows skipped
# ---------------------------------------------------------------------------


def test_table_rows_grouped_with_header_labels_attached() -> None:
    table = TableData(
        shape_id=9,
        shape_name="Table 1",
        rows=[["Region", "Revenue"], ["East", "100"], ["West", "200"]],
        header_row=["Region", "Revenue"],
        n_rows=3,
        n_cols=2,
    )
    slide = _slide(2, tables=[table])
    units, chunks = chunk_presentation(_presentation([slide]))

    assert len(units) == 2  # header row itself produces no unit
    assert all(u.unit_kind == "table_row" for u in units)
    assert "Region: East" in units[0].text and "Revenue: 100" in units[0].text
    assert "Region: West" in units[1].text and "Revenue: 200" in units[1].text

    locator0 = json.loads(units[0].locator_json)
    assert locator0 == {"slide": 2, "shape_id": 9, "shape_name": "Table 1", "row": 1}
    locator1 = json.loads(units[1].locator_json)
    assert locator1["row"] == 2

    assert [c.provenance for c in chunks] == ["extracted", "extracted"]


def test_single_row_table_has_no_header_and_uses_generic_column_labels() -> None:
    table = TableData(
        shape_id=3,
        shape_name="Table 1",
        rows=[["Only", "Row"]],
        header_row=None,
        n_rows=1,
        n_cols=2,
    )
    slide = _slide(1, tables=[table])
    [unit], _ = chunk_presentation(_presentation([slide]))
    assert "Column 1: Only" in unit.text
    assert "Column 2: Row" in unit.text
    locator = json.loads(unit.locator_json)
    assert locator["row"] == 0


def test_wholly_blank_data_row_produces_no_unit() -> None:
    table = TableData(
        shape_id=3,
        shape_name="Table 1",
        rows=[["Header"], [""], ["value"]],
        header_row=["Header"],
        n_rows=3,
        n_cols=1,
    )
    slide = _slide(1, tables=[table])
    units, _ = chunk_presentation(_presentation([slide]))
    assert len(units) == 1
    assert "value" in units[0].text


# ---------------------------------------------------------------------------
# Chart data
# ---------------------------------------------------------------------------


def test_chart_unit_pairs_series_values_with_categories() -> None:
    chart = ChartData(
        shape_id=5,
        shape_name="Chart 1",
        chart_type="COLUMN_CLUSTERED",
        title=None,
        categories=["East", "West"],
        series=[ChartSeriesData(name="Revenue", values=[1.1, 2.2])],
    )
    slide = _slide(3, charts=[chart])
    [unit], [chunk] = chunk_presentation(_presentation([slide]))

    assert unit.unit_kind == "chart_data"
    assert "Categories: East, West" in unit.text
    assert "Revenue: East=1.1, West=2.2" in unit.text
    assert "COLUMN_CLUSTERED" in unit.text
    locator = json.loads(unit.locator_json)
    assert locator == {
        "slide": 3,
        "shape_id": 5,
        "shape_name": "Chart 1",
        "chart_type": "COLUMN_CLUSTERED",
    }
    assert chunk.provenance == "extracted"


def test_chart_with_missing_value_rendered_as_no_value_not_zero() -> None:
    chart = ChartData(
        shape_id=5,
        shape_name="Chart 1",
        chart_type=None,
        title="My Chart",
        categories=["A", "B"],
        series=[ChartSeriesData(name="Series", values=[None, 4.0])],
    )
    slide = _slide(1, charts=[chart])
    [unit], _ = chunk_presentation(_presentation([slide]))
    assert "A=[no value]" in unit.text
    assert "B=4.0" in unit.text
    assert "Chart: My Chart" in unit.text  # title preferred over shape_name
    locator = json.loads(unit.locator_json)
    assert "chart_type" not in locator  # never fabricated when unknown


# ---------------------------------------------------------------------------
# Speaker notes -- distinct unit_kind, never merged with slide text
# ---------------------------------------------------------------------------


def test_notes_produce_a_separate_unit_kind_from_slide_text() -> None:
    slide = _slide(
        4,
        text_shapes=[TextShapeData(shape_id=1, shape_name="TextBox", text="Visible content")],
        notes_text="Presenter-only remark",
    )
    units, chunks = chunk_presentation(_presentation([slide]))

    kinds = {u.unit_kind for u in units}
    assert kinds == {"slide_text", "notes"}

    notes_unit = next(u for u in units if u.unit_kind == "notes")
    text_unit = next(u for u in units if u.unit_kind == "slide_text")
    assert "Presenter-only remark" in notes_unit.text
    assert "Presenter-only remark" not in text_unit.text
    assert "Visible content" not in notes_unit.text

    locator = json.loads(notes_unit.locator_json)
    assert locator == {"slide": 4}
    assert len(chunks) == 2


def test_slide_with_no_notes_produces_no_notes_unit() -> None:
    slide = _slide(1, text_shapes=[TextShapeData(shape_id=1, shape_name="TB", text="x")])
    units, _ = chunk_presentation(_presentation([slide]))
    assert all(u.unit_kind != "notes" for u in units)


# ---------------------------------------------------------------------------
# Ordering, multi-slide, and empty-slide handling
# ---------------------------------------------------------------------------


def test_units_ordered_slide_then_text_table_chart_notes() -> None:
    table = TableData(
        shape_id=2,
        shape_name="T",
        rows=[["H"], ["v"]],
        header_row=["H"],
        n_rows=2,
        n_cols=1,
    )
    chart = ChartData(
        shape_id=3,
        shape_name="C",
        chart_type=None,
        title=None,
        categories=["x"],
        series=[ChartSeriesData(name="s", values=[1.0])],
    )
    slide = _slide(
        1,
        text_shapes=[TextShapeData(shape_id=1, shape_name="TB", text="text")],
        tables=[table],
        charts=[chart],
        notes_text="notes",
    )
    units, chunks = chunk_presentation(_presentation([slide]))
    assert [u.unit_kind for u in units] == ["slide_text", "table_row", "chart_data", "notes"]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]
    assert [u.unit_index for u in units] == [0, 1, 2, 3]


def test_multiple_slides_produce_independent_ordered_units() -> None:
    slide1 = _slide(1, text_shapes=[TextShapeData(shape_id=1, shape_name="TB", text="one")])
    slide2 = _slide(2, text_shapes=[TextShapeData(shape_id=1, shape_name="TB", text="two")])
    units, chunks = chunk_presentation(_presentation([slide1, slide2]))

    assert [u.heading for u in units] == ["Slide 1", "Slide 2"]
    assert [u.unit_index for u in units] == [0, 1]
    assert [c.ordinal for c in chunks] == [0, 1]
    locators = [json.loads(u.locator_json) for u in units]
    assert locators[0]["slide"] == 1
    assert locators[1]["slide"] == 2


def test_empty_slide_produces_no_units_without_crashing() -> None:
    slide = _slide(1)  # no text, tables, charts, or notes
    units, chunks = chunk_presentation(_presentation([slide]))
    assert units == []
    assert chunks == []


# ---------------------------------------------------------------------------
# Token cap
# ---------------------------------------------------------------------------


class _WordCounter:
    name = "test:words"

    def count(self, text: str) -> int:
        return len(text.split())


def _chunk(slide: SlideData, cap: int):
    return chunk_presentation(_presentation([slide]), _WordCounter(), max_tokens=cap)


def test_over_cap_shape_splits_on_paragraphs_with_part_locator() -> None:
    paragraphs = [" ".join(f"p{p}w{w}" for w in range(5)) for p in range(6)]
    slide = _slide(
        1, text_shapes=[TextShapeData(shape_id=7, shape_name="Box", text="\n".join(paragraphs))]
    )
    units, chunks = _chunk(slide, 14)
    assert len(units) == len(chunks) > 1
    for i, unit in enumerate(units, start=1):
        lines = unit.text.split("\n")
        assert lines[0] == "Slide: 1 | Shape: Box"
        assert all(line in paragraphs for line in lines[1:])  # never mid-paragraph
        assert _WordCounter().count(unit.text) <= 14
        locator = json.loads(unit.locator_json)
        assert locator["slide"] == 1 and locator["shape_id"] == 7
        assert locator["part"] == i and locator["of"] == len(units)
        assert unit.unit_kind == "slide_text"


def test_over_cap_notes_split_on_sentences() -> None:
    notes = " ".join(f"Sentence number {i} ends here." for i in range(10))
    units, _ = _chunk(_slide(2, notes_text=notes), 14)
    assert len(units) > 1
    assert all(u.text.startswith("Slide: 2 | Speaker notes\n") for u in units)
    assert all(u.unit_kind == "notes" for u in units)
    assert all(_WordCounter().count(u.text) <= 14 for u in units)
    assert json.loads(units[1].locator_json) == {"slide": 2, "part": 2, "of": len(units)}


def test_over_cap_table_row_splits_on_cells_repeating_label() -> None:
    table = TableData(
        shape_id=9,
        shape_name="T",
        rows=[[f"H{i}" for i in range(8)], [f"v{i}" for i in range(8)]],
        header_row=[f"H{i}" for i in range(8)],
        n_rows=2,
        n_cols=8,
    )
    units, _ = _chunk(_slide(1, tables=[table]), 12)
    assert len(units) > 1
    assert all(u.text.split("\n")[0] == "Slide: 1 | Table: T | Row: 1" for u in units)
    cells = [line for u in units for line in u.text.split("\n")[1:]]
    assert cells == [f"H{i}: v{i}" for i in range(8)]
    assert json.loads(units[0].locator_json)["row"] == 1


def test_over_cap_chart_series_splits_on_pairs_repeating_series_label() -> None:
    cats = [f"c{i}" for i in range(12)]
    chart = ChartData(
        shape_id=5,
        shape_name="Ch",
        chart_type=None,
        title="T",
        categories=cats,
        series=[ChartSeriesData(name="Rev", values=list(range(12)))],
    )
    units, _ = _chunk(_slide(1, charts=[chart]), 12)
    assert len(units) > 1
    assert all(u.text.startswith("Slide: 1 | Chart: T\n") for u in units)
    series_lines = [l for u in units for l in u.text.split("\n") if l.startswith("Rev: ")]
    assert len(series_lines) > 1
    pairs = [p for l in series_lines for p in l[len("Rev: "):].split(", ")]
    assert pairs == [f"c{i}={i}" for i in range(12)]
    assert all(_WordCounter().count(u.text) <= 12 for u in units)


def test_under_cap_units_unchanged_without_part() -> None:
    slide = _slide(
        1, text_shapes=[TextShapeData(shape_id=7, shape_name="Box", text="Hello there")]
    )
    [unit], _ = _chunk(slide, 512)
    assert unit.text == "Slide: 1 | Shape: Box\nHello there"
    assert json.loads(unit.locator_json) == {"slide": 1, "shape_id": 7, "shape_name": "Box"}
