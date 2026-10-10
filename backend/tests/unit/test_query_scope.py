"""Enforced query scope (doc 17 section 5.4): the boundary holds on the fast and
agent paths, an empty scope never widens, named files only narrow, and a
question without a scope behaves exactly as before."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import Engine

from docket.core.config import Settings
from docket.core.db.engine import get_session_factory
from docket.core.db.models import (
    AuthorizedSource, Chunk, ChunkRecipe, EvidenceUnit, EvidenceVersion, Source,
    SourceStatus, VersionStatus, Workspace,
)
from docket.eval.schema import RunRecord
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.evidence.workbook_reader import WorkbookReader, WorkbookReadError
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.services.agent import run_context
from docket.services.agent.tools import make_read_evidence_tool, make_search_knowledge_tool
from docket.services.query import service as service_module
from docket.services.query.classifier import QueryMode
from docket.infra.retrieval.resolver import EvidenceResolver
from docket.services.query.scope import QueryScope, ScopeKind, resolve
from docket.services.query.service import QueryService
from docket.services.query.trust import AbstentionReason

SPECS = [
    {"path": "/w/docs/Alpha-Plan.docx", "text": "alpha plan roadmap"},
    {"path": "/w/docs/sub/Beta-Notes.docx", "text": "beta notes roadmap"},
    {"path": "/w/docs2/Gamma-Memo.docx", "text": "gamma memo roadmap"},
    {"path": "/w/other/Delta-Brief.docx", "text": "delta brief roadmap",
     "source_status": SourceStatus.REVOKED},
    {"path": "/w/docs/Pending-File.docx", "text": "pending file roadmap",
     "version_status": VersionStatus.PENDING},
]
IDS = {"alpha": "ev_0", "beta": "ev_1", "gamma": "ev_2", "delta": "ev_3", "pending": "ev_4"}


def _build(engine: Engine, tmp_path: Path, gateway: FakeInferenceGateway):
    sf = get_session_factory(engine)
    records: list[ChunkRecord] = []
    with sf() as session:
        session.add(Workspace(id="ws", name="ws"))
        session.add(AuthorizedSource(id="auth", workspace_id="ws", scope_path="/w"))
        session.add(ChunkRecipe(id="rcp", chunk_size=1, overlap=0, splitter="t", parser_name="t", parser_version="1"))
        for i, spec in enumerate(SPECS):
            sid, vid, uid, cid = f"src_{i}", f"ev_{i}", f"eu_{i}", f"chk_{i}"
            session.add(Source(
                id=sid, workspace_id="ws", authorized_source_id="auth", source_type="local_folder",
                path=spec["path"], status=spec.get("source_status", SourceStatus.ACTIVE),
            ))
            session.add(EvidenceVersion(
                id=vid, source_id=sid, file_path=spec["path"], content_hash=f"h{i}", byte_size=1,
                parser_name="t", parser_version="1",
                status=spec.get("version_status", VersionStatus.READY),
            ))
            session.add(EvidenceUnit(
                id=uid, evidence_version_id=vid, unit_index=0, heading=None, content_hash=f"u{i}",
                unit_kind="section", locator_json=None,
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


@pytest.fixture()
def fx(migrated_sqlite_engine: Engine, tmp_path: Path):
    gw = FakeInferenceGateway()
    table, sf = _build(migrated_sqlite_engine, tmp_path, gw)
    return SimpleNamespace(engine=migrated_sqlite_engine, table=table, sf=sf, gw=gw, tmp=tmp_path)


def _service(fx, *, agent=None, canned: str | None = "roadmap [x]", settings: Settings | None = None):
    gw = FakeInferenceGateway(canned_response=canned) if canned is not None else fx.gw
    return QueryService(
        engine=fx.engine, table=fx.table, gateway=gw, resolver=EvidenceResolver(fx.sf), top_k=8,
        settings=settings or Settings(), agent=agent,
    ), gw


class _SpySearch:
    """Wraps the real hybrid_search the fast path uses, recording its kwargs."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[dict] = []
        real = service_module.hybrid_search

        def spy(**kw):
            self.calls.append(kw)
            return real(**kw)

        monkeypatch.setattr(service_module, "hybrid_search", spy)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_folder_containment_is_component_wise(fx) -> None:
    r = resolve(fx.engine, QueryScope.for_folder("/w/docs"))
    assert set(r.version_ids) == {IDS["alpha"], IDS["beta"]}  # not /w/docs2, not PENDING
    assert r.file_count == 2 and not r.empty and r.label == "docs"
    r2 = resolve(fx.engine, QueryScope.for_folder("/w/docs2/"))
    assert set(r2.version_ids) == {IDS["gamma"]}
    assert set(resolve(fx.engine, QueryScope.for_folder("/w/doc")).version_ids) == set()


def test_folder_scope_ready_and_active_only(fx) -> None:
    other = resolve(fx.engine, QueryScope.for_folder("/w/other"))
    assert other.empty and other.version_ids == () and other.file_count == 0
    assert other.reason == AbstentionReason.NO_EVIDENCE.value
    assert IDS["pending"] not in resolve(fx.engine, QueryScope.for_folder("/w/docs")).version_ids


def test_single_file_path_scope_matches_only_that_file(fx) -> None:
    r = resolve(fx.engine, QueryScope.for_folder("/w/docs/Alpha-Plan.docx"))
    assert r.version_ids == (IDS["alpha"],)


def test_source_scope_and_unavailable_source(fx) -> None:
    assert resolve(fx.engine, QueryScope.for_source("src_1")).version_ids == (IDS["beta"],)
    revoked = resolve(fx.engine, QueryScope.for_source("src_3"))
    assert revoked.empty and revoked.reason == AbstentionReason.SOURCE_UNAVAILABLE.value
    missing = resolve(fx.engine, QueryScope.for_source("nope"))
    assert missing.empty and missing.reason == AbstentionReason.SOURCE_UNAVAILABLE.value


def test_everything_is_unrestricted_not_empty(fx) -> None:
    r = resolve(fx.engine, QueryScope.everything())
    assert r.version_ids is None and not r.empty and r.unrestricted
    assert r.file_count == 3  # alpha, beta, gamma are eligible


# ---------------------------------------------------------------------------
# fast path
# ---------------------------------------------------------------------------


def test_fast_path_search_stays_inside_the_scope(fx, monkeypatch) -> None:
    spy = _SpySearch(monkeypatch)
    svc, gw = _service(fx)
    result = svc.ask("what is the roadmap?", mode=QueryMode.FAST, scope=QueryScope.for_folder("/w/docs"))
    assert set(spy.calls[0]["scope_version_ids"]) == {IDS["alpha"], IDS["beta"]}
    prompt = gw.generate_calls[0]["prompt"]
    assert "alpha plan" in prompt and "gamma memo" not in prompt and "delta brief" not in prompt
    assert result.scope_label == "docs" and result.scope_file_count == 2


def test_empty_scope_never_searches_and_makes_no_model_call(fx, monkeypatch) -> None:
    spy = _SpySearch(monkeypatch)
    svc, gw = _service(fx)
    for mode in (QueryMode.FAST, QueryMode.AGENT):
        result = svc.ask("what is the roadmap?", mode=mode, scope=QueryScope.for_folder("/w/empty"))
        assert result.abstained and result.citations == []
        assert result.answer == "Nothing is indexed in empty yet."
        assert result.abstention_reason == AbstentionReason.NO_EVIDENCE.value
        assert result.scope_label == "empty" and result.scope_file_count == 0
    assert spy.calls == []
    assert gw.generate_calls == [] and gw.embed_calls == []


def test_empty_scope_with_history_makes_no_rewrite_call(fx) -> None:
    from docket.services.query.conversation import ConversationTurn

    svc, gw = _service(fx)
    result = svc.ask(
        "and the other one?", mode=QueryMode.FAST,
        history=[ConversationTurn(question="q", answer="a")],
        scope=QueryScope.for_folder("/w/empty"),
    )
    assert result.abstained and gw.generate_calls == []


def test_unavailable_source_scope_uses_its_own_reason(fx) -> None:
    svc, gw = _service(fx)
    result = svc.ask("roadmap?", mode=QueryMode.FAST, scope=QueryScope.for_source("src_3", "Delta"))
    assert result.abstained and result.abstention_reason == AbstentionReason.SOURCE_UNAVAILABLE.value
    assert gw.generate_calls == []


def test_named_file_inside_scope_narrows(fx, monkeypatch) -> None:
    spy = _SpySearch(monkeypatch)
    svc, _ = _service(fx)
    result = svc.ask(
        "In Alpha-Plan.docx what is the roadmap?", mode=QueryMode.FAST,
        scope=QueryScope.for_folder("/w/docs"),
    )
    assert spy.calls[0]["scope_version_ids"] == [IDS["alpha"]]
    assert result.scoped_to == ["Alpha-Plan.docx"]


def test_named_file_outside_scope_explains_and_does_not_widen(fx, monkeypatch) -> None:
    spy = _SpySearch(monkeypatch)
    svc, gw = _service(fx)
    for mode in (QueryMode.FAST, QueryMode.AGENT):
        result = svc.ask(
            "In Gamma-Memo.docx what is the roadmap?", mode=mode,
            scope=QueryScope.for_folder("/w/docs"),
        )
        assert result.abstained and "outside the selected folder" in result.answer
        assert result.scoped_to == ["Gamma-Memo.docx"]
    assert spy.calls == []
    assert gw.generate_calls == []


def test_named_file_without_scope_is_unchanged(fx, monkeypatch) -> None:
    spy = _SpySearch(monkeypatch)
    svc, _ = _service(fx)
    result = svc.ask("In Gamma-Memo.docx what is the roadmap?", mode=QueryMode.FAST)
    assert spy.calls[0]["scope_version_ids"] == [IDS["gamma"]]
    assert result.scoped_to == ["Gamma-Memo.docx"] and result.scope_label is None


def test_none_scope_is_identical_to_previous_behaviour(fx, monkeypatch) -> None:
    spy = _SpySearch(monkeypatch)
    svc, _ = _service(fx)
    q = "what is the roadmap?"
    base = svc.ask(q, mode=QueryMode.FAST)
    explicit = svc.ask(q, mode=QueryMode.FAST, scope=None)
    assert base.model_dump() == explicit.model_dump()
    assert base.scope_label is None and base.scope_file_count is None
    assert base.abstention_reason is None
    assert all("scope_version_ids" not in c for c in spy.calls)
    # "everything" searches the same way and only adds the scope fields
    everything = svc.ask(q, mode=QueryMode.FAST, scope=QueryScope.everything("All"))
    assert "scope_version_ids" not in spy.calls[-1]
    dumped = everything.model_dump()
    assert dumped.pop("scope_label") == "All" and dumped.pop("scope_file_count") == 3
    expected = base.model_dump()
    expected.pop("scope_label"), expected.pop("scope_file_count")
    assert dumped == expected


# ---------------------------------------------------------------------------
# agent path
# ---------------------------------------------------------------------------


class _ToolAgent:
    """Fake agent that drives the REAL tools under the run context the service
    bound: it searches, then tries to read every chunk id it knows about."""

    def __init__(self, fx, *, read_ids: list[str], search_gw=None) -> None:
        self.search = make_search_knowledge_tool(
            engine=fx.engine, table=fx.table, gateway=search_gw or FakeInferenceGateway(),
            top_k=8, scope_provider=run_context.current_search_scope,
        )
        self.read = make_read_evidence_tool(
            resolver=EvidenceResolver(fx.sf), boundary_provider=run_context.current_boundary_ids
        )
        self.read_ids = read_ids
        self.found: list[str] = []
        self.read_results: dict[str, dict] = {}

    def invoke(self, state: dict, config: dict | None = None) -> dict:
        self.found = [r["chunk_id"] for r in json.loads(self.search.invoke({"query": "roadmap"}))["results"]]
        messages: list = []
        for i, cid in enumerate(self.read_ids):
            out = self.read.invoke({"chunk_id": cid})
            self.read_results[cid] = json.loads(out)
            messages.append(AIMessage(content="", tool_calls=[
                {"name": "read_evidence", "args": {"chunk_id": cid}, "id": f"c{i}"}]))
            messages.append(ToolMessage(content=out, tool_call_id=f"c{i}"))
        label = next((r["citation_label"] for r in self.read_results.values() if "error" not in r), "")
        messages.append(AIMessage(content=f"The roadmap is covered {label}."))
        return {"messages": messages, "iterations": 1, "tool_calls_made": len(self.read_ids), "blocked_calls": []}


def test_agent_search_and_reads_respect_the_boundary(fx) -> None:
    agent = _ToolAgent(fx, read_ids=["chk_2", "chk_0"])  # gamma is outside /w/docs
    svc, _ = _service(fx, agent=agent)
    result = svc.ask("what is the roadmap?", mode=QueryMode.AGENT, scope=QueryScope.for_folder("/w/docs"))
    assert set(agent.found) == {"chk_0", "chk_1"}
    assert "outside the selected search scope" in agent.read_results["chk_2"]["error"]
    assert "error" not in agent.read_results["chk_0"]
    assert [c.chunk_id for c in result.citations] == ["chk_0"]
    assert result.scope_label == "docs" and result.scope_file_count == 2
    # the run state is cleared afterwards
    assert run_context.current_search_scope() is None and run_context.current_boundary_ids() is None


def test_agent_without_scope_is_unrestricted(fx) -> None:
    agent = _ToolAgent(fx, read_ids=["chk_2"])
    svc, _ = _service(fx, agent=agent)
    result = svc.ask("what is the roadmap?", mode=QueryMode.AGENT)
    assert set(agent.found) == {"chk_0", "chk_1", "chk_2"}
    assert [c.chunk_id for c in result.citations] == ["chk_2"]


def test_agent_named_file_inside_scope_narrows_search(fx) -> None:
    agent = _ToolAgent(fx, read_ids=["chk_0"])
    svc, _ = _service(fx, agent=agent)
    result = svc.ask(
        "In Beta-Notes.docx what is the roadmap?", mode=QueryMode.AGENT,
        scope=QueryScope.for_folder("/w/docs"),
    )
    assert agent.found == ["chk_1"] and result.scoped_to == ["Beta-Notes.docx"]


def test_service_builds_agent_with_scope_and_boundary_providers(fx, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        service_module, "build_investigation_agent", lambda **kw: captured.update(kw) or object()
    )
    svc, _ = _service(fx)
    svc._get_agent()
    assert captured["scope_provider"] is run_context.current_search_scope
    assert captured["boundary_provider"] is run_context.current_boundary_ids


def test_empty_scope_list_in_search_tool_does_not_widen(fx) -> None:
    tool = make_search_knowledge_tool(
        engine=fx.engine, table=fx.table, gateway=fx.gw, top_k=8, scope_provider=lambda: [],
    )
    before = len(fx.gw.embed_calls)
    assert json.loads(tool.invoke({"query": "roadmap"})) == {"results": []}
    assert len(fx.gw.embed_calls) == before  # nothing was embedded or searched


def test_workbook_reader_refuses_chunks_outside_the_boundary(fx) -> None:
    reader = WorkbookReader(
        session_factory=fx.sf, store=ContentAddressedStore(fx.tmp / "store"),
        boundary_provider=lambda: ["ev_0"],
    )
    with pytest.raises(WorkbookReadError, match="outside the selected search scope"):
        reader.read("chk_2", "A1", None)
    # inside the boundary it proceeds past the check (then fails: not a workbook)
    with pytest.raises(WorkbookReadError) as exc:
        reader.read("chk_0", "A1", None)
    assert "outside the selected search scope" not in str(exc.value)


# ---------------------------------------------------------------------------
# concurrency, records
# ---------------------------------------------------------------------------


def test_concurrent_scoped_asks_do_not_share_boundaries(fx) -> None:
    barrier = threading.Barrier(2)
    seen: dict[str, tuple] = {}

    class _Barrier:
        def invoke(self, state, config=None):
            q = state["messages"][-1].content
            barrier.wait(timeout=10)
            seen[q] = (
                sorted(run_context.current_search_scope() or []),
                sorted(run_context.current_boundary_ids() or []),
            )
            barrier.wait(timeout=10)
            return {"messages": [AIMessage(content="x")], "iterations": 1, "tool_calls_made": 0, "blocked_calls": []}

    svc, _ = _service(fx, agent=_Barrier())
    scopes = {"qa": QueryScope.for_folder("/w/docs"), "qb": QueryScope.for_folder("/w/docs2")}
    errors: list[BaseException] = []

    def run(q: str) -> None:
        try:
            svc.ask(q, mode=QueryMode.AGENT, scope=scopes[q])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(q,)) for q in scopes]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert not errors, errors
    assert seen["qa"] == (["ev_0", "ev_1"], ["ev_0", "ev_1"])
    assert seen["qb"] == (["ev_2"], ["ev_2"])


def test_old_run_records_without_scope_fields_still_load() -> None:
    old = json.dumps({"question_id": "q1", "repeat": 0, "answer": "a"})
    record = RunRecord.model_validate_json(old)
    assert record.scope_label is None and record.scope_file_count is None
    new = RunRecord(question_id="q", repeat=0, scope_label="docs", scope_file_count=4)
    assert RunRecord.model_validate_json(new.model_dump_json()).scope_file_count == 4


def test_scope_kind_values() -> None:
    assert {k.value for k in ScopeKind} == {"everything", "folder", "source"}


# ---------------------------------------------------------------------------
# CLI: docket query --here / --folder
# ---------------------------------------------------------------------------


def _cli_query(monkeypatch, args: list[str]):
    from typer.testing import CliRunner

    import docket.interfaces.cli.main as cli_main

    asked: list[dict] = []

    class _Svc:
        def __init__(self, **kw) -> None:
            pass

        def ask(self, question, **kw):
            asked.append(kw)
            return SimpleNamespace(
                mode="fast", answer="ok", citations=[], validation_warnings=[],
                scope_label=kw["scope"].label if "scope" in kw else None,
                scope_file_count=2 if "scope" in kw else None,
            )

    ctx = SimpleNamespace(
        vector_writer=SimpleNamespace(table=object()), page_table_for_query=None, engine=None,
        gateway=None, resolver=None, settings=None, index_manifest_guard=None,
    )
    monkeypatch.setattr(cli_main, "build_context", lambda: ctx)
    monkeypatch.setattr(cli_main, "QueryService", _Svc)
    result = CliRunner().invoke(cli_main.app, ["query", "what?", *args])
    return result, asked


def test_cli_query_default_is_global(monkeypatch) -> None:
    result, asked = _cli_query(monkeypatch, [])
    assert result.exit_code == 0 and asked == [{}]
    assert "Scope:" not in result.output


def test_cli_query_here_and_folder_pass_a_folder_scope(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    result, asked = _cli_query(monkeypatch, ["--here"])
    assert result.exit_code == 0
    scope = asked[0]["scope"]
    assert scope.kind == ScopeKind.FOLDER and scope.folder == str(tmp_path.resolve())
    assert f"Scope: {tmp_path.name} (2 documents)" in result.output

    sub = tmp_path / "sub"
    sub.mkdir()
    result, asked = _cli_query(monkeypatch, ["--folder", str(sub)])
    assert asked[0]["scope"].folder == str(sub.resolve())
