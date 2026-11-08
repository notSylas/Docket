"""Deterministically generate the G6 interlinked three-file corpus and its gold set.

Everything is fictional: "Marrowfield Civil Works Ltd", the "Kestrel Port
Authority" and contract KBR-2026-014 do not exist. The three files are linked on
purpose, for cross-document retrieval testing:

  budget.xlsx    sheets Project Budgets (line IDs BL-101..BL-107, USD thousand),
                 Vendor Rates (USD, mixed units, keyed to a budget line),
                 Headcount (FTE per quarter, keyed to a budget line), Notes
  contract.pdf   numbered clauses with rates, caps and dates; clauses cite budget
                 lines by ID and name (reportlab, real text layer)
  memo.docx      approval memos AM-01..AM-05 that override a specific contract
                 number (the memo says "replaces the figure stated in the
                 clause" and does NOT repeat the old number) and cite a budget
                 row and a clause number

Values are literal numbers in the tables below. Every expected gold value is
READ back from the generated files (cells via openpyxl, contract text via pypdf,
memo paragraphs via python-docx) or computed from those reads. Gold spans are
verbatim substrings of that extracted text.

Needs reportlab, pypdf, python-docx, python-pptx, openpyxl, pyyaml (a scratch
venv; do NOT install into backend/.venv). Rebuild from anywhere:

    <venv>/bin/python eval-public/build_synthetic_corpus_g6.py

It rewrites eval-public/corpus-synthetic-g6/ and eval-public/gold-g6.yaml and is
byte-for-byte reproducible (fixed timestamps, reportlab invariant mode).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sys
import zipfile
from pathlib import Path

import yaml
from docx import Document
from openpyxl import Workbook
from openpyxl.styles import Font
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_gold_g2 import Gold, assign_splits, doc_paragraphs, find, load_tables, text_span  # noqa: E402
from build_synthetic_corpus_g1 import num_re, rnd  # noqa: E402,F401

OUT = HERE / "corpus-synthetic-g6"
GOLD = HERE / "gold-g6.yaml"
FIXED = dt.datetime(2026, 7, 10, 9, 0, 0)
ZIP_STAMP = (2026, 7, 10, 9, 0, 0)
COMPANY = "Marrowfield Civil Works Ltd"
FB, FC, FM = "budget.xlsx", "contract.pdf", "memo.docx"

# --------------------------------------------------------------------------
# Data tables (the only hand-typed numbers)
# --------------------------------------------------------------------------

# Line ID, Budget (USD k), Line Item, Category, Period
BUDGET = [
    ("BL-101", 120, "Site Survey", "Survey"),
    ("BL-102", 840, "Steel Fabrication", "Fabrication"),
    ("BL-103", 310, "Crane Hire", "Equipment"),
    ("BL-104", 95, "Geotechnical Testing", "Testing"),
    ("BL-105", 48, "Safety Training", "Training"),
    ("BL-106", 150, "Environmental Monitoring", "Monitoring"),
    ("BL-107", 210, "Commissioning", "Handover"),
]
PERIOD = "FY2026"
# Rate ID, Vendor, Rate (USD), Unit, Budget Line
VENDORS = [
    ("VR-01", "Tallis Cranes", 2250, "per day", "BL-103"),
    ("VR-02", "Ironwell Fabricators", 1720, "per tonne", "BL-102"),
    ("VR-03", "Greywater Surveys", 95, "per hour", "BL-101"),
    ("VR-04", "Safeguard Trainers", 38, "per hour", "BL-105"),
    ("VR-05", "Eastfen Labs", 1150, "per sample", "BL-104"),
]
# Team, Q1..Q4 FTE, Budget Line
HEADCOUNT = [
    ("Survey Crew", 6, 6, 2, 0, "BL-101"),
    ("Fabrication", 14, 18, 22, 16, "BL-102"),
    ("Site Safety", 4, 4, 4, 4, "BL-105"),
    ("Environmental", 2, 3, 3, 3, "BL-106"),
    ("Commissioning", 0, 0, 4, 9, "BL-107"),
]
CONTRACT_NO = "KBR-2026-014"
CONTRACT_VALUE = 1900000  # USD

# slug -> clause number (structure, not an expected value)
CL = {"parties": "1.1", "effective": "1.2", "scope": "2.1", "completion": "2.3", "crane_budget": "3.1",
      "crane_rate": "3.2", "steel": "4.1", "training": "5.1", "env": "6.1", "payment": "7.1", "ld": "7.2",
      "value": "7.3", "amend": "8.1"}

CLAUSES = [
    ("1", "Parties and term", [
        (CL["parties"], f"This Contract {CONTRACT_NO} is made between {COMPANY} (the Contractor) and Kestrel Port Authority (the Client) for the Kestrel Bridge Retrofit."),
        (CL["effective"], "Effective Date. The Contract is signed on 15 January 2026 and takes effect on 1 February 2026."),
    ]),
    ("2", "Scope and completion", [
        (CL["scope"], "Scope. The Contractor shall retrofit the deck and bearings of the Kestrel Bridge and hand the structure over to the Client."),
        (CL["completion"], "Completion Date. The Works shall reach completion by 30 November 2026, after commissioning charged to budget line BL-107 Commissioning."),
    ]),
    ("3", "Crane hire", [
        (CL["crane_budget"], "Crane costs are charged to budget line BL-103 Crane Hire in the Project Budgets sheet of the budget workbook."),
        (CL["crane_rate"], "Crane day rate. The day rate for crane hire is USD 2,400 per day, capped at 120 crane days."),
    ]),
    ("4", "Steel fabrication", [
        (CL["steel"], "Steel cap. Fabricated steel is charged to budget line BL-102 Steel Fabrication at USD 1,850 per tonne, capped at 450 tonnes."),
    ]),
    ("5", "Safety", [
        (CL["training"], "Training hours. Every site worker must complete at least 40 hours of safety training, funded from budget line BL-105 Safety Training."),
    ]),
    ("6", "Environmental monitoring", [
        (CL["env"], "Monitoring cap. Costs charged to budget line BL-106 Environmental Monitoring are capped at USD 135,000."),
    ]),
    ("7", "Payment and damages", [
        (CL["payment"], "Payment terms. The Client pays within 45 days of invoice and retains 5% of each invoice as retention."),
        (CL["ld"], "Liquidated damages are USD 3,500 per day of delay, capped at 10% of the Contract Value."),
        (CL["value"], f"Contract Value. The Contract Value is USD {CONTRACT_VALUE:,}."),
    ]),
    ("8", "Amendments", [
        (CL["amend"], "No term of this Contract may be changed except by a signed approval memo that cites the number of the clause being amended."),
    ]),
]

# id, date, text
MEMOS = [
    ("AM-01", "12 March 2026",
     f"Clause {CL['crane_rate']} of Contract {CONTRACT_NO} is amended. The crane day rate is now USD 2,650 per day. "
     "This amendment replaces the figure stated in the clause and affects budget row BL-103 Crane Hire."),
    ("AM-02", "2 April 2026",
     f"Clause {CL['steel']} of Contract {CONTRACT_NO} is amended. The steel cap is now 520 tonnes. "
     "This amendment replaces the figure stated in the clause and affects budget row BL-102 Steel Fabrication. "
     "A transfer of USD 60 thousand from budget row BL-107 Commissioning to budget row BL-102 Steel Fabrication is approved."),
    ("AM-03", "20 May 2026",
     f"Clause {CL['env']} of Contract {CONTRACT_NO} is amended. The monitoring cap is now USD 160,000. "
     "This amendment replaces the figure stated in the clause and affects budget row BL-106 Environmental Monitoring."),
    ("AM-04", "15 June 2026",
     f"Clause {CL['completion']} of Contract {CONTRACT_NO} is amended. The Completion Date is now 15 January 2027. "
     "This amendment replaces the date stated in the clause and affects budget row BL-107 Commissioning."),
    ("AM-05", "1 July 2026",
     f"Clause {CL['training']} of Contract {CONTRACT_NO} is unchanged. Two additional Site Safety FTE are approved from Q3, "
     "charged to budget row BL-105 Safety Training."),
]

# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _save_wb(wb: Workbook, path: Path) -> None:
    wb.properties.creator = f"{COMPANY} (synthetic)"
    wb.properties.created = FIXED
    wb.properties.modified = FIXED
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _bold(ws) -> None:
    for cell in ws[1]:
        cell.font = Font(bold=True)


def build_budget(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Project Budgets"
    ws.append(["Line ID", "Line Item", "Budget (USD k)", "Category", "Period"])
    _bold(ws)
    for lid, amt, item, cat in BUDGET:
        ws.append([lid, item, amt, cat, PERIOD])
    ws.append(["TOTAL", "All lines", sum(b[1] for b in BUDGET), None, PERIOD])

    vr = wb.create_sheet("Vendor Rates")
    vr.append(["Rate ID", "Vendor", "Rate (USD)", "Unit", "Budget Line"])
    _bold(vr)
    for row in VENDORS:
        vr.append(list(row))

    hc = wb.create_sheet("Headcount")
    hc.append(["Team", "Q1 FTE", "Q2 FTE", "Q3 FTE", "Q4 FTE", "Budget Line"])
    _bold(hc)
    for row in HEADCOUNT:
        hc.append(list(row))

    n = wb.create_sheet("Notes")
    n.append(["Note", "Detail"])
    for r in [
        ("Budget units", "Project Budgets amounts are in USD k, meaning USD thousand"),
        ("Rate units", "Vendor Rates are in whole USD per the unit shown, not USD thousand"),
        ("Headcount units", "Headcount is in full-time equivalents (FTE) per fiscal quarter"),
        ("Fiscal year", "FY2026 runs from 1 February 2026 to 31 January 2027; Q1 is February to April"),
    ]:
        n.append(list(r))
    _save_wb(wb, path)


def build_contract(path: Path) -> None:
    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15, leading=19, spaceAfter=8)
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=8, spaceAfter=3)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=4)
    story = [Paragraph(f"Services Contract {CONTRACT_NO}: Kestrel Bridge Retrofit", h1),
             Paragraph(f"{COMPANY} and Kestrel Port Authority", body), Spacer(1, 6)]
    for num, title, items in CLAUSES:
        story.append(Paragraph(f"{num}. {title}", h2))
        for cn, text in items:
            story.append(Paragraph(f"{cn} {text}", body))
    doc = SimpleDocTemplate(str(path), pagesize=A4, invariant=1, title=f"Contract {CONTRACT_NO}",
                            author=f"{COMPANY} (synthetic)", creator="g6 builder", subject="Kestrel Bridge Retrofit",
                            leftMargin=56, rightMargin=56, topMargin=56, bottomMargin=56)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.build(story)


def build_memo(path: Path) -> None:
    doc = Document()
    cp = doc.core_properties
    cp.created = cp.modified = FIXED
    cp.author = f"{COMPANY} Programme Office (synthetic)"
    cp.last_modified_by = cp.author
    cp.revision = 1
    doc.add_heading(f"Approval Memo: Contract {CONTRACT_NO} amendments", level=1)
    doc.add_paragraph("To: Kestrel Bridge Retrofit steering group")
    doc.add_paragraph("From: Priya Vantongeren, Programme Director")
    doc.add_paragraph("Each approval below overrides the contract figure it names and is final once signed.")
    for mid, date, text in MEMOS:
        doc.add_paragraph(f"Approval {mid} ({date}): {text}")
    doc.save(path)


def _freeze_zip_timestamps(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".xlsx", ".docx"}:
            continue
        with zipfile.ZipFile(path) as zin:
            items = [(i.filename, zin.read(i.filename)) for i in zin.infolist()]
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name, data in items:
                if name == "docProps/core.xml":
                    data = re.sub(rb"(<dcterms:modified[^>]*>)[^<]*", rb"\g<1>2026-07-10T09:00:00Z", data)
                info = zipfile.ZipInfo(name, date_time=ZIP_STAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zout.writestr(info, data)


def build_corpus() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    build_budget(OUT / FB)
    build_contract(OUT / FC)
    build_memo(OUT / FM)
    _freeze_zip_timestamps(OUT)


# --------------------------------------------------------------------------
# Reading the generated files back
# --------------------------------------------------------------------------


def pdf_text(path: Path) -> str:
    return re.sub(r"\s+", " ", " ".join(p.extract_text() for p in PdfReader(path).pages)).strip()


def pat(*values) -> str:
    return "re:" + "|".join(num_re(v) for v in values)


def kpat(v: float) -> str:
    """USD-thousand value written either in thousands or in full dollars."""
    return pat(v, v * 1000)


def ival(s: str) -> int:
    return int(s.replace(",", ""))


def date_pat(s: str) -> str:
    d, m, y = s.split()
    return rf"re:\b{d}\s+{m.lower()}\s+{y}\b"


def build_gold() -> Gold:
    g = Gold("g6", "synthetic corpus eval-public/corpus-synthetic-g6 (Marrowfield)",
             generator="eval-public/build_synthetic_corpus_g6.py", tag="g6 category")
    T = load_tables(OUT / FB)
    PB, VR, HC, NT = T["Project Budgets"], T["Vendor Rates"], T["Headcount"], T["Notes"]
    C = pdf_text(OUT / FC)
    memo = doc_paragraphs(OUT / FM)

    def cm(pattern: str) -> re.Match:
        m = re.search(pattern, C)
        assert m, pattern
        return m

    def cspan(pattern: str) -> str:
        m = cm(pattern)
        s = m.start()
        seg = C[s : s + 60]
        if s + 60 < len(C) and C[s + 60] != " ":
            seg = seg[: seg.rfind(" ")]
        # Stop at the end of the clause sentence: the PDF text continues into the next
        # numbered heading, which can fall in a different indexed chunk.
        brk = re.search(r"\.(?=\s+\d)", seg[len(m.group(0)):])
        if brk:
            seg = seg[: len(m.group(0)) + brk.end()]
        seg = seg.strip()
        assert 20 <= len(seg) <= 60 and m.group(0) in seg, (pattern, seg)
        return seg

    def mm(mid: str, pattern: str) -> re.Match:
        return find(memo, rf"Approval {mid} \(.*?{pattern}")

    def mspan(mid: str, pattern: str) -> str:
        m = re.search(pattern, next(p for p in memo if p.startswith(f"Approval {mid} (")))
        assert m, (mid, pattern)
        return text_span(m)

    # ---- contract values (read from the PDF text)
    crane_old = ival(cm(r"day rate for crane hire is USD ([\d,]+) per day").group(1))
    crane_days = int(cm(r"capped at (\d+) crane days").group(1))
    steel_rate = ival(cm(r"Steel Fabrication at USD ([\d,]+) per tonne").group(1))
    steel_old = int(cm(r"capped at (\d+) tonnes").group(1))
    hours = int(cm(r"at least (\d+) hours").group(1))
    env_old = ival(cm(r"Environmental Monitoring are capped at USD ([\d,]+)").group(1))
    done_old = cm(r"reach completion by (\d+ \w+ \d{4})").group(1)
    retention = int(cm(r"retains (\d+)% of each invoice").group(1))
    cvalue = ival(cm(r"Contract Value is USD ([\d,]+)").group(1))
    # ---- memo values (read from the docx)
    crane_new = ival(mm("AM-01", r"now USD ([\d,]+) per day").group(1))
    steel_new = int(mm("AM-02", r"steel cap is now (\d+) tonnes").group(1))
    xfer = ival(mm("AM-02", r"transfer of USD ([\d,]+) thousand").group(1))
    env_new = ival(mm("AM-03", r"now USD ([\d,]+)\.").group(1))
    done_new = mm("AM-04", r"Completion Date is now (\d+ \w+ \d{4})").group(1)
    extra_fte = {"Two": 2}[mm("AM-05", r"(Two) additional Site Safety FTE").group(1)]
    # ---- workbook values
    bud = lambda lid: PB.val(lid, "Budget")  # noqa: E731
    total_budget = PB.val("TOTAL", "Budget")
    assert total_budget == sum(b[1] for b in BUDGET)
    vend = lambda rid: (VR.val(rid, "Vendor"), VR.val(rid, "Rate"), VR.val(rid, "Unit"))  # noqa: E731
    team_of = lambda lid: next(r for r in HC.rows if r[5] == lid)  # noqa: E731

    def bspan(lid):
        return PB.span(lid, "Line Item", "Budget")

    # ---- single-file controls (one per file and per sheet)
    g.q("ctl", "budget-line", "table_lookup",
        "On the Project Budgets sheet, what is the budget for line BL-105 Safety Training, in USD thousand?",
        must=[kpat(bud("BL-105"))], spans=[bspan("BL-105")], docs=[FB], origin=f"BL-105 = {bud('BL-105')} USD k")
    n, rate, unit = vend("VR-05")
    g.q("ctl", "vendor-rate", "table_lookup",
        "On the Vendor Rates sheet, what rate does Eastfen Labs charge, and per what unit?",
        must=[pat(rate), r"re:\bsample"], spans=[VR.span("VR-05", "Vendor", "Rate", "Unit")], docs=[FB],
        origin=f"{n} {rate} USD {unit}")
    fab = HC.val("Fabrication", "Q3 FTE")
    g.q("ctl", "headcount-q3", "table_lookup",
        "On the Headcount sheet, how many Fabrication FTE are planned for Q3?",
        must=[pat(fab)], spans=[HC.span("Fabrication", "Q3 FTE")], docs=[FB], origin=f"Fabrication Q3 FTE = {fab}")
    g.q("ctl", "contract-retention", "single_fact",
        "What percentage of each invoice does the Client retain as retention under the contract?",
        must=[pat(retention)], spans=[cspan(r"retains \d+% of each invoice as retention")], docs=[FC],
        origin=f"retention {retention}%")
    g.q("ctl", "memo-safety-quarter", "single_fact",
        "Which approval memo item adds Site Safety staff, and from which quarter?",
        must=["AM-05", r"re:\bq3\b|third quarter"], spans=[mspan("AM-05", r"Two additional Site Safety FTE are approved from Q3")],
        docs=[FM], origin="AM-05 adds Site Safety FTE from Q3")

    # ---- two-file joins
    g.q("join", "crane-clause-budget", "multi_doc",
        f"Clause {CL['crane_budget']} of the contract charges crane costs to a budget line. What is that line's budget in USD thousand?",
        must=[kpat(bud("BL-103"))], spans=[cspan(r"Crane costs are charged to budget line BL-103 Crane Hire"), bspan("BL-103")],
        docs=[FC, FB], origin=f"clause 3.1 -> BL-103 -> {bud('BL-103')} USD k")
    g.q("join", "steel-clause-category", "multi_doc",
        f"Clause {CL['steel']} of the contract charges fabricated steel to a budget line. What category and budget amount (USD thousand) does that line have?",
        must=[r"re:fabrication", kpat(bud("BL-102"))],
        spans=[cspan(r"Fabricated steel is charged to budget line BL-102"), PB.span("BL-102", "Line Item", "Budget")],
        docs=[FC, FB], origin=f"clause 4.1 -> BL-102 -> {PB.val('BL-102', 'Category')}, {bud('BL-102')} USD k")
    g.q("join", "training-hours-budget", "multi_doc",
        f"Clause {CL['training']} of the contract requires a minimum number of safety training hours per worker. How many hours, and what is the budget (USD thousand) of the line that funds it?",
        must=[pat(hours), kpat(bud("BL-105"))],
        spans=[cspan(r"Every site worker must complete at least \d+ hours"), bspan("BL-105")], docs=[FC, FB],
        origin=f"clause 5.1: {hours} hours; BL-105 = {bud('BL-105')} USD k")
    g.q("join", "commissioning-transfer", "multi_doc",
        "Approval AM-02 transfers money out of a commissioning budget row. How much is transferred, and what was that row's original budget in USD thousand?",
        must=[kpat(xfer), kpat(bud("BL-107"))],
        spans=[mspan("AM-02", r"transfer of USD [\d,]+ thousand from budget row BL-107"), bspan("BL-107")], docs=[FM, FB],
        origin=f"AM-02 transfer {xfer} thousand; BL-107 original {bud('BL-107')}")
    g.q("join", "safety-memo-clause-hours", "multi_doc",
        "Approval AM-05 names a contract clause on training. What minimum training hours per worker does that clause set?",
        must=[pat(hours)],
        spans=[mspan("AM-05", rf"Clause {CL['training']} of Contract"), cspan(r"at least \d+ hours of safety training")],
        docs=[FM, FC], origin=f"AM-05 cites clause 5.1 -> {hours} hours (clause unchanged)")
    g.q("join", "completion-original", "multi_doc",
        "Approval AM-04 amends a contract clause. Which clause number is that, and what completion date did the contract originally state in it?",
        must=[r"re:\b" + re.escape(CL["completion"]) + r"\b", date_pat(done_old)],
        spans=[mspan("AM-04", r"Clause [\d.]+ of Contract"), cspan(r"The Works shall reach completion by")], docs=[FM, FC],
        origin=f"AM-04 -> clause {CL['completion']}, original {done_old} (memo does not repeat it)")
    diff = cvalue / 1000 - total_budget
    g.q("join", "contract-vs-total-budget", "multi_doc",
        "By how much, in USD thousand, does the Contract Value exceed the total of all project budget lines?",
        must=[kpat(diff)], spans=[cspan(r"Contract Value is USD [\d,]+"), PB.span("TOTAL", "Line Item", "Budget")], docs=[FC, FB],
        origin=f"derived: {cvalue / 1000:g} - {total_budget} = {diff:g} USD k")
    ss = HC.val("Site Safety", "Q3 FTE")
    g.q("join", "site-safety-q3-after-memo", "multi_doc",
        "How many Site Safety FTE are planned for Q3 once the extra staff approved in AM-05 are added to the Headcount sheet figure?",
        must=[pat(ss + extra_fte)], spans=[HC.span("Site Safety", "Q3 FTE"), mspan("AM-05", r"Two additional Site Safety FTE are approved from Q3")],
        docs=[FB, FM], origin=f"derived: {ss} + {extra_fte} = {ss + extra_fte}")

    # ---- three-file chains (memo -> budget row -> sheet -> contract)
    v1 = vend("VR-01")
    g.q("chain", "crane-vendor-cap", "multi_doc",
        "Approval AM-01 changed a rate that applies to one budget row. Which vendor serves that row on the Vendor Rates sheet, and how many crane days does the contract cap the hire at?",
        must=[r"re:tallis", pat(crane_days)],
        spans=[mspan("AM-01", r"affects budget row BL-103 Crane Hire"), VR.span("VR-01", "Vendor", "Rate"), cspan(r"capped at \d+ crane days")],
        docs=[FM, FB, FC], origin=f"AM-01 -> BL-103 -> {v1[0]} (VR-01) -> contract cap {crane_days} days")
    v2 = vend("VR-02")
    g.q("chain", "steel-vendor-budget-cap", "multi_doc",
        "Approval AM-02 changes the steel cap. Which vendor serves the affected budget row on the Vendor Rates sheet, what is that row's budget in USD thousand, and what is the new tonne cap?",
        must=[r"re:ironwell", kpat(bud("BL-102")), pat(steel_new)],
        spans=[mspan("AM-02", r"steel cap is now \d+ tonnes"), VR.span("VR-02", "Vendor", "Rate"), bspan("BL-102")],
        docs=[FM, FB, FC] , origin=f"AM-02 -> BL-102 -> {v2[0]}, budget {bud('BL-102')}, new cap {steel_new}")
    et = team_of("BL-106")
    g.q("chain", "env-team-original-cap", "multi_doc",
        "Approval AM-03 changes the environmental monitoring cap. Which team on the Headcount sheet is charged to the affected budget row, how many of its FTE are planned in Q2, and what cap did the contract originally state?",
        must=[r"re:environmental", pat(et[2]), kpat(env_old / 1000)],
        spans=[mspan("AM-03", r"affects budget row BL-106 Environmental Monitoring"), HC.span("Environmental", "Q2 FTE"),
               cspan(rf"capped at USD {env_old:,}")],
        docs=[FM, FB, FC], origin=f"AM-03 -> BL-106 -> team {et[0]} Q2 FTE {et[2]}; original cap {env_old}")
    ct = team_of("BL-107")
    g.q("chain", "commissioning-q4-original-date", "multi_doc",
        "Approval AM-04 extends the completion date. Which Headcount sheet team belongs to the budget row it names, how many of its FTE are planned in Q4, and what was the contract's original completion date?",
        must=[r"re:commissioning", pat(ct[4]), date_pat(done_old)],
        spans=[mspan("AM-04", r"affects budget row BL-107 Commissioning"), HC.span("Commissioning", "Q4 FTE"),
               cspan(r"The Works shall reach completion by")],
        docs=[FM, FB, FC], origin=f"AM-04 -> BL-107 -> team {ct[0]} Q4 FTE {ct[4]}; original date {done_old}")
    g.q("chain", "safety-budget-hours", "multi_doc",
        "Approval AM-05 adds staff charged to a budget row. What is that row's budget in USD thousand, and how many training hours per worker does the contract clause on that row require?",
        must=[kpat(bud("BL-105")), pat(hours)],
        spans=[mspan("AM-05", r"charged to budget row BL-105 Safety Training"), bspan("BL-105"), cspan(r"at least \d+ hours of safety training")],
        docs=[FM, FB, FC], origin=f"AM-05 -> BL-105 -> {bud('BL-105')} USD k; clause 5.1 {hours} hours")
    remaining = bud("BL-107") - xfer  # xfer is already USD thousand
    g.q("chain", "transfer-source-remaining", "multi_doc",
        "Approval AM-02 moves money into steel fabrication from another budget row. Which row is the source, what budget (USD thousand) remains on it after the transfer, and what per-tonne rate does the steel clause of the contract charge?",
        must=[r"re:commissioning", kpat(remaining), pat(steel_rate)],
        spans=[mspan("AM-02", r"transfer of USD [\d,]+ thousand from budget row BL-107"), bspan("BL-107"), cspan(r"at USD [\d,]+ per tonne")],
        docs=[FM, FB, FC], origin=f"AM-02 source BL-107: {bud('BL-107')} - {xfer} = {remaining:g}; contract {steel_rate} per tonne")

    # ---- amended values: the memo overrides the contract figure
    g.q("amend", "crane-rate-now", "multi_doc",
        "What crane day rate, in USD per day, is currently in force for the Kestrel Bridge contract?",
        must=[pat(crane_new)], spans=[mspan("AM-01", r"The crane day rate is now USD [\d,]+ per day"), cspan(r"day rate for crane hire is USD [\d,]+ per day")],
        docs=[FM, FC], origin=f"memo AM-01 {crane_new} overrides contract clause 3.2 ({crane_old})")
    g.q("amend", "steel-cap-now", "multi_doc",
        "What is the current cap, in tonnes, on fabricated steel charged under the contract?",
        must=[pat(steel_new)], spans=[mspan("AM-02", r"The steel cap is now \d+ tonnes"), cspan(r"capped at \d+ tonnes")],
        docs=[FM, FC], origin=f"memo AM-02 {steel_new} overrides contract clause 4.1 ({steel_old})")
    g.q("amend", "env-cap-now", "multi_doc",
        "What is the current cap, in USD, on environmental monitoring costs under the contract?",
        must=[pat(env_new, env_new / 1000)], spans=[mspan("AM-03", r"The monitoring cap is now USD [\d,]+"), cspan(r"BL-106 Environmental Monitoring are capped at USD")],
        docs=[FM, FC], origin=f"memo AM-03 {env_new} overrides contract clause 6.1 ({env_old})")
    g.q("amend", "completion-now", "multi_doc",
        "What is the current Completion Date for the Kestrel Bridge works?",
        must=[date_pat(done_new)], spans=[mspan("AM-04", r"The Completion Date is now \d+ \w+ \d{4}"), cspan(r"The Works shall reach completion by")],
        docs=[FM, FC], origin=f"memo AM-04 {done_new} overrides contract clause 2.3 ({done_old})")

    # ---- derived numbers (amended values, contract caps, budget rows)
    crane_max = crane_new * crane_days
    over1 = crane_max / 1000 - bud("BL-103")
    g.q("derive", "crane-max-vs-budget", "numeric",
        "At the currently approved crane day rate and the contract's cap on crane days, what is the maximum crane hire cost in USD, and by how much, in USD thousand, does it exceed the Crane Hire budget line?",
        must=[pat(crane_max, crane_max / 1000), kpat(over1)],
        spans=[mspan("AM-01", r"The crane day rate is now USD [\d,]+ per day"), cspan(r"capped at \d+ crane days"), bspan("BL-103")],
        docs=[FM, FC, FB], origin=f"derived: {crane_new} x {crane_days} = {crane_max}; minus {bud('BL-103')}k = {over1:g}k")
    steel_max = steel_new * steel_rate
    over2 = steel_max / 1000 - bud("BL-102")
    g.q("derive", "steel-max-vs-budget", "numeric",
        "At the contract's per-tonne rate and the currently approved tonne cap, what is the maximum steel fabrication cost in USD, and by how much, in USD thousand, does it exceed the original Steel Fabrication budget on the Project Budgets sheet (before any transfer)?",
        must=[pat(steel_max, steel_max / 1000), kpat(over2)],
        spans=[mspan("AM-02", r"The steel cap is now \d+ tonnes"), cspan(r"at USD [\d,]+ per tonne"), bspan("BL-102")],
        docs=[FM, FC, FB], origin=f"derived: {steel_new} x {steel_rate} = {steel_max}; minus {bud('BL-102')}k = {over2:g}k")

    # ---- unanswerable but plausible
    blob = (C + " " + " ".join(memo) + " " + " ".join(str(c) for t in T.values() for r in t.rows for c in r)).lower()
    for absent in ("insurance", "terminat", "actual", "bl-108", "fy2027"):
        assert absent not in blob, absent
    g.q("miss", "no-bl108", "out_of_corpus", "What is the budget for line BL-108 Equipment Insurance?", answerable=False,
        origin="budget lines stop at BL-107; nothing about insurance anywhere")
    g.q("miss", "no-termination-notice", "out_of_corpus",
        "What notice period does the contract require for termination for convenience?", answerable=False,
        origin="the contract has no termination clause")
    g.q("miss", "no-actual-spend", "out_of_corpus", "How much has actually been spent to date on BL-103 Crane Hire?",
        answerable=False, origin="the workbook holds budgets and rates only; no actuals")
    g.q("miss", "no-fy2027-headcount", "out_of_corpus", "How many Environmental FTE are planned for FY2027?",
        answerable=False, origin="Headcount covers FY2026 Q1-Q4 only")
    return g


def write_gold(g: Gold) -> None:
    assign_splits(g.questions)
    header = (
        "# G6 interlinked three-file gold set over corpus-synthetic-g6 (budget.xlsx, contract.pdf, memo.docx).\n"
        "# GENERATED by eval-public/build_synthetic_corpus_g6.py: expected values are read from the corpus files and\n"
        "# derived answers are computed in code, never typed. Category tags live in the id and origin. SYNTHETIC: not a\n"
        "# milestone population. Do not edit by hand; rerun the generator.\n"
    )
    body = yaml.safe_dump({"version": 1, "population": "synthetic_public", "questions": g.questions},
                          sort_keys=False, allow_unicode=True, width=110)
    GOLD.write_text(header + body, encoding="utf-8")


def main() -> None:
    build_corpus()
    g = build_gold()
    write_gold(g)
    cats: dict[str, int] = {}
    for q in g.questions:
        c = q["id"].split("-")[1]
        cats[c] = cats.get(c, 0) + 1
    print(f"{GOLD.name}: {len(g.questions)} questions {cats}")
    for p in sorted(OUT.iterdir()) + [GOLD]:
        print(hashlib.sha256(p.read_bytes()).hexdigest()[:16], p.name)


if __name__ == "__main__":
    main()
