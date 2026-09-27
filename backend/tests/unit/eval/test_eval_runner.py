"""Runner against the fake gateway on the tiny fixture corpus (no Ollama, no Docling)."""

from __future__ import annotations

import shutil

import pytest
from conftest import PlainTextParser, ScriptedGateway

from docket.db.models import Source, SourceStatus
from docket.eval.report import build_report
from docket.eval.runner import (
    EvalRunner,
    EvalSetupError,
    RecordingGateway,
    discover_sources,
    run_eval,
    select_questions,
)
from docket.eval.schema import Split, load_gold_set, load_records
from docket.inference.gateway import FakeInferenceGateway
from docket.query.classifier import QueryMode
from docket.query.prompts import ABSTENTION_PHRASE

SCRIPT = {
    "vacation days are there": ("25 days", "25 days and $1,200,000. {tag}"),
    "days of paid vacation": ("25 days", "Employees get 25 days. {tag}"),
    "unused vacation": ("March 31", "They expire after March 31. {tag}"),
    "support channels": ("three channels", "Email, phone, and chat. {tag}"),
    "marketing budget": ("marketing budget", "It is $1,200,000. {tag}"),
    "And the engineering": ("engineering budget", "It is $3,500,000. {tag}"),
}


@pytest.fixture
def gold(fixtures_dir):
    return load_gold_set(fixtures_dir / "gold.yaml")


def test_discover_sources(fixtures_dir, tmp_path):
    assert set(discover_sources(fixtures_dir / "corpus")) == {"handbook", "finance"}
    (tmp_path / "a.pdf").write_text("x")
    assert list(discover_sources(tmp_path)) == [tmp_path.name]  # no sub-folders: one source
    (tmp_path / "sub").mkdir()
    with pytest.raises(EvalSetupError, match="loose files"):
        discover_sources(tmp_path)
    with pytest.raises(EvalSetupError, match="does not exist"):
        discover_sources(tmp_path / "missing")


def test_select_questions(gold):
    assert len(select_questions(gold)) == 8
    dev = select_questions(gold, split=Split.DEV)
    test = select_questions(gold, split=Split.TEST)
    assert len(dev) + len(test) == 8
    assert [q.id for q in select_questions(gold, ids=["ceo-name"])] == ["ceo-name"]
    with pytest.raises(EvalSetupError, match="unknown question ids"):
        select_questions(gold, ids=["nope"])


def test_recording_gateway_records_calls_and_meta():
    inner = FakeInferenceGateway(canned_response="hi")
    inner.last_generate_meta = {"prompt_eval_count": 42}
    gateway = RecordingGateway(inner)
    assert gateway.generate(system="s", prompt="p") == "hi"
    assert len(gateway.embed("x")) == 32
    call = gateway.calls[0]
    assert (call.system, call.prompt, call.response, call.prompt_eval_count) == ("s", "p", "hi", 42)
    assert call.latency_s >= 0
    gateway.reset()
    assert gateway.calls == []


def test_full_run_records_everything(gold, fixtures_dir, tmp_path):
    out = tmp_path / "runs.jsonl"
    records = run_eval(
        gold, fixtures_dir / "corpus", out,
        gateway=ScriptedGateway(SCRIPT), parser=PlainTextParser(),
        repeats=2, mode=QueryMode.FAST,
    )
    assert len(records) == 16
    assert load_records(out) == records  # JSONL round-trips

    by_id = {r.question_id: r for r in records if r.repeat == 0}
    vacation = by_id["vacation-days"]
    assert vacation.answer.startswith("Employees get 25 days")
    assert vacation.citations and vacation.citations[0].chunk_id in {c.chunk_id for c in vacation.retrieved}
    assert any("25 days of paid vacation per year" in c.text for c in vacation.retrieved)
    assert vacation.prompt and vacation.prompt.startswith("Context:\n")
    assert vacation.system and "ONLY from the provided" in vacation.system
    assert vacation.prompt_eval_count == 500
    assert vacation.latency_s > 0
    assert vacation.spans_indexed == [True]
    assert vacation.mode == "fast"

    assert "Conversation so far:" in by_id["engineering-follow-up"].prompt
    assert by_id["ceo-name"].abstained and by_id["ceo-name"].answer == ABSTENTION_PHRASE
    assert by_id["ceo-name"].spans_indexed == []

    # Revoked questions run last. Retrieval now filters revoked sources
    # (milestone M3: hybrid_search joins to sources/evidence_versions and
    # excludes anything not ACTIVE/current), so the "finance" source's
    # engineering-budget chunk must not come back at all while it's revoked,
    # and the harness must report zero leaks.
    assert records[-1].question_id == "revoked-engineering"
    assert not any(
        "engineering budget is $3,500,000" in c.text for c in by_id["revoked-engineering"].retrieved
    )
    report = build_report(gold, records)
    assert report.revoked_retrieval_leaks == 0
    assert report.overall.strict.passed == 8
    assert (report.abstention_recall.passed, report.abstention_recall.total) == (4, 4)  # runs


def test_revoke_setup_applied_during_run_and_restored(gold, fixtures_dir):
    statuses: list[dict[str, SourceStatus]] = []

    def snapshot() -> None:
        with runner._context.session_factory() as session:
            statuses.append({s.path.rsplit("/", 1)[1]: s.status for s in session.query(Source).all()})

    gateway = ScriptedGateway({}, on_generate=snapshot)
    with EvalRunner(fixtures_dir / "corpus", gateway=gateway, parser=PlainTextParser(),
                    mode=QueryMode.FAST) as runner:
        runner.ingest()
        runner.run(select_questions(gold, ids=["revoked-engineering", "ceo-name"]), repeats=1)
        with runner._context.session_factory() as session:
            assert {s.status for s in session.query(Source).all()} == {SourceStatus.ACTIVE}
    by_call = statuses  # ceo-name runs first (no revoke), revoked-engineering second
    assert by_call[0]["finance"] is SourceStatus.ACTIVE
    assert by_call[1]["finance"] is SourceStatus.REVOKED
    assert by_call[1]["handbook"] is SourceStatus.ACTIVE


def test_unknown_revoke_source_is_rejected(gold, fixtures_dir):
    bad = gold.by_id()["revoked-engineering"].model_copy(deep=True)
    bad.setup.revoke = ["nonexistent"]
    with EvalRunner(fixtures_dir / "corpus", gateway=ScriptedGateway({}), parser=PlainTextParser()) as runner:
        runner.ingest()
        with pytest.raises(EvalSetupError, match="unknown source 'nonexistent'"):
            runner.run([bad], repeats=1)


def test_temp_data_dir_is_removed_and_data_dir_can_be_supplied(fixtures_dir, tmp_path, gold):
    runner = EvalRunner(fixtures_dir / "corpus", gateway=ScriptedGateway({}), parser=PlainTextParser())
    data_dir = runner._data_dir
    assert data_dir.exists()
    runner.close()
    assert not data_dir.exists()

    own = tmp_path / "mine"
    with EvalRunner(fixtures_dir / "corpus", gateway=ScriptedGateway({}), parser=PlainTextParser(),
                    data_dir=own) as kept:
        kept.ingest()
    assert own.exists()  # caller-owned dirs are left alone
    shutil.rmtree(own)


def test_inference_error_is_recorded_not_raised(fixtures_dir, gold):
    from docket.inference.gateway import InferenceUnavailableError

    class Down(ScriptedGateway):
        def generate(self, **kw):
            raise InferenceUnavailableError("ollama is down")

    with EvalRunner(fixtures_dir / "corpus", gateway=Down({}), parser=PlainTextParser(),
                    mode=QueryMode.FAST) as runner:
        runner.ingest()
        (record,) = runner.run(select_questions(gold, ids=["vacation-days"]), repeats=1)
    assert "ollama is down" in record.error and record.answer == ""


def test_ingest_with_nothing_indexed_raises(tmp_path):
    (tmp_path / "empty").mkdir()
    with EvalRunner(tmp_path, gateway=ScriptedGateway({}), parser=PlainTextParser()) as runner:
        with pytest.raises(EvalSetupError, match="nothing was indexed"):
            runner.ingest()


def test_agent_run_records_tool_trace_and_token_metadata(gold, fixtures_dir):
    from langchain_core.messages import AIMessage, ToolMessage
    import json

    class FakeAgent:
        def __init__(self, messages):
            self.messages = messages

        def invoke(self, state, config=None):
            return {"messages": state["messages"] + self.messages,
                    "iterations": 2, "tool_calls_made": 1, "blocked_calls": []}

    with EvalRunner(fixtures_dir / "corpus", gateway=ScriptedGateway({}),
                    parser=PlainTextParser(), mode=QueryMode.AGENT) as runner:
        runner.ingest()
        chunk = next(c for c in runner.corpus_chunks() if "25 days of paid vacation" in c.text)
        evidence = runner._context.resolver.resolve(chunk.chunk_id)
        runner._service._agent = FakeAgent([
            AIMessage(content="", tool_calls=[{"name": "read_evidence",
                "args": {"chunk_id": chunk.chunk_id}, "id": "read_1"}]),
            ToolMessage(content=json.dumps({"chunk_id": chunk.chunk_id,
                "text": evidence.text, "citation_label": evidence.citation_label}),
                tool_call_id="read_1"),
            AIMessage(content=f"Employees get 25 days. {evidence.citation_label}",
                response_metadata={"prompt_eval_count": 321, "eval_count": 24}),
        ])
        record = runner.run_once(gold.by_id()["vacation-days"], 0)

    assert record.mode == "agent" and record.citations
    assert [row["role"] for row in record.agent_trace][-3:] == ["ai", "tool", "ai"]
    assert record.prompt_eval_count == 321 and record.eval_count == 24
    assert "25 days of paid vacation" in record.prompt
    assert record.agent_trace[-1]["response_metadata"]["eval_count"] == 24
