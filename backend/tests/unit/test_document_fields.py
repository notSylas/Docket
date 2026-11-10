"""Deterministic classifier + contract-field extractors on realistic text."""
from __future__ import annotations

import pytest

from docket.services.documents import classify
from docket.services.documents.fields import ChunkText, extract_all, parse_duration


def ct(text, i=0, heading=None):
    return ChunkText(f"chk{i}", text, i, heading)


def fv(chunks, field, parties=None):
    got = {f.field: f for f in extract_all(chunks, parties)}
    return got.get(field)


@pytest.mark.parametrize("text,want", [
    ("This Agreement shall be governed by and construed in accordance with the laws of the State of New York, without regard to conflict of laws.", "New York"),
    ("THIS AGREEMENT SHALL BE GOVERNED BY AND INTERPRETED IN ACCORDANCE WITH THE LAWS OF THE STATE OF NEW YORK.", "New York"),
    ("This Agreement and any dispute shall be governed by, and construed in accordance with, the laws of England and Wales.", "England and Wales"),
    ("The Agreement is governed by Delaware law, excluding its conflict of law rules. Governing law: the laws of the State of Delaware apply.", "Delaware"),
    ("This Agreement shall be construed under the laws of the Commonwealth of Massachusetts and the federal laws of the United States.", "Massachusetts"),
    ("This Agreement is governed by the laws of Hong Kong.", "Hong Kong"),
])
def test_governing_law(text, want):
    f = fv([ct(text)], "governing_law")
    assert f and f.normalized == want and f.confidence >= 0.8
    s, e = f.span
    assert text[s:e].strip() == f.value


def test_governing_law_ignores_incorporation_and_conflicts():
    org = ct("Acme Inc. is a corporation organized under the laws of the State of Delaware and has its office in Texas.")
    assert fv([org], "governing_law") is None
    both = [ct("This Agreement is governed by the laws of the State of New York.", 0),
            ct("This Agreement shall be governed by the laws of California.", 1)]
    f = fv(both, "governing_law")
    assert f.confidence < 0.8  # conflicting jurisdictions never answered


@pytest.mark.parametrize("text,iso", [
    ("This Agreement is made and entered into as of March 21, 2018 (the \"Effective Date\") by and between", "2018-03-21"),
    ("THIS DISTRIBUTOR AGREEMENT is effective as of the 5th day of May, 2019, between", "2019-05-05"),
    ("This Agreement is dated as of 21 June 2005 and made between", "2005-06-21"),
    ("Effective Date: 2020-01-09", "2020-01-09"),
    ("This Agreement, made as of 03/04/2011, between", "2011-03-04"),
])
def test_effective_date(text, iso):
    f = fv([ct(text)], "effective_date")
    assert f and f.normalized == iso and f.confidence >= 0.8


def test_effective_date_not_taken_from_body_or_blank():
    assert fv([ct("This Agreement is made as of ______ day of ________, 20__ between"), ct("x", 5), ct("y", 6), ct("z", 7),
               ct("Payment is due on March 1, 2021.", 8)], "effective_date") is None


@pytest.mark.parametrize("text,norm", [
    ("The Customer may terminate this Agreement for convenience upon sixty (60) days' prior written notice to the Supplier.", "60 days"),
    ("Either party may terminate this Agreement without cause by giving thirty (30) days written notice.", "30 days"),
    ("Either party may terminate this Agreement at any time upon 90 days prior notice.", "90 days"),
    ("Buyer may terminate for convenience on not less than six (6) months advance written notice.", "6 months"),
])
def test_notice_period(text, norm):
    f = fv([ct(text)], "termination_notice_period")
    assert f and f.normalized == norm and f.confidence >= 0.8


def test_notice_period_cure_not_confused():
    t = ct("Either party may terminate this Agreement upon thirty (30) days written notice if the other party materially breaches and fails to cure.")
    f = fv([t], "termination_notice_period")
    assert f is None or f.confidence < 0.8


def test_term_and_renewal():
    t = ct("The initial term of this Agreement shall be five (5) years from the Effective Date. Thereafter this Agreement shall automatically renew for successive one (1) year periods unless either party gives notice.")
    assert fv([t], "expiration_or_term").normalized == "5 years"
    r = fv([t], "renewal_term")
    assert r.normalized == "1 year" and r.confidence >= 0.8
    d = fv([ct("This Agreement shall remain in effect until December 31, 2024 unless terminated earlier. The term of this Agreement")], "expiration_or_term")
    assert d is None or d.confidence < 0.8 or d.normalized == "2024-12-31"


def test_liability_cap_and_flags():
    cap = ct("IN NO EVENT SHALL EITHER PARTY'S AGGREGATE LIABILITY UNDER THIS AGREEMENT EXCEED $1,000,000.")
    f = fv([cap], "liability_cap")
    assert f.normalized == "$1,000,000" and f.confidence >= 0.8
    ex = fv([ct("Distributor shall be the exclusive distributor of the Products in the Territory.")], "exclusivity")
    assert ex.normalized == "present"
    ne = fv([ct("Distributor shall be the non-exclusive distributor of the Products. Disputes are subject to the exclusive jurisdiction of the courts.")], "exclusivity")
    assert ne.normalized == "absent" and ne.confidence < 0.8
    assert fv([ct("Neither party shall assign this Agreement without the prior written consent of the other.")], "anti_assignment").normalized == "present"
    assert fv([ct("Each party shall have the right to audit the books and records of the other party.")], "audit_rights").normalized == "present"
    ins = fv([ct("Licensee shall maintain commercial general liability insurance of not less than $2,000,000 per occurrence.")], "insurance")
    assert ins.normalized == "$2,000,000"
    assert fv([ct("Licensee shall not compete with Licensor in the Territory during the Term.")], "non_compete").normalized == "present"


def test_parse_duration_variants():
    assert parse_duration("within sixty (60) days")[0] == "60 days"
    assert parse_duration("thirty days")[0] == "30 days"
    assert parse_duration("one (1) year")[0] == "1 year"
    assert parse_duration("12 months")[0] == "12 months"


def test_parties_and_dates():
    pre = ("MASTER SERVICES AGREEMENT\nThis Agreement is entered into as of June 1, 2020 by and between Acme Widgets, Inc., a Delaware corporation "
           "(\"Acme\"), and Globex Corporation, a New York corporation (\"Globex\"), whereas the parties wish")
    p = classify.extract_parties(pre)
    assert p[:2] == ["Acme Widgets, Inc.", "Globex Corporation"] or p[:2] == ["Acme Widgets, Inc", "Globex Corporation"]
    assert classify.preamble_dates(pre)["effective"] == "2020-06-01"
    p2 = classify.extract_parties("This Agreement is between Skype Communications S.a r.l. and Acme Ltd (together, the Parties).")
    assert len(p2) == 2


def test_doc_type_title_family():
    assert classify.classify_doc_type("MSA.pdf", "Master Services Agreement", "") == "agreement"
    assert classify.classify_doc_type("a.pdf", "First Amendment to Lease", "") == "amendment"
    assert classify.classify_doc_type("book.xlsx", "x", "") == "spreadsheet"
    assert classify.classify_doc_type("m.eml", "x", "") == "email"
    assert classify.classify_doc_type("inv.pdf", "Invoice 1042", "Amount due: $5") == "invoice"
    assert classify.classify_doc_type("memo.docx", "MEMORANDUM", "TO: Board\nFROM: CFO") == "memo"
    assert classify.classify_doc_type("q3.pdf", "Quarterly Report", "") == "report"
    assert classify.classify_doc_type("notes.txt", "Ideas", "stuff") == "other"
    a = classify.family_key("Acme MSA v2 FINAL (1).pdf", "agreement", "acme")
    b = classify.family_key("Acme MSA 2021-03-04 signed.pdf", "contract", "acme")
    assert a == b and a.startswith("contract|acme|acme msa")
    assert classify.family_key("Acme MSA.pdf", "agreement", "other") != a
    assert classify.guess_title("x.pdf", "\n12\nDISTRIBUTOR AGREEMENT\nThis agreement is made", None) == "DISTRIBUTOR AGREEMENT"


def test_dates_formats():
    got = [i for i, _, _ in classify.find_dates("On 21st day of March, 2018 and Sept. 3, 2019 and 2020-02-29 and 13/13/2020")]
    assert got == ["2018-03-21", "2019-09-03", "2020-02-29"]
