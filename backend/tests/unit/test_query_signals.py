"""Query signals (doc 05 section 6): explicit file scoping, ambiguous-period
handling and number normalization. FakeInferenceGateway, real SQLite/LanceDB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from docket.core.config import Settings
from docket.core.db.engine import get_session_factory
from docket.core.db.models import (
    AuthorizedSource, Chunk, ChunkRecipe, EvidenceUnit, EvidenceVersion, Source,
    SourceStatus, VersionStatus, Workspace,
)
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.retrieval import hybrid as hybrid_module
from docket.infra.retrieval.hybrid import (
    _sanitize_fts_query, fts_search, hybrid_search, number_forms, vector_search, with_number_forms,
)
from docket.infra.retrieval.resolver import EvidenceResolver, ResolvedEvidence
from docket.prompts.query import period_ambiguity_note
from docket.services.agent.tools import make_search_knowledge_tool
from docket.services.query.classifier import QueryMode
from docket.services.query.conversation import ConversationTurn
from docket.services.query.prompts import ABSTENTION_PHRASE
from docket.services.query.service import QueryService
from docket.services.query.signals import (
    EligibleFile, detect_period_ambiguity, extract_signals, has_period, load_eligible_files,
    normalize_fiscal_year,
)

# ---------------------------------------------------------------------------
# Signals: file scoping
# ---------------------------------------------------------------------------

FILES = [
    EligibleFile("Revenue-FY2025-26.xlsx", "ev_a"),
    EligibleFile("Revenue-FY2025-26-Budget.xlsx", "ev_b"),
    EligibleFile("Revenue-FY2024-25.xlsx", "ev_c"),
    EligibleFile("Board-Memo-Q2-FY2025-26.docx", "ev_d"),
    EligibleFile("Budget.xlsx", "ev_e"),
]


def _scope(question: str, files=FILES) -> list[str]:
    return extract_signals(question, files).scope_files


def test_extension_names_scope_the_file_case_insensitively() -> None:
    assert _scope("In Revenue-FY2024-25.xlsx, what was total revenue for August?") == ["Revenue-FY2024-25.xlsx"]
    assert _scope("what is in REVENUE-FY2024-25.XLSX?") == ["Revenue-FY2024-25.xlsx"]
    sig = extract_signals("According to Revenue-FY2025-26-Budget.xlsx, what is August?", FILES)
    assert sig.scope_version_ids == ["ev_b"]


def test_extension_disambiguates_prefix_collision() -> None:
    assert _scope("In Revenue-FY2025-26.xlsx what is the Q1 subtotal?") == ["Revenue-FY2025-26.xlsx"]
    assert _scope("In Revenue-FY2025-26-Budget.xlsx what is the Q1 subtotal?") == ["Revenue-FY2025-26-Budget.xlsx"]


def test_quoted_and_backticked_stems_scope() -> None:
    assert _scope('In "Revenue-FY2024-25" what was August?') == ["Revenue-FY2024-25.xlsx"]
    assert _scope("In `revenue fy2024 25` what was August?") == ["Revenue-FY2024-25.xlsx"]


def test_adjacent_noun_scopes_with_interchangeable_separators() -> None:
    assert _scope("In the Revenue-FY2024-25 workbook, what was August?") == ["Revenue-FY2024-25.xlsx"]
    assert _scope("What does the memo Board Memo Q2 FY2025-26 say about Q3?") == ["Board-Memo-Q2-FY2025-26.docx"]
    assert _scope("Revenue_FY2024_25 spreadsheet: August total?") == ["Revenue-FY2024-25.xlsx"]


def test_topical_words_never_scope() -> None:
    assert _scope("What was August revenue in FY2025-26?") == []
    assert _scope("Revenue FY2024-25 August total?") == []
    assert _scope("Tell me about the board memo for Q2 FY2025-26") == []


def test_prefix_collision_without_extension_does_not_scope() -> None:
    # The Budget stem holds the shorter stem as a prefix; a bare mention of
    # the longer one must not scope to the shorter one (nor does a bare mention of
    # either without a quote/noun/extension).
    assert _scope("What is in Revenue-FY2025-26-Budget for August?") == []
    assert _scope("In the Revenue-FY2025-26-Budget workbook, August total?") == ["Revenue-FY2025-26-Budget.xlsx"]
    assert _scope("In the Revenue-FY2025-26 workbook, August total?") == ["Revenue-FY2025-26.xlsx"]


def test_two_files_named_do_not_scope() -> None:
    assert _scope("Compare Revenue-FY2024-25.xlsx with Revenue-FY2025-26.xlsx for August") == []


def test_single_token_stems_never_scope() -> None:
    assert _scope("In Budget.xlsx what is the total?") == []
    assert _scope('In the "Budget" workbook what is the total?') == []


def test_non_ready_or_revoked_files_are_not_candidates() -> None:
    assert _scope("In Revenue-FY2024-25.xlsx what was August?", files=[]) == []
    assert _scope("In Revenue-FY2024-25.xlsx what was August?", files=[FILES[0]]) == []


# ---------------------------------------------------------------------------
# Signals: periods and numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question", [
    "What was revenue in FY2025-26?", "revenue for fy25-26", "revenue in 2025", "Revenue for 2024-25",
    "What was Q1 revenue?", "H2 headcount", "Sales in q3",
])
def test_has_period_true(question: str) -> None:
    assert has_period(question)


@pytest.mark.parametrize("question", [
    "What was August revenue?", "total revenue in June?", "What was Marigold Chai revenue in September?",
    "Total 2330000 units",
])
def test_has_period_false_for_bare_month(question: str) -> None:
    assert not has_period(question)


def test_signals_carry_number_forms() -> None:
    assert extract_signals("was it 6,545,000?", []).number_forms == ["6545000"]


# ---------------------------------------------------------------------------
# Number normalization
# ---------------------------------------------------------------------------


def test_number_forms_examples() -> None:
    assert number_forms("1,234.56") == ["1234.56"]
    assert number_forms("revenue 6,545,000 total") == ["6545000"]
    assert number_forms("loss of -1,234.5 and 12,000") == ["-1234.5", "12000"]
    assert number_forms("1,2,3") == []
    assert number_forms("lists 1,234,5 and 12,34") == []
    assert number_forms("no numbers here, 2025") == []


def test_fts_query_keeps_original_and_adds_raw_form() -> None:
    q = _sanitize_fts_query("total of 1,234.56")
    for term in ('"total"', '"1"', '"234"', '"56"', '"1234"'):
        assert term in q
    assert '"6545000"' in _sanitize_fts_query("What is 6,545,000?")
    assert _sanitize_fts_query("What is 1,2,3?") == '"1" OR "2" OR "3"'
    assert _sanitize_fts_query("quick brown fox") == '"quick" OR "brown" OR "fox"'


def test_vector_query_text_gets_raw_form_only_when_numbers_present() -> None:
    assert with_number_forms("plain question") == "plain question"
    assert with_number_forms("is it 1,234.56?") == "is it 1,234.56? 1234.56"
    gw = FakeInferenceGateway()
    assert gw.embed(with_number_forms("x")) == gw.embed("x")


# ---------------------------------------------------------------------------
# Indexed fixture: workbooks of two fiscal years (+ a memo)
# ---------------------------------------------------------------------------

SHEET = "Monthly Revenue"
FY25_CTX = ["FY2025-26", "fiscal year 2025 2026", "Units: INR thousands (actuals)", "August"]
FY24_CTX = ["FY2024-25", "fiscal year 2024 2025", "Units: INR (actuals)", "August"]


def _build(engine: Engine, tmp_path: Path, specs: list[dict], *, gateway: FakeInferenceGateway):
    sf = get_session_factory(engine)
    records: list[ChunkRecord] = []
    with sf() as session:
        ws = Workspace(id="ws", name="ws")
        session.add(ws)
        session.add(AuthorizedSource(id="auth", workspace_id="ws", scope_path="/tmp"))
        session.add(ChunkRecipe(id="rcp", chunk_size=1, overlap=0, splitter="t", parser_name="t", parser_version="1"))
        for i, spec in enumerate(specs):
            sid, vid, uid, cid = f"src_{i}", f"ev_{i}", f"eu_{i}", f"chk_{i}"
            session.add(Source(
                id=sid, workspace_id="ws", authorized_source_id="auth", source_type="local_folder",
                path=f"/tmp/{spec['file']}",
                status=spec.get("source_status", SourceStatus.ACTIVE),
            ))
            session.add(EvidenceVersion(
                id=vid, source_id=sid, file_path=f"/tmp/{spec['file']}", content_hash=f"h{i}", byte_size=1,
                parser_name="t", parser_version="1", status=spec.get("version_status", VersionStatus.READY),
            ))
            locator = {"sheet": spec["sheet"], "context": spec["context"]} if spec.get("sheet") else None
            session.add(EvidenceUnit(
                id=uid, evidence_version_id=vid, unit_index=0, heading=None, content_hash=f"u{i}",
                unit_kind="sheet" if locator else "section",
                locator_json=json.dumps(locator) if locator else None,
            ))
            session.flush()
            session.add(Chunk(
                id=cid, source_id=sid, evidence_version_id=vid, evidence_unit_id=uid,
                chunk_recipe_id="rcp", ordinal=0, heading=None, text=spec["text"], content_hash=f"c{i}",
            ))
            records.append(ChunkRecord(
                chunk_id=cid, source_id=sid, evidence_version_id=vid, evidence_unit_id=uid,
                chunk_recipe_id="rcp", ordinal=0, heading=None, text=spec["text"], content_hash=f"c{i}",
            ))
        session.commit()
    FtsIndexWriter(engine).upsert(records, embeddings=None)
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(records, embeddings=[gateway.embed(r.text) for r in records])
    return writer._open_table(), sf


TWO_FY = [
    {"file": "Revenue-FY2025-26.xlsx", "sheet": SHEET, "context": FY25_CTX,
     "text": "Month: Aug Juniper Masala: 1043 Total: 8660"},
    {"file": "Revenue-FY2024-25.xlsx", "sheet": SHEET, "context": FY24_CTX,
     "text": "Month: Aug Saffron Rusk (INR): 2330000 Total (INR): 6545000"},
]


@pytest.fixture()
def two_fy(migrated_sqlite_engine: Engine, tmp_path: Path):
    gw = FakeInferenceGateway()
    table, sf = _build(migrated_sqlite_engine, tmp_path, TWO_FY, gateway=gw)
    return migrated_sqlite_engine, table, sf, gw


def test_load_eligible_files_excludes_revoked_and_non_ready(migrated_sqlite_engine, tmp_path) -> None:
    specs = [
        {"file": "A-One.xlsx", "text": "a one"},
        {"file": "B-Two.xlsx", "text": "b two", "source_status": SourceStatus.REVOKED},
        {"file": "C-Three.xlsx", "text": "c three", "version_status": VersionStatus.PENDING},
    ]
    _build(migrated_sqlite_engine, tmp_path, specs, gateway=FakeInferenceGateway())
    assert [f.name for f in load_eligible_files(migrated_sqlite_engine)] == ["A-One.xlsx"]


# ---------------------------------------------------------------------------
# Retrieval: scope applies before top-k
# ---------------------------------------------------------------------------


def test_scope_restricts_every_leg_before_topk(two_fy) -> None:
    engine, table, _, gw = two_fy
    q = "August total"
    unscoped = hybrid_search(engine=engine, table=table, gateway=gw, query=q, top_k=8)
    assert {r.chunk_id for r in unscoped} == {"chk_0", "chk_1"}
    for ev, chunk in (("ev_0", "chk_0"), ("ev_1", "chk_1")):
        assert fts_search(engine, q, 1, scope_version_ids=[ev]) == [chunk]
        assert vector_search(table, engine, gw, q, 1, scope_version_ids=[ev]) == [chunk]
        scoped = hybrid_search(engine=engine, table=table, gateway=gw, query=q, top_k=1, scope_version_ids=[ev])
        assert [r.chunk_id for r in scoped] == [chunk]


def test_scope_cannot_widen_eligibility_and_empty_scope_returns_nothing(migrated_sqlite_engine, tmp_path) -> None:
    gw = FakeInferenceGateway()
    specs = [dict(TWO_FY[0]), {**TWO_FY[1], "source_status": SourceStatus.REVOKED}]
    table, _ = _build(migrated_sqlite_engine, tmp_path, specs, gateway=gw)
    e = migrated_sqlite_engine
    assert hybrid_search(engine=e, table=table, gateway=gw, query="August", scope_version_ids=["ev_1"]) == []
    assert hybrid_search(engine=e, table=table, gateway=gw, query="August", scope_version_ids=[]) == []


def test_no_scope_calls_legs_exactly_as_before(two_fy, monkeypatch) -> None:
    engine, table, _, gw = two_fy
    calls: list[tuple] = []
    monkeypatch.setattr(hybrid_module, "fts_search", lambda *a, **k: calls.append(("fts", a, k)) or [])
    monkeypatch.setattr(hybrid_module, "vector_search", lambda *a, **k: calls.append(("vec", a, k)) or [])
    hybrid_search(engine=engine, table=table, gateway=gw, query="q", top_k=3)
    assert all(k == {} for _, _, k in calls)
    hybrid_search(engine=engine, table=table, gateway=gw, query="q", top_k=3, scope_version_ids=["ev_0"])
    assert all(k == {"scope_version_ids": ["ev_0"]} for _, _, k in calls[2:])


def test_number_form_finds_raw_stored_value(migrated_sqlite_engine, tmp_path) -> None:
    gw = FakeInferenceGateway()
    table, _ = _build(migrated_sqlite_engine, tmp_path, [TWO_FY[1]], gateway=gw)
    assert fts_search(migrated_sqlite_engine, "6,545,000", 3) == ["chk_0"]
    assert fts_search(migrated_sqlite_engine, "99,999", 3) == []


def test_agent_search_knowledge_honours_scope_provider(monkeypatch) -> None:
    import docket.services.agent.tools as tools_module

    calls: list[dict] = []
    monkeypatch.setattr(tools_module, "hybrid_search", lambda **kw: calls.append(kw) or [])
    state = {"ids": ["ev_9"]}
    tool = make_search_knowledge_tool(
        engine=None, table=None, gateway=None, scope_provider=lambda: state["ids"]
    )
    tool.invoke({"query": "a"})
    state["ids"] = []
    tool.invoke({"query": "b"})
    assert calls[0]["scope_version_ids"] == ["ev_9"]
    assert "scope_version_ids" not in calls[1]
    plain = make_search_knowledge_tool(engine=None, table=None, gateway=None)
    plain.invoke({"query": "c"})
    assert "scope_version_ids" not in calls[2]


# ---------------------------------------------------------------------------
# Service: scoping, nothing-found, ambiguity
# ---------------------------------------------------------------------------


def _service(fx, *, settings: Settings | None = None, response: str | None = None, extra_gw=None):
    engine, table, sf, gw = fx
    gw = extra_gw or gw
    return QueryService(
        engine=engine, table=table, gateway=gw, resolver=EvidenceResolver(sf), top_k=8,
        settings=settings or Settings(),
    ), gw


def _gw_with(response: str) -> FakeInferenceGateway:
    return FakeInferenceGateway(canned_response=response)


def test_explicit_file_scopes_retrieval_and_is_recorded(two_fy) -> None:
    engine, table, sf, _ = two_fy
    gw = _gw_with("The total was 6545000 [Revenue-FY2024-25.xlsx #chk_1]")
    svc, gw = _service(two_fy, extra_gw=gw)
    # The canned answer cites by the real label; rebuild it from the resolver.
    label = EvidenceResolver(sf).resolve("chk_1").citation_label
    gw.canned_response = f"The total was 6545000 {label}"
    result = svc.ask("In Revenue-FY2024-25.xlsx, what was total revenue for August?", mode=QueryMode.FAST)
    assert result.scoped_to == ["Revenue-FY2024-25.xlsx"]
    assert result.ambiguity is None
    prompt = gw.generate_calls[-1]["prompt"]
    assert "6545000" in prompt and "8660" not in prompt
    assert "AMBIGUITY" not in gw.generate_calls[-1]["system"]


def test_scoped_file_without_chunks_says_nothing_found_and_does_not_widen(migrated_sqlite_engine, tmp_path) -> None:
    gw = FakeInferenceGateway()
    specs = [TWO_FY[0], {"file": "Empty-Workbook.xlsx", "text": "x"}]
    table, sf = _build(migrated_sqlite_engine, tmp_path, specs, gateway=gw)
    # Remove the empty workbook's chunk everywhere so the file is eligible but empty.
    with sf() as session:
        session.delete(session.get(Chunk, "chk_1"))
        session.commit()
    with migrated_sqlite_engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM fts_chunks WHERE chunk_id = 'chk_1'")
    table.delete("chunk_id = 'chk_1'")
    svc = QueryService(engine=migrated_sqlite_engine, table=table, gateway=gw, resolver=EvidenceResolver(sf), top_k=8)
    result = svc.ask("In Empty-Workbook.xlsx what was August revenue?", mode=QueryMode.FAST)
    assert result.abstained and "Empty-Workbook.xlsx" in result.answer
    assert result.scoped_to == ["Empty-Workbook.xlsx"]
    assert result.validation_warnings
    assert not gw.generate_calls  # no model call, no widening to the other workbook


def test_scope_disabled_flag_does_not_scope(two_fy) -> None:
    gw = _gw_with("x")
    svc, gw = _service(two_fy, settings=Settings(query_scope_enabled=False), extra_gw=gw)
    result = svc.ask("In Revenue-FY2024-25.xlsx, what was total revenue for August?", mode=QueryMode.FAST)
    assert result.scoped_to is None
    assert "8660" in gw.generate_calls[-1]["prompt"]  # both workbooks retrieved


def test_ambiguity_fires_for_two_fiscal_years_without_period(two_fy) -> None:
    gw = _gw_with("anything")
    svc, gw = _service(two_fy, extra_gw=gw)
    result = svc.ask("What was August revenue?", mode=QueryMode.FAST)
    assert result.scoped_to is None
    assert result.ambiguity == {
        "reason": "period",
        "options": [
            {"file": "Revenue-FY2025-26.xlsx", "fiscal_year": "FY2025-26"},
            {"file": "Revenue-FY2024-25.xlsx", "fiscal_year": "FY2024-25"},
        ],
    } or {o["file"] for o in result.ambiguity["options"]} == {"Revenue-FY2025-26.xlsx", "Revenue-FY2024-25.xlsx"}
    system = gw.generate_calls[-1]["system"]
    assert "PERIOD AMBIGUITY NOTE" in system
    assert "Units: INR thousands (actuals)" in system and "Units: INR (actuals)" in system
    assert "FY2025-26" in system and "FY2024-25" in system
    assert "asking which fiscal year" in system
    # the note is metadata, not part of the context evidence
    assert "PERIOD AMBIGUITY" not in gw.generate_calls[-1]["prompt"]
    # verbatim context lines only: bare labels/months are not repeated as "units"
    assert "workbook context: Units: INR thousands (actuals)" in system


@pytest.mark.parametrize("question", [
    "What was August revenue in FY2025-26?",
    "What was Q1 revenue?",
    "What was August revenue in 2024?",
    "In Revenue-FY2024-25.xlsx what was August revenue?",
])
def test_ambiguity_does_not_fire_when_period_or_scope_given(two_fy, question: str) -> None:
    svc, gw = _service(two_fy, extra_gw=_gw_with("anything"))
    result = svc.ask(question, mode=QueryMode.FAST)
    assert result.ambiguity is None
    assert "PERIOD AMBIGUITY" not in gw.generate_calls[-1]["system"]


def test_ambiguity_not_fired_for_followup_with_period_in_history(two_fy) -> None:
    svc, gw = _service(two_fy, extra_gw=_gw_with("anything"))
    history = [ConversationTurn(question="August revenue in FY2024-25?", answer="It was 6545000.")]
    result = svc.ask("What about August again?", mode=QueryMode.FAST, history=history)
    assert result.ambiguity is None


def test_ambiguity_flag_off_is_identical_to_baseline(two_fy) -> None:
    svc, gw = _service(two_fy, settings=Settings(query_ambiguity_enabled=False), extra_gw=_gw_with("anything"))
    result = svc.ask("What was August revenue?", mode=QueryMode.FAST)
    assert result.ambiguity is None
    from docket.services.query.prompts import SYSTEM_PROMPT
    assert gw.generate_calls[-1]["system"] == SYSTEM_PROMPT


def test_both_flags_off_do_not_read_eligible_files(two_fy, monkeypatch) -> None:
    import docket.services.query.service as service_module

    monkeypatch.setattr(service_module, "load_eligible_files", lambda e: pytest.fail("must not be called"))
    svc, _ = _service(
        two_fy, settings=Settings(query_scope_enabled=False, query_ambiguity_enabled=False),
        extra_gw=_gw_with("anything"),
    )
    assert svc.ask("What was August revenue?", mode=QueryMode.FAST).scoped_to is None


# ---------------------------------------------------------------------------
# detect_period_ambiguity (pure)
# ---------------------------------------------------------------------------


def _ev(version: str, file: str, sheet: str | None, context: list[str]) -> ResolvedEvidence:
    return ResolvedEvidence(
        chunk_id=f"chk_{version}", text="t", source_display_name=file, evidence_version_id=version,
        heading=None, citation_label=f"[{file} #{version}]", sheet=sheet, context=context,
    )


def test_detect_requires_different_labels_and_shared_sheet() -> None:
    a = _ev("a", "A.xlsx", "Rev", ["FY2025-26", "Units: INR k"])
    b = _ev("b", "B.xlsx", "Rev", ["FY2024-25", "Units: INR"])
    found = detect_period_ambiguity([a, b])
    assert found is not None
    assert [(o.file, o.fiscal_year, o.context_lines) for o in found.options] == [
        ("A.xlsx", "FY2025-26", ["Units: INR k"]), ("B.xlsx", "FY2024-25", ["Units: INR"]),
    ]
    # equal labels (e.g. actual vs budget of one fiscal year), a single workbook, a missing
    # label, or different sheets: no ambiguity
    same = _ev("c", "C.xlsx", "Rev", ["FY25-26"])
    assert detect_period_ambiguity([a, same]) is None
    assert detect_period_ambiguity([a]) is None
    assert detect_period_ambiguity([a, _ev("d", "D.xlsx", "Rev", ["Units: INR"])]) is None
    assert detect_period_ambiguity([a, _ev("e", "E.xlsx", "Other", ["FY2024-25"])]) is None
    assert detect_period_ambiguity([a, _ev("f", "F.docx", None, [])]) is None


def test_detect_requires_top_ranked_chunk_in_the_conflict() -> None:
    a = _ev("a", "A.xlsx", "Rev", ["FY2025-26"])
    b = _ev("b", "B.xlsx", "Rev", ["FY2024-25"])
    top = _ev("w", "Warehouse.xlsx", "Throughput", ["Units: percent"])
    assert detect_period_ambiguity([top, a, b]) is None
    assert detect_period_ambiguity([a, top, b]) is not None


def test_normalize_fiscal_year_forms() -> None:
    assert normalize_fiscal_year("FY25-26") == "FY2025-26"
    assert normalize_fiscal_year("FY 2025/2026") == "FY2025-26"
    assert normalize_fiscal_year("FY2025-26") == "FY2025-26"
    assert normalize_fiscal_year("2025") is None


def test_note_text_is_pinned_and_carries_verbatim_lines() -> None:
    note = period_ambiguity_note([("A.xlsx", "FY2025-26", ["Units: INR thousands (actuals)"]), ("B.xlsx", "FY2024-25", [])])
    assert "- FY2025-26: A.xlsx -- workbook context: Units: INR thousands (actuals)" in note
    assert "- FY2024-25: B.xlsx\n" in note
    assert "never cite it" in note and "End your answer with one short question" in note


def test_run_record_and_query_result_fields_default_to_none() -> None:
    from docket.eval.schema import RunRecord
    from docket.services.query.service import QueryResult

    r = RunRecord(question_id="q", repeat=0)
    assert r.scoped_to is None and r.ambiguity is None
    qr = QueryResult(question="q", answer="a", citations=[], abstained=False, validation_warnings=[], mode="fast")
    assert qr.scoped_to is None and qr.ambiguity is None
