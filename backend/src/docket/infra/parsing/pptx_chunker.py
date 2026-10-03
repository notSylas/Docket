"""Splits a parsed `.pptx` presentation (`pptx_wrapper.ParsedPresentation`)
into `EvidenceUnitDraft`/`ChunkDraft` lists -- the same intermediate shape
`docket.infra.parsing.chunker.chunk_document` produces for Docling markdown and
`docket.infra.parsing.xlsx_chunker.chunk_workbook` produces for spreadsheets, so
`docket.services.ingestion.chunk_writer.ChunkWriter.persist_units_and_chunks` and
`docket.services.ingestion.pipeline.IngestionPipeline` can consume any of them
without caring which parser produced it.

Granularity and `unit_kind` decisions:

- **Slide text** (`unit_kind="slide_text"`): one `EvidenceUnit` per text-frame
  shape, not one per slide. Doc 03 section 7's locator example
  (``{"slide": 3, "shape_id": 7}``) is shape-scoped, and a slide commonly
  carries a title placeholder plus one or more independent text boxes --
  merging them into a single slide-wide blob would lose which shape a quoted
  sentence actually came from.
- **Table rows** (`unit_kind="table_row"`): one `EvidenceUnit` per populated
  data row, with the table's header row (if any) propagated into every data
  row's text as ``"Header: value"`` pairs -- the exact row-grouping
  treatment `xlsx_chunker.py` gives spreadsheet rows (doc 02 section 6: "Do
  not flatten a table into unrelated numbers"). A single-row table has no
  separate header to borrow, so its one row is still emitted as data, using
  generic ``"Column N"`` labels.
- **Chart data** (`unit_kind="chart_data"`): one `EvidenceUnit` per chart,
  pairing each series' values with their categories inline -- this is
  native chart *data* only (python-pptx's chart API), never a rendered
  image of the chart. A chart with no native data (i.e. a picture, not a
  real `GraphicFrame.chart`) never reaches this module at all --
  `pptx_wrapper` records it as `UnextractedShapeData` instead, and this
  module does not invent an empty/placeholder unit for it.
- **Speaker notes** (`unit_kind="notes"`): one `EvidenceUnit` per slide that
  has non-empty notes, kept entirely separate from that slide's
  `slide_text`/`table_row` units -- never concatenated into the same
  `EvidenceUnit`/`Chunk`. Citing a claim as "on slide 3" when it only
  appeared in the presenter's private notes would misrepresent the source
  (doc 02 section 3: "distinguish speaker notes from visible slide text").

Every chunk gets `provenance="extracted"` here, same reasoning as
`xlsx_chunker.py`: nothing in this checkpoint produces a Docket-computed
derivation, including chart data -- it's what the chart states, not
something Docket calculated.
"""

from __future__ import annotations

import hashlib
import json
from itertools import zip_longest

from docket.core.config import settings
from docket.infra.parsing.chunker import ChunkDraft, EvidenceUnitDraft
from docket.infra.parsing.pptx_wrapper import (
    ChartData,
    ParsedPresentation,
    SlideData,
    TableData,
)
from docket.infra.parsing.tokens import (
    TokenCounter,
    get_token_counter,
    pack_parts,
    part_locator,
    split_to_fit,
)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slide_heading(slide_number: int) -> str:
    return f"Slide {slide_number}"


def _slide_text_units(slide: SlideData) -> list[tuple[str, dict]]:
    """Return ``(text, locator)`` pairs, one per text-frame shape."""
    results: list[tuple[str, dict]] = []
    for shape in slide.text_shapes:
        header_line = f"Slide: {slide.slide_number} | Shape: {shape.shape_name}"
        text = f"{header_line}\n{shape.text}"
        locator = {
            "slide": slide.slide_number,
            "shape_id": shape.shape_id,
            "shape_name": shape.shape_name,
        }
        results.append((text, locator))
    return results


def _table_row_fragments(table: TableData, row_cells: list[str]) -> list[str]:
    fragments: list[str] = []
    for col_idx, cell_text in enumerate(row_cells):
        if table.header_row is not None and col_idx < len(table.header_row):
            label = table.header_row[col_idx].strip() or f"Column {col_idx + 1}"
        else:
            label = f"Column {col_idx + 1}"
        fragments.append(f"{label}: {cell_text}")
    return fragments


def _table_row_units(slide: SlideData) -> list[tuple[str, dict]]:
    """Return ``(text, locator)`` pairs, one per non-blank data row across
    every table on ``slide``. The header row itself (``rows[0]`` when
    ``header_row`` is set) is never emitted as its own unit -- it's
    propagated into every data row's labels instead, mirroring
    `xlsx_chunker._build_sheet`'s treatment of a spreadsheet's header row.
    """
    results: list[tuple[str, dict]] = []
    for table in slide.tables:
        data_start = 1 if table.header_row is not None else 0
        for row_idx in range(data_start, table.n_rows):
            row_cells = table.rows[row_idx]
            if not any(cell.strip() for cell in row_cells):
                continue  # wholly blank row: no evidence to cite
            header_line = (
                f"Slide: {slide.slide_number} | Table: {table.shape_name} | Row: {row_idx}"
            )
            fragments = _table_row_fragments(table, row_cells)
            text = header_line + "\n" + "\n".join(fragments)
            locator = {
                "slide": slide.slide_number,
                "shape_id": table.shape_id,
                "shape_name": table.shape_name,
                "row": row_idx,
            }
            results.append((text, locator))
    return results


def _chart_text(slide_number: int, chart: ChartData) -> str:
    label = chart.title or chart.shape_name
    type_suffix = f" ({chart.chart_type})" if chart.chart_type else ""
    header_line = f"Slide: {slide_number} | Chart: {label}{type_suffix}"

    lines = [header_line]
    if chart.categories:
        lines.append("Categories: " + ", ".join(chart.categories))

    for series in chart.series:
        series_name = series.name or "Series"
        pairs = []
        for category, value in zip_longest(chart.categories, series.values):
            value_text = "[no value]" if value is None else str(value)
            if category is not None:
                pairs.append(f"{category}={value_text}")
            else:
                pairs.append(value_text)
        lines.append(f"{series_name}: " + ", ".join(pairs))

    return "\n".join(lines)


def _chart_units(slide: SlideData) -> list[tuple[str, dict]]:
    results: list[tuple[str, dict]] = []
    for chart in slide.charts:
        text = _chart_text(slide.slide_number, chart)
        locator = {
            "slide": slide.slide_number,
            "shape_id": chart.shape_id,
            "shape_name": chart.shape_name,
        }
        if chart.chart_type:
            locator["chart_type"] = chart.chart_type
        results.append((text, locator))
    return results


def _notes_unit(slide: SlideData) -> tuple[str, dict] | None:
    if slide.notes_text is None:
        return None
    header_line = f"Slide: {slide.slide_number} | Speaker notes"
    text = f"{header_line}\n{slide.notes_text}"
    locator = {"slide": slide.slide_number}
    return text, locator


def _split_unit_text(text: str, unit_kind: str, counter: TokenCounter, cap: int) -> list[str]:
    """`text` unchanged when within `cap` tokens. Otherwise split the body
    under its first (label) line, which is repeated in every part: shape and
    notes text on paragraph, then sentence, then word boundaries; table rows
    on `Header: value` cell lines; chart data on `name=value` pair
    boundaries, with each series' (or `Categories`) label repeated."""
    if counter.count(text) <= cap:
        return [text]
    header_line, _, body = text.partition("\n")
    if unit_kind == "chart_data":
        lines = []
        for line in body.split("\n"):
            if counter.count(f"{header_line}\n{line}") <= cap:
                lines.append(line)
                continue
            label, _, rest = line.partition(": ")
            groups = pack_parts(rest.split(", "), ", ", counter, cap, f"{header_line}\n{label}:")
            lines.extend(f"{label}: {', '.join(group)}" for group in groups)
        body = "\n".join(lines)
    return [f"{header_line}\n{part}" for part in split_to_fit(body, counter, cap, header_line)]


def chunk_presentation(
    presentation: ParsedPresentation,
    counter: TokenCounter | None = None,
    max_tokens: int | None = None,
) -> tuple[list[EvidenceUnitDraft], list[ChunkDraft]]:
    """Build one `EvidenceUnitDraft` + one `ChunkDraft` per text shape, table
    data row, chart, and non-empty notes slide across `presentation`, in
    slide order (and, within a slide: text shapes, then table rows, then
    charts, then notes).

    Page provenance doesn't apply to presentations (no concept of a "page" in
    a deck) -- `page_start`/`page_end` are left `None` throughout, exactly
    like a Docling document with no page markers or an `.xlsx` workbook.

    A unit over `max_tokens` (default `settings.chunk_max_tokens`) becomes
    several unit/chunk pairs sharing its locator plus `{"part": i, "of": n}`;
    a unit within the cap is emitted unchanged.
    """
    counter = counter or get_token_counter()
    cap = max_tokens or settings.chunk_max_tokens
    units: list[EvidenceUnitDraft] = []
    chunks: list[ChunkDraft] = []
    ordinal = 0

    def _emit(text: str, locator: dict, unit_kind: str) -> None:
        nonlocal ordinal
        heading = _slide_heading(slide.slide_number)
        parts = _split_unit_text(text, unit_kind, counter, cap)

        for part_index, part_text in enumerate(parts):
            unit_index = len(units)
            content_hash = _sha256_hex(part_text)
            part_loc = part_locator(locator, part_index, len(parts))

            units.append(
                EvidenceUnitDraft(
                    unit_index=unit_index,
                    heading=heading,
                    text=part_text,
                    content_hash=content_hash,
                    unit_kind=unit_kind,
                    locator_json=json.dumps(part_loc, sort_keys=True),
                )
            )
            chunks.append(
                ChunkDraft(
                    evidence_unit_index=unit_index,
                    ordinal=ordinal,
                    heading=heading,
                    text=part_text,
                    content_hash=content_hash,
                    provenance="extracted",
                )
            )
            ordinal += 1

    for slide in presentation.slides:
        for text, locator in _slide_text_units(slide):
            _emit(text, locator, "slide_text")
        for text, locator in _table_row_units(slide):
            _emit(text, locator, "table_row")
        for text, locator in _chart_units(slide):
            _emit(text, locator, "chart_data")
        notes = _notes_unit(slide)
        if notes is not None:
            _emit(notes[0], notes[1], "notes")

    return units, chunks
