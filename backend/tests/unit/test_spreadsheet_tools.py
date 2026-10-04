"""Tests for the deterministic spreadsheet tools (doc 05 section 8): the
`WorkbookReader` behind `read_range`, the `calculate` tool, and their LangChain
wrappers. Real workbooks are generated with openpyxl, parsed and chunked with
the production xlsx parser/chunker, and stored in a real migrated SQLite DB and
a real content-addressed store -- no Ollama.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
import pytest
from openpyxl.styles import Font
from sqlalchemy import Engine

from docket.core.db.engine import get_session_factory
from docket.core.db.identity import compute_chunk_id, compute_recipe_id
from docket.core.db.models import (
    AuthorizedSource,
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    Source,
    SourceStatus,
    VersionStatus,
    Workspace,
)
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.evidence.workbook_reader import (
    MAX_CELLS,
    MAX_ROWS,
    WorkbookReader,
    WorkbookReadError,
    cell_units,
    context_units,
    parse_a1_range,
)
from docket.infra.parsing.xlsx_chunker import chunk_workbook
from docket.infra.parsing.xlsx_wrapper import XlsxParser
from docket.services.agent.calculator import CalculationError, calculate, safe_calculate
from docket.services.agent.tools import make_calculate_tool, make_read_range_tool


def _inject_cached(xlsx_path: Path, cell_ref: str, cached: str, sheet_file: str) -> None:
    with zipfile.ZipFile(xlsx_path) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    xml = items[sheet_file].decode()
    pattern = re.compile(rf'<c r="{cell_ref}"([^>]*)><f>([^<]*)</f>(?:<v>[^<]*</v>|<v\s*/>)</c>')
    m = pattern.search(xml)
    assert m, f"no empty-cache formula at {cell_ref}"
    xml = xml[: m.start()] + f'<c r="{cell_ref}"><f>{m.group(2)}</f><v>{cached}</v></c>' + xml[m.end():]
    items[sheet_file] = xml.encode()
    with zipfile.ZipFile(xlsx_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in items.items():
            zf.writestr(n, d)


def _revenue_fy2526(path: Path) -> None:
    """INR thousands on a Notes sheet; blank vs 0; percent; hidden row; one
    formula with a cached result (E2) and one without (E3)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monthly Revenue"
    ws.append(["Month", "Marigold Chai", "Saffron Rusk", "Juniper Masala", "Total", "MoM Growth %"])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.append(["Apr", 4120, 2310, None, "=SUM(B2:D2)", None])  # row 2, D blank
    ws.append(["May", 4380, 2455, 0, "=SUM(B3:D3)", 0.063])  # row 3, D is 0
    ws.append(["Jun", 4610, 2590, 780, 7980, 0.167])  # row 4
    ws.append(["Jul", 4725, 2640, 915, 8280, -0.04])  # row 5
    ws.append(["Memo: one-off export order (not in totals)", None, None, None, 1250, None])  # row 6
    ws.row_dimensions[6].hidden = True
    for r in range(2, 7):
        ws.cell(row=r, column=6).number_format = "0.0%"
        for col in range(2, 6):
            ws.cell(row=r, column=col).number_format = "#,##0"
    notes = wb.create_sheet("Notes")
    notes.append(["Note", "Detail"])
    notes.append(["Units", "INR thousands (actuals)"])
    notes.append(["Fiscal year", "April to March"])
    wb.save(path)
    _inject_cached(path, "E2", "6430", "xl/worksheets/sheet1.xml")  # E3 stays uncached


def _revenue_fy2425(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monthly Revenue"
    ws.append(["Month", "Marigold Chai (INR)", "Total (INR)"])
    ws.append(["Apr", 3650000, 5660000])
    ws.append(["May", 3820000, 5910000])
    wb.save(path)


def _expenses(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Operating Costs"
    ws["A1"] = "Tarnwick Provisions - Expenses FY2025-26, Q2"
    ws.merge_cells("A1:F1")
    ws["A2"] = "All figures in INR thousands"
    ws.append(["Month", "Staff", "Logistics", "Marketing", "Total", "% of Revenue"])
    ws.append(["Jul", 2950, 1480, 620, 5050, 0.610])
    ws.append(["Aug", 3010, 1540, 700, 5250, 0.606])
    wb.save(path)


def _plain(path: Path) -> None:
    """No unit statement anywhere."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(["Month", "Orders Shipped"])
    ws.append(["Jul", 18420])
    ws.append(["Aug", 19860])
    ws.append(["Sep", "n/a"])
    wb.save(path)


def _many_rows(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Big"
    ws.append(["Id", "Value"])
    for i in range(1, 121):
        ws.append([i, i * 10])
    wb.save(path)


@dataclass
class Env:
    engine: Engine
    session_factory: object
    store: ContentAddressedStore
    reader: WorkbookReader
    tmp: Path
    chunks: dict = field(default_factory=dict)  # (file, sheet, row) -> chunk_id
    versions: dict = field(default_factory=dict)  # file -> version id
    sources: dict = field(default_factory=dict)  # file -> source id
    workspace_id: str = ""
    authorized_id: str = ""

    def add_workbook(self, name: str, build, *, status=VersionStatus.READY, source_status=SourceStatus.ACTIVE,
                     source_id: str | None = None, key: str | None = None) -> str:
        key = key or name
        path = self.tmp / "src" / name
        path.parent.mkdir(exist_ok=True)
        build(path)
        data = path.read_bytes()
        parsed = XlsxParser().parse("src", path)
        units, chunks = chunk_workbook(parsed)
        content_hash = self.store.put(data)
        with self.session_factory() as session:
            if source_id is None:
                source = Source(
                    workspace_id=self.workspace_id, authorized_source_id=self.authorized_id,
                    source_type="local_folder", path=str(path), status=source_status,
                )
                session.add(source)
                session.flush()
                source_id = source.id
            version = EvidenceVersion(
                source_id=source_id, file_path=str(path), content_hash=content_hash,
                byte_size=len(data), mime_type="application/vnd.ms-excel", parser_name="openpyxl",
                parser_version="3", status=status,
            )
            session.add(version)
            session.flush()
            recipe_id = compute_recipe_id(
                chunk_size=512, overlap=0, splitter="xlsx", parser_name="openpyxl", parser_version="3"
            )
            if session.get(ChunkRecipe, recipe_id) is None:
                session.add(ChunkRecipe(
                    id=recipe_id, chunk_size=512, overlap=0, splitter="xlsx",
                    parser_name="openpyxl", parser_version="3",
                ))
                session.flush()
            unit_ids = {}
            for u in units:
                row = EvidenceUnit(
                    evidence_version_id=version.id, unit_index=u.unit_index, heading=u.heading,
                    content_hash=u.content_hash, unit_kind=u.unit_kind, locator_json=u.locator_json,
                )
                session.add(row)
                session.flush()
                unit_ids[u.unit_index] = row.id
            for c in chunks:
                cid = compute_chunk_id(version.id, recipe_id, c.ordinal, c.content_hash)
                session.add(Chunk(
                    id=cid, source_id=source_id, evidence_version_id=version.id,
                    evidence_unit_id=unit_ids[c.evidence_unit_index], chunk_recipe_id=recipe_id,
                    ordinal=c.ordinal, heading=c.heading, text=c.text, content_hash=c.content_hash,
                    provenance=c.provenance,
                ))
                loc = json.loads(units[c.evidence_unit_index].locator_json)
                self.chunks[(key, loc["sheet"], int(re.search(r"\d+", loc["range"]).group()))] = cid
            session.commit()
            self.versions[key] = version.id
            self.sources[key] = source_id
        return version.id

    def chunk(self, file: str, sheet: str, row: int) -> str:
        return self.chunks[(file, sheet, row)]


@pytest.fixture()
def env(migrated_sqlite_engine: Engine, tmp_path: Path) -> Env:
    session_factory = get_session_factory(migrated_sqlite_engine)
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (tmp_path / "store" / sub).mkdir(parents=True)
    store = ContentAddressedStore(tmp_path / "store")
    with session_factory() as session:
        ws = Workspace(name="W")
        session.add(ws)
        session.flush()
        auth = AuthorizedSource(workspace_id=ws.id, scope_path="/data")
        session.add(auth)
        session.commit()
        e = Env(
            engine=migrated_sqlite_engine, session_factory=session_factory, store=store,
            reader=WorkbookReader(session_factory=session_factory, store=store), tmp=tmp_path,
            workspace_id=ws.id, authorized_id=auth.id,
        )
    e.add_workbook("Revenue-FY2025-26.xlsx", _revenue_fy2526)
    e.add_workbook("Revenue-FY2024-25.xlsx", _revenue_fy2425)
    e.add_workbook("Expenses-FY2025-26.xlsx", _expenses)
    e.add_workbook("Orders.xlsx", _plain)
    return e


REV = "Revenue-FY2025-26.xlsx"
OLD = "Revenue-FY2024-25.xlsx"
EXP = "Expenses-FY2025-26.xlsx"
MR = "Monthly Revenue"


def _ref(env: Env, row: int, **kw) -> dict:
    return {"chunk_id": env.chunk(REV, MR, row), **kw}


# ---------------------------------------------------------------------------
# A1 parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("B5", "B5"), ("A1:F12", "A1:F12"), ("$B$5:$D$7", "B5:D7"), ("f12:a1", "A1:F12"), (" c7 ", "C7")],
)
def test_parse_a1_range_valid(text, expected) -> None:
    assert parse_a1_range(text).text == expected


@pytest.mark.parametrize(
    "text", ["", "A:A", "3:3", "A1:", "A0", "A1:B2:C3", "A1,B2", "ZZZZ1", "XFE1", "A1048577", "Sheet1!A1", None, 5],
)
def test_parse_a1_range_invalid(text) -> None:
    with pytest.raises(WorkbookReadError):
        parse_a1_range(text)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_read_values_headers_formats_and_defaults_sheet_from_chunk(env: Env) -> None:
    out = env.reader.read(env.chunk(REV, MR, 4), "A4:F4").to_json()
    assert out["sheet"] == MR  # defaulted from the chunk's own sheet
    assert out["source"] == REV
    assert out["evidence_version_id"] == env.versions[REV]
    assert out["provenance"] == "extracted"
    assert [h["header"] for h in out["headers"]] == [
        "Month", "Marigold Chai", "Saffron Rusk", "Juniper Masala", "Total", "MoM Growth %"
    ]
    cells = {c["address"]: c for c in out["rows"][0]["cells"]}
    assert cells["B4"]["value"] == 4610 and cells["B4"]["header"] == "Marigold Chai"
    assert cells["B4"]["number_format"] == "#,##0"
    assert cells["F4"]["value"] == 0.167 and cells["F4"]["number_format"] == "0.0%"
    assert cells["F4"]["percent"] is True
    assert cells["A4"]["value"] == "Jun"
    assert out["coverage"] == {
        "cells_requested": 6, "cells_returned": 6, "rows_in_range": 1, "rows_returned": 1, "truncated": False,
        "sheet_used_range": "A1:F6",
    }


def test_explicit_sheet_overrides_chunk_sheet(env: Env) -> None:
    out = env.reader.read(env.chunk(REV, MR, 4), "A2:B3", sheet="Notes").to_json()
    assert out["sheet"] == "Notes"
    values = [c["value"] for r in out["rows"] for c in r["cells"]]
    assert values == ["Units", "INR thousands (actuals)", "Fiscal year", "April to March"]
    with pytest.raises(WorkbookReadError, match="not found"):
        env.reader.read(env.chunk(REV, MR, 4), "A1", sheet="Nope")


def test_blank_cell_is_null_and_distinct_from_zero(env: Env) -> None:
    out = env.reader.read(env.chunk(REV, MR, 2), "D2:D3").to_json()
    blank, zero = (r["cells"][0] for r in out["rows"])
    assert blank["value"] is None and blank["blank"] is True
    assert zero["value"] == 0 and "blank" not in zero


def test_formula_text_and_cached_value_and_missing_cache(env: Env) -> None:
    out = env.reader.read(env.chunk(REV, MR, 2), "E2:E3").to_json()
    cached, missing = (r["cells"][0] for r in out["rows"])
    assert cached["formula"] is True and cached["formula_text"] == "=SUM(B2:D2)" and cached["value"] == 6430
    assert missing["formula"] is True and missing["formula_text"] == "=SUM(B3:D3)"
    assert missing["value"] is None  # never 0
    assert missing["cached_value_missing"] is True and missing["note"] == "no cached value"
    assert "blank" not in missing


def test_hidden_rows_are_flagged(env: Env) -> None:
    out = env.reader.read(env.chunk(REV, MR, 4), "A5:E6").to_json()
    assert [(r["row"], r["hidden"]) for r in out["rows"]] == [(5, False), (6, True)]


def test_merged_title_header_is_reported_not_invented(env: Env) -> None:
    out = env.reader.read(env.chunk(EXP, "Operating Costs", 4), "A3:F4").to_json()
    assert all(h["header"] is None for h in out["headers"])
    assert "merged" in out["header_note"] and "Expenses FY2025-26, Q2" in out["header_note"]
    first = out["rows"][0]["cells"]
    assert first[0]["value"] == "Month" and first[1]["header"] is None
    assert out["rows"][1]["cells"][1]["value"] == 2950


def test_merged_cells_report_merge_membership(env: Env) -> None:
    out = env.reader.read(env.chunk(EXP, "Operating Costs", 4), "A1:C1").to_json()
    cells = out["rows"][0]["cells"]
    assert cells[0]["merged_range"] == "A1:F1" and cells[0]["value"].startswith("Tarnwick")
    assert cells[1]["merged_into"] == "A1:F1" and cells[1]["blank"] is True


def test_context_and_units_lines(env: Env) -> None:
    out = env.reader.read(env.chunk(REV, MR, 4), "B4").to_json()
    assert "FY2025-26" in out["context"]["fiscal_year"]
    assert "Units: INR thousands (actuals)" in out["context"]["units_and_scope"]  # verbatim
    assert out["units"] == "INR thousands"
    exp = env.reader.read(env.chunk(EXP, "Operating Costs", 4), "B4").to_json()
    assert "All figures in INR thousands" in exp["context"]["units_and_scope"]
    assert exp["units"] == "INR thousands"
    plain = env.reader.read(env.chunk("Orders.xlsx", "Orders", 2), "B2").to_json()
    assert plain["units"] == "not stated"


def test_units_in_header_text_and_percent(env: Env) -> None:
    out = env.reader.read(env.chunk(OLD, MR, 2), "B2").to_json()
    assert out["units"] == "not stated"  # no sheet-level statement ...
    assert out["rows"][0]["cells"][0]["units"] == "INR"  # ... but the header states it
    pct = env.reader.read(env.chunk(REV, MR, 4), "F4").to_json()
    assert pct["rows"][0]["cells"][0]["units"] == "percent"


def test_unit_helpers() -> None:
    assert context_units(["Units: INR thousands (actuals)", "Fiscal year: April to March"]) == "INR thousands"
    assert context_units(["Currency: INR", "Units: thousands"]) == "INR thousands"
    assert context_units(["in USD", "in INR"]).startswith("ambiguous")
    assert context_units([]) == "not stated"
    assert cell_units("Total (INR thousands)", False, "not stated") == "INR thousands"
    assert cell_units("Total (INR)", False, "INR thousands") == "INR thousands"  # header currency, sheet scale
    assert cell_units("Rate", True, "INR") == "percent"


def test_overlapping_row_chunks_and_labels(env: Env) -> None:
    out = env.reader.read(env.chunk(REV, MR, 3), "B3:D4").to_json()
    assert out["chunk_ids"] == [env.chunk(REV, MR, 3), env.chunk(REV, MR, 4)]
    assert out["citation_labels"] == [f"[{REV} #{c[:12]}]" for c in out["chunk_ids"]]
    cell_only = env.reader.read(env.chunk(REV, MR, 3), "B4").to_json()
    assert cell_only["chunk_ids"] == [env.chunk(REV, MR, 4)]


def test_caps_and_truncation_coverage(env: Env) -> None:
    env.add_workbook("Big.xlsx", _many_rows)
    cid = env.chunk("Big.xlsx", "Big", 5)
    out = env.reader.read(cid, "A1:B121").to_json()
    cov = out["coverage"]
    assert cov["rows_in_range"] == 121 and cov["rows_returned"] == MAX_ROWS
    assert cov["cells_requested"] == 242 and cov["cells_returned"] == MAX_ROWS * 2
    assert cov["truncated"] is True and "61-121" in cov["message"]
    assert len(out["rows"]) == MAX_ROWS and out["returned_range"] == "A1:B60"
    wide = env.reader.read(cid, "A1:Z30").to_json()  # 26 cols -> 7 rows by the 200-cell cap
    assert wide["coverage"]["cells_returned"] <= MAX_CELLS and wide["coverage"]["truncated"] is True
    with pytest.raises(WorkbookReadError, match="columns wide"):
        env.reader.read(cid, "A1:GR2", max_cells=100)


def test_generous_range_is_clipped_to_the_used_area(env: Env) -> None:
    """A model asking for A3:F100 on a 6-row sheet gets the real rows, not ~90
    blank ones (real-stack finding: those swamped the model)."""
    out = env.reader.read(env.chunk(REV, MR, 4), "A3:F100").to_json()
    assert [r["row"] for r in out["rows"]] == [3, 4, 5, 6]
    cov = out["coverage"]
    assert cov["clipped_to_used_range"] is True and cov["sheet_used_range"] == "A1:F6"
    assert cov["truncated"] is False and cov["rows_in_range"] == 98 and cov["rows_returned"] == 4
    assert "last used row (6)" in cov["clip_message"]
    # entirely below the data: only the first row, so a single blank cell still answers
    below = env.reader.read(env.chunk(REV, MR, 4), "B50:B60").to_json()
    assert [r["row"] for r in below["rows"]] == [50] and below["rows"][0]["cells"][0]["blank"] is True
    assert "entirely below" in below["coverage"]["clip_message"]
    # blank cells are compact; General format is omitted
    blank = env.reader.read(env.chunk(REV, MR, 2), "D2").to_json()["rows"][0]["cells"][0]
    assert blank == {"address": "D2", "value": None, "blank": True}
    assert "number_format" not in env.reader.read(env.chunk(REV, MR, 2), "A2").to_json()["rows"][0]["cells"][0]


def test_ineligible_chunks_are_refused(env: Env) -> None:
    with pytest.raises(WorkbookReadError, match="not found"):
        env.reader.read("chk_nope", "A1")
    cid = env.chunk(REV, MR, 4)
    with env.session_factory() as session:  # revoke the source
        session.get(Source, env.sources[REV]).status = SourceStatus.REVOKED
        session.commit()
    with pytest.raises(WorkbookReadError, match="no longer available"):
        env.reader.read(cid, "A1")
    pending = env.chunk(OLD, MR, 2)
    with env.session_factory() as session:
        session.get(EvidenceVersion, env.versions[OLD]).status = VersionStatus.PENDING
        session.commit()
    with pytest.raises(WorkbookReadError, match="no longer available"):
        env.reader.read(pending, "A1")


def test_reads_stored_blob_not_live_file(env: Env) -> None:
    cid = env.chunk(REV, MR, 4)
    (env.tmp / "src" / REV).unlink()  # the live file is gone
    out = env.reader.read(cid, "B4").to_json()
    assert out["rows"][0]["cells"][0]["value"] == 4610


def test_missing_blob_is_a_clear_error(env: Env) -> None:
    cid = env.chunk(OLD, MR, 2)
    with env.session_factory() as session:
        h = session.get(EvidenceVersion, env.versions[OLD]).content_hash
    env.store._object_path(h).unlink()
    with pytest.raises(WorkbookReadError, match="no longer available"):
        env.reader.read(cid, "A1")


def test_version_pinning_never_reads_a_superseded_version(env: Env) -> None:
    old_chunk = env.chunk(REV, MR, 4)
    with env.session_factory() as session:
        session.get(EvidenceVersion, env.versions[REV]).status = VersionStatus.SUPERSEDED
        session.commit()

    def changed(path: Path) -> None:
        _revenue_fy2526(path)
        wb = openpyxl.load_workbook(path)
        wb[MR]["B4"] = 9999
        wb.save(path)

    env.add_workbook(REV, changed, source_id=env.sources[REV], key="v2")
    with pytest.raises(WorkbookReadError, match="no longer available"):
        env.reader.read(old_chunk, "B4")
    new = env.reader.read(env.chunk("v2", MR, 4), "B4").to_json()
    assert new["rows"][0]["cells"][0]["value"] == 9999
    assert new["evidence_version_id"] == env.versions["v2"]


def test_non_spreadsheet_chunk_is_refused(env: Env, tmp_path: Path) -> None:
    with env.session_factory() as session:
        src = session.get(Source, env.sources["Orders.xlsx"])
        v = EvidenceVersion(
            source_id=src.id, file_path="/data/memo.docx", content_hash=env.store.put(b"docx bytes"),
            byte_size=10, parser_name="docling", parser_version="1", status=VersionStatus.READY,
        )
        session.add(v)
        session.flush()
        unit = EvidenceUnit(evidence_version_id=v.id, unit_index=0, heading="H", content_hash="x" * 64)
        session.add(unit)
        session.flush()
        recipe = session.query(ChunkRecipe).first()
        session.add(Chunk(
            id="chk_doc", source_id=src.id, evidence_version_id=v.id, evidence_unit_id=unit.id,
            chunk_recipe_id=recipe.id, ordinal=0, heading="H", text="t", content_hash="y" * 64,
        ))
        session.commit()
    with pytest.raises(WorkbookReadError, match="not from a spreadsheet"):
        env.reader.read("chk_doc", "A1")


# ---------------------------------------------------------------------------
# read_range tool wrapper
# ---------------------------------------------------------------------------


def test_read_range_tool_returns_json_and_error_json(env: Env) -> None:
    tool = make_read_range_tool(reader=env.reader)
    ok = json.loads(tool.invoke({"chunk_id": env.chunk(REV, MR, 4), "range": "B4"}))
    assert ok["rows"][0]["cells"][0]["value"] == 4610
    bad = json.loads(tool.invoke({"chunk_id": "chk_nope", "range": "B4"}))
    assert "error" in bad
    bad_range = json.loads(tool.invoke({"chunk_id": env.chunk(REV, MR, 4), "range": "A:A"}))
    assert "error" in bad_range
    missing_arg = json.loads(tool.invoke({"chunk_id": env.chunk(REV, MR, 4)}))
    assert "error" in missing_arg


# ---------------------------------------------------------------------------
# calculate
# ---------------------------------------------------------------------------


def _calc(env: Env, op: str, refs: list[dict], round_to=2) -> dict:
    return calculate(env.reader, op, refs, round_to)


def test_sum_over_range_and_exact_inputs(env: Env) -> None:
    out = _calc(env, "sum", [_ref(env, 4, range="B2:B5")])
    assert out["result"] == 4120 + 4380 + 4610 + 4725 == 17835
    assert out["result_text"] == "17835.00"
    assert out["provenance"] == "derived" and out["operation"] == "sum"
    assert out["units"] == "INR thousands"
    assert [i["address"] for i in out["inputs"]] == ["B2", "B3", "B4", "B5"]
    assert out["inputs"][0] == {
        "ref": 1, "source": REV, "sheet": MR, "address": "B2", "value": 4120,
        "header": "Marigold Chai", "units": "INR thousands", "number_format": "#,##0",
    }
    assert out["expression"].endswith("= 17835.00")
    assert out["chunk_ids"] == [env.chunk(REV, MR, r) for r in (2, 3, 4, 5)]
    assert out["citation_labels"][0] == f"[{REV} #{env.chunk(REV, MR, 2)[:12]}]"


def test_exact_decimal_division_and_rounding(env: Env) -> None:
    refs = [_ref(env, 4, cell="E4"), _ref(env, 4, cell="E5"), _ref(env, 4, cell="B2")]
    assert _calc(env, "average", refs)["result_text"] == "6793.33"  # (7980 + 8280 + 4120) / 3
    ratio = _calc(env, "ratio", [_ref(env, 4, cell="E5"), _ref(env, 4, cell="B4")], 4)
    assert ratio["result_text"] == "1.7961"  # 8280 / 4610 = 1.79609...
    # 21245 / 3 -> 7081.67 and half-up (not banker's / binary-float) rounding
    from decimal import Decimal

    from docket.services.agent.calculator import _compute, _quantize

    total, _ = _compute("average", [Decimal(7980), Decimal(8280), Decimal(4985)])
    assert str(_quantize(total, 2)) == "7081.67"
    assert str(_quantize(Decimal("2.675"), 2)) == "2.68" and str(_quantize(Decimal("0.125"), 2)) == "0.13"


def test_pct_change_sign_and_rounding(env: Env) -> None:
    down = _calc(env, "pct_change", [_ref(env, 4, cell="E4"), _ref(env, 4, cell="E5")])  # 7980 -> 8280
    assert down["result"] == 3.76 and down["result_text"] == "3.76"
    drop = _calc(env, "pct_change", [_ref(env, 4, cell="E5"), _ref(env, 4, cell="E4")])  # 8280 -> 7980
    assert drop["result"] == -3.62 and drop["result_text"] == "-3.62"  # -3.6231... half-up
    assert drop["units"] == "percent"
    assert drop["expression"].startswith("(7980 - 8280) / 8280 * 100")


def test_difference_ordering_ratio_min_max_count(env: Env) -> None:
    a, b = _ref(env, 4, cell="B4"), _ref(env, 4, cell="B3")  # 4610, 4380
    assert _calc(env, "difference", [a, b])["result"] == 230
    assert _calc(env, "difference", [b, a])["result"] == -230
    ratio = _calc(env, "ratio", [a, b])
    assert ratio["result_text"] == "1.05" and ratio["units"] == "unitless"
    rng = _ref(env, 4, range="B2:B5")
    assert _calc(env, "min", [rng])["result"] == 4120
    assert _calc(env, "max", [rng])["result"] == 4725
    count = _calc(env, "count", [rng])
    assert count["result"] == 4 and count["result_text"] == "4"


def test_round_to_variants(env: Env) -> None:
    refs = [_ref(env, 4, cell="E4"), _ref(env, 4, cell="B2"), _ref(env, 4, cell="B3")]
    assert _calc(env, "average", refs, 0)["result_text"] == "5493"  # 16480 / 3 = 5493.33
    assert _calc(env, "average", refs, None)["result_text"] == "5493.3333333333"
    with pytest.raises(CalculationError, match="round_to"):
        _calc(env, "sum", refs, 11)
    with pytest.raises(CalculationError, match="round_to"):
        _calc(env, "sum", refs, "2")  # type: ignore[arg-type]


def test_division_by_zero_and_zero_is_a_value_not_blank(env: Env) -> None:
    zero, one = _ref(env, 3, cell="D3"), _ref(env, 4, cell="D4")  # 0 and 780
    with pytest.raises(CalculationError, match="denominator"):
        _calc(env, "ratio", [one, zero])
    with pytest.raises(CalculationError, match="starting value"):
        _calc(env, "pct_change", [zero, one])
    assert _calc(env, "sum", [zero, one])["result"] == 780  # a literal 0 is a real value


def test_mixed_units_are_refused(env: Env) -> None:
    thousands = _ref(env, 4, cell="B4")
    inr = {"chunk_id": env.chunk(OLD, MR, 2), "cell": "B2"}
    for op in ("sum", "average", "min", "max"):
        with pytest.raises(CalculationError, match="different stated units"):
            _calc(env, op, [thousands, inr])
    with pytest.raises(CalculationError, match="different stated units"):
        _calc(env, "difference", [thousands, inr])
    with pytest.raises(CalculationError, match="different stated units"):
        _calc(env, "ratio", [thousands, inr])
    with pytest.raises(CalculationError, match="different stated units"):
        _calc(env, "pct_change", [thousands, inr])
    err = safe_calculate(env.reader, "sum", [thousands, inr])
    assert "INR thousands" in err["error"] and "INR (" in err["error"]
    # same-unit refs from different workbooks/sheets still work
    exp = {"chunk_id": env.chunk(EXP, "Operating Costs", 4), "cell": "E4"}
    assert _calc(env, "sum", [thousands, exp])["result"] == 4610 + 5050


def test_partially_stated_units_are_refused_and_unstated_is_reported(env: Env) -> None:
    orders = {"chunk_id": env.chunk("Orders.xlsx", "Orders", 2), "range": "B2:B3"}
    out = _calc(env, "sum", [orders])
    assert out["units"] == "not stated" and out["result"] == 38280
    assert any("does not state units" in w for w in out["warnings"])
    with pytest.raises(CalculationError, match="different stated units|only some"):
        _calc(env, "sum", [orders, _ref(env, 4, cell="B4")])
    ratio = _calc(env, "ratio", [orders | {"range": None, "cell": "B3"}, orders | {"range": None, "cell": "B2"}])
    assert ratio["units"] == "unitless"


def test_percent_cells(env: Env) -> None:
    out = _calc(env, "average", [_ref(env, 4, range="F3:F5")], 4)
    assert out["units"] == "percent" and out["result"] == pytest.approx((0.063 + 0.167 - 0.04) / 3, abs=1e-4)
    assert out["result_percent_text"] == "6.3333%"
    with pytest.raises(CalculationError, match="different stated units|only some"):
        _calc(env, "sum", [_ref(env, 4, range="F3:F5"), _ref(env, 4, cell="B4")])


def test_text_blank_and_missing_cache_cells_are_rejected_naming_the_cell(env: Env) -> None:
    with pytest.raises(CalculationError, match=r"Monthly Revenue!A4.*not a number"):
        _calc(env, "sum", [_ref(env, 4, cell="A4")])
    with pytest.raises(CalculationError, match=r"Monthly Revenue!D2.*blank"):
        _calc(env, "sum", [_ref(env, 4, cell="D2")])
    with pytest.raises(CalculationError, match=r"Monthly Revenue!E3.*no cached value"):
        _calc(env, "sum", [_ref(env, 4, cell="E3")])
    with pytest.raises(CalculationError, match=r"Monthly Revenue!E3.*no cached value"):
        _calc(env, "sum", [_ref(env, 4, range="E2:E4")])  # also inside a range
    with pytest.raises(CalculationError, match="A4"):
        _calc(env, "sum", [_ref(env, 4, range="A4:B4")])  # text inside a range
    orders = {"chunk_id": env.chunk("Orders.xlsx", "Orders", 2), "range": "B2:B4"}
    with pytest.raises(CalculationError, match=r"Orders!B4.*n/a"):
        _calc(env, "sum", [orders])


def test_blank_inside_a_range_is_skipped_and_reported_never_zero(env: Env) -> None:
    out = _calc(env, "average", [_ref(env, 4, range="D2:D5")])  # blank, 0, 780, 915
    assert out["input_count"] == 3 and out["result"] == pytest.approx(565.0)
    assert out["blank_cells_skipped"] == [f"{REV} > {MR}!D2"]
    assert any("blank" in w for w in out["warnings"])


def test_hidden_row_cells_are_warned(env: Env) -> None:
    out = _calc(env, "sum", [_ref(env, 4, range="E4:E6")])  # row 6 is the hidden memo row
    assert out["result"] == 7980 + 8280 + 1250
    assert any("hidden rows" in w and "E6" in w for w in out["warnings"])
    assert out["inputs"][-1]["hidden_row"] is True


def test_validation_errors(env: Env) -> None:
    ref = _ref(env, 4, cell="B4")
    with pytest.raises(CalculationError, match="unknown operation"):
        _calc(env, "median", [ref])
    with pytest.raises(CalculationError, match="exactly two"):
        _calc(env, "difference", [ref])
    with pytest.raises(CalculationError, match="exactly two"):
        _calc(env, "ratio", [ref, ref, ref])
    with pytest.raises(CalculationError, match="non-empty"):
        _calc(env, "sum", [])
    with pytest.raises(CalculationError, match="exactly one of"):
        _calc(env, "sum", [{"chunk_id": env.chunk(REV, MR, 4)}])
    with pytest.raises(CalculationError, match="exactly one of"):
        _calc(env, "sum", [_ref(env, 4, cell="B4", range="B4:B5")])
    with pytest.raises(CalculationError, match="single cell"):
        _calc(env, "difference", [_ref(env, 4, range="B2:B3"), ref])
    with pytest.raises(CalculationError, match="too many refs"):
        _calc(env, "sum", [ref] * 201)
    with pytest.raises(CalculationError, match="each ref"):
        _calc(env, "sum", ["B4"])  # type: ignore[list-item]


def test_too_many_input_cells_in_ranges(env: Env) -> None:
    env.add_workbook("Big.xlsx", _many_rows)
    cid = env.chunk("Big.xlsx", "Big", 5)
    ok = calculate(env.reader, "sum", [{"chunk_id": cid, "range": "B2:B121"}])
    assert ok["input_count"] == 120 and ok["result"] == 10 * sum(range(1, 121))
    with pytest.raises(WorkbookReadError):
        calculate(env.reader, "sum", [{"chunk_id": cid, "range": "A2:B121"}])  # 240 cells
    with pytest.raises(WorkbookReadError):
        calculate(env.reader, "sum", [{"chunk_id": cid, "range": "B2:B121"}, {"chunk_id": cid, "range": "B2:B100"}])


def test_refs_are_resolved_by_the_tool_so_invented_cells_fail(env: Env) -> None:
    out = safe_calculate(env.reader, "sum", [_ref(env, 4, cell="B99")])
    assert "blank" in out["error"] and "B99" in out["error"]  # a cell that is not there is blank, not a number
    assert "error" in safe_calculate(env.reader, "sum", [{"chunk_id": "chk_invented", "cell": "B4"}])
    assert "error" in safe_calculate(env.reader, "sum", [_ref(env, 4, cell="B4", sheet="Nope")])
    # a model-supplied value is simply not an accepted field
    out = calculate(env.reader, "sum", [_ref(env, 4, cell="B4", value=1)])
    assert out["result"] == 4610


def test_ineligible_chunk_in_calculate(env: Env) -> None:
    cid = env.chunk(REV, MR, 4)
    with env.session_factory() as session:
        session.get(Source, env.sources[REV]).status = SourceStatus.REVOKED
        session.commit()
    out = safe_calculate(env.reader, "sum", [{"chunk_id": cid, "cell": "B4"}])
    assert "no longer available" in out["error"]


def test_calculate_tool_wrapper(env: Env) -> None:
    tool = make_calculate_tool(reader=env.reader)
    out = json.loads(tool.invoke({
        "operation": "sum",
        "refs": [{"chunk_id": env.chunk(REV, MR, 4), "range": "B2:B3"}],
    }))
    assert out["result"] == 8500 and out["provenance"] == "derived"
    assert "error" in json.loads(tool.invoke({"operation": "median", "refs": []}))
    assert "error" in json.loads(tool.invoke({"operation": "sum"}))  # missing refs: validation error as JSON
    schema = tool.args_schema.model_json_schema()
    assert set(schema["properties"]) == {"operation", "refs", "round_to"}
