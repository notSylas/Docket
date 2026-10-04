"""Unit tests for `docket.services.query.service.QueryService`.

Uses `FakeInferenceGateway` throughout -- no Ollama/GPU dependency. The
retrieval side is real (a `migrated_sqlite_engine` SQLite DB with CP1's
migration applied, populated with a real `Chunk`/`Source` chain plus matching
FTS5 and LanceDB rows), following the same fixture pattern as
`test_resolver.py` (ORM chain) and `test_hybrid_retrieval.py` (FTS/vector
population).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import Engine
from sqlalchemy.orm import Session

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
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.services.query.classifier import QueryMode
from docket.services.query.conversation import ConversationTurn
from docket.services.query.prompts import ABSTENTION_PHRASE
from docket.services.query.service import QueryService, _citations_from_agent_messages
from docket.infra.retrieval.resolver import EvidenceResolver, ResolvedEvidence

CHUNK_TEXT = "Reciprocal Rank Fusion combines multiple ranked search results into one."


@pytest.fixture()
def built(migrated_sqlite_engine: Engine) -> dict:
    """Populate a real `Chunk`/`Source` chain (for the resolver) AND matching
    FTS5 + LanceDB rows (for hybrid_search) in one fixture, all pointed at
    `migrated_sqlite_engine`'s tmp_path-backed SQLite DB."""
    session_factory = get_session_factory(migrated_sqlite_engine)
    with session_factory() as session:
        workspace = Workspace(name="Default Workspace")
        session.add(workspace)
        session.flush()

        authorized_source = AuthorizedSource(
            workspace_id=workspace.id, scope_path="/home/user/Documents"
        )
        session.add(authorized_source)
        session.flush()

        source = Source(
            workspace_id=workspace.id,
            authorized_source_id=authorized_source.id,
            source_type="local_folder",
            path="/home/user/Documents/report.pdf",
            status=SourceStatus.ACTIVE,
        )
        session.add(source)
        session.flush()

        evidence_version = EvidenceVersion(
            source_id=source.id,
            content_hash="a" * 64,
            byte_size=1024,
            mime_type="application/pdf",
            observed_at=datetime.now(timezone.utc),
            parser_name="docling",
            parser_version="1.0.0",
            status=VersionStatus.READY,
        )
        session.add(evidence_version)
        session.flush()

        evidence_unit = EvidenceUnit(
            evidence_version_id=evidence_version.id,
            unit_index=0,
            heading="Introduction",
            content_hash="b" * 64,
        )
        session.add(evidence_unit)
        session.flush()

        recipe_id = compute_recipe_id(
            chunk_size=200, overlap=40, splitter="words",
            parser_name="docling", parser_version="1.0.0",
        )
        recipe = ChunkRecipe(
            id=recipe_id, chunk_size=200, overlap=40, splitter="words",
            parser_name="docling", parser_version="1.0.0",
        )
        session.add(recipe)
        session.flush()

        chunk_content_hash = "c" * 64
        chunk_id = compute_chunk_id(evidence_version.id, recipe.id, 0, chunk_content_hash)
        chunk = Chunk(
            id=chunk_id,
            source_id=source.id,
            evidence_version_id=evidence_version.id,
            evidence_unit_id=evidence_unit.id,
            chunk_recipe_id=recipe.id,
            ordinal=0,
            heading="Introduction",
            text=CHUNK_TEXT,
            content_hash=chunk_content_hash,
        )
        session.add(chunk)
        session.commit()

        chunk_id_value = chunk.id
        source_id_value = source.id

    return {
        "session_factory": session_factory,
        "chunk_id": chunk_id_value,
        "source_id": source_id_value,
    }


def _index_chunk(migrated_sqlite_engine: Engine, tmp_path: Path, gateway: FakeInferenceGateway, built: dict):
    record = ChunkRecord(
        chunk_id=built["chunk_id"],
        source_id=built["source_id"],
        evidence_version_id="ev_1",
        evidence_unit_id="eu_1",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text=CHUNK_TEXT,
        content_hash="hash_" + built["chunk_id"],
    )

    fts_writer = FtsIndexWriter(migrated_sqlite_engine)
    fts_writer.upsert([record], embeddings=None)

    vector_writer = LanceIndexWriter(tmp_path / "lancedb")
    embedding = gateway.embed(CHUNK_TEXT)
    vector_writer.upsert([record], embeddings=[embedding])
    return vector_writer._open_table()


def _service(
    migrated_sqlite_engine: Engine, table, gateway: FakeInferenceGateway, built: dict, *, top_k: int = 8,
    settings=None,
) -> QueryService:
    resolver = EvidenceResolver(built["session_factory"])
    extra = {"settings": settings} if settings is not None else {}
    return QueryService(
        engine=migrated_sqlite_engine,
        table=table,
        gateway=gateway,
        resolver=resolver,
        top_k=top_k,
        **extra,
    )


# ---------------------------------------------------------------------------
# Empty retrieval -> abstain without calling the gateway.
# ---------------------------------------------------------------------------


def test_empty_retrieval_abstains_without_calling_gateway(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    # No chunks indexed at all -- fts_chunks stays empty, and the lancedb
    # table is created (LanceDB needs at least one row to infer a schema)
    # then immediately emptied, so hybrid_search returns nothing regardless
    # of the question asked.
    gateway = FakeInferenceGateway()
    vector_writer = LanceIndexWriter(tmp_path / "lancedb")
    throwaway = ChunkRecord(
        chunk_id="chk_throwaway",
        source_id=built["source_id"],
        evidence_version_id="ev_1",
        evidence_unit_id="eu_1",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text="throwaway row, deleted immediately below",
        content_hash="hash_throwaway",
    )
    vector_writer.upsert([throwaway], embeddings=[gateway.embed(throwaway.text)])
    vector_writer.delete(["chk_throwaway"])
    empty_table = vector_writer._open_table()
    service = _service(migrated_sqlite_engine, empty_table, gateway, built)

    result = service.ask("What does RRF do?")

    assert result.abstained is True
    assert result.answer == ABSTENTION_PHRASE
    assert result.citations == []
    assert len(gateway.generate_calls) == 0


# ---------------------------------------------------------------------------
# Real match cases.
# ---------------------------------------------------------------------------


def test_real_match_with_valid_citation(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)

    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    gateway.canned_response = f"RRF fuses ranked lists {label}."

    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(CHUNK_TEXT)

    assert result.abstained is False
    assert len(result.citations) == 1
    assert result.citations[0].citation_label == label
    assert result.citations[0].chunk_id == built["chunk_id"]
    assert result.validation_warnings == []
    assert len(gateway.generate_calls) == 1


def test_real_match_with_no_citations_abstains_after_repair(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    gateway.canned_response = "RRF combines ranked lists, no citation given."

    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(CHUNK_TEXT)

    assert result.abstained is True
    assert result.answer == ABSTENTION_PHRASE
    assert result.citations == []
    assert len(gateway.generate_calls) == 2
    assert any("no citations" in warning for warning in result.validation_warnings)


def test_real_match_with_fabricated_citation_abstains_after_repair(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    gateway.canned_response = "RRF combines ranked lists [madeup.pdf #ffffffffffff]."

    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(CHUNK_TEXT)

    assert result.abstained is True
    assert result.answer == ABSTENTION_PHRASE
    assert len(gateway.generate_calls) == 2
    assert any("unknown citation" in warning for warning in result.validation_warnings)


def test_fast_path_result_reports_fast_mode(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    gateway.canned_response = f"RRF fuses ranked lists {label}."

    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(CHUNK_TEXT)

    assert result.mode == QueryMode.FAST.value


# ---------------------------------------------------------------------------
# Explicit `mode=` parameter on `ask()` -- bypasses the classifier entirely.
# ---------------------------------------------------------------------------


def test_explicit_mode_fast_skips_classifier_even_for_agent_phrasing(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    """A question phrased to trigger the heuristic classifier's AGENT
    routing (e.g. "compare") must still run the fast path when `mode=FAST`
    is passed explicitly -- proving `ask()` doesn't even consult
    `self._classifier` when `mode` is given (not just that it happens to
    agree with it)."""
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    gateway.canned_response = f"RRF fuses ranked lists {label}."

    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(f"Compare {CHUNK_TEXT}", mode=QueryMode.FAST)

    assert result.mode == QueryMode.FAST.value
    # Fast path calls gateway.generate() -- proves _ask_fast actually ran,
    # not just that the returned mode label says FAST.
    assert len(gateway.generate_calls) == 1


def test_explicit_mode_agent_routes_to_agent_even_for_plain_phrasing(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    """A plain single-fact-lookup question (would normally classify FAST)
    must still run the agent path when `mode=AGENT` is passed explicitly,
    using the injected fake agent rather than the default
    `HeuristicQueryClassifier` (never constructed/consulted here -- no
    `classifier=` is passed to `QueryService` at all)."""
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])
    label = f"[report.pdf #{built['chunk_id'][:12]}]"

    final_messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "read_evidence", "args": {"chunk_id": built["chunk_id"]}, "id": "call_1"}
            ],
        ),
        ToolMessage(
            content=json.dumps(
                {
                    "chunk_id": built["chunk_id"],
                    "text": CHUNK_TEXT,
                    "citation_label": label,
                    "source_display_name": "report.pdf",
                    "heading": "Introduction",
                }
            ),
            tool_call_id="call_1",
        ),
        AIMessage(content=f"RRF fuses ranked lists {label}."),
    ]
    fake_agent = _FakeAgent(final_messages)

    service = QueryService(
        engine=migrated_sqlite_engine,
        table=table,
        gateway=gateway,
        resolver=resolver,
        agent=fake_agent,
    )

    result = service.ask(CHUNK_TEXT, mode=QueryMode.AGENT)

    assert result.mode == QueryMode.AGENT.value
    assert len(fake_agent.invoke_calls) == 1
    # Agent path never calls gateway.generate() -- proves _ask_agent ran.
    assert len(gateway.generate_calls) == 0


# ---------------------------------------------------------------------------
# `_citations_from_agent_messages` -- pure, no LangGraph/LLM involved.
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Stands in for `EvidenceResolver` -- records what it was asked to
    resolve and returns a `ResolvedEvidence` per chunk_id, no DB needed."""

    def __init__(self) -> None:
        self.resolve_many_calls: list[list[str]] = []

    def resolve_many(self, chunk_ids: list[str]) -> list[ResolvedEvidence]:
        self.resolve_many_calls.append(list(chunk_ids))
        return [
            ResolvedEvidence(
                chunk_id=chunk_id,
                text=f"text for {chunk_id}",
                source_display_name="doc.pdf",
                evidence_version_id="ev_1",
                heading=None,
                citation_label=f"[doc.pdf #{chunk_id[:12]}]",
            )
            for chunk_id in chunk_ids
        ]


def test_citations_from_agent_messages_keeps_only_successful_read_evidence_calls() -> None:
    """A hand-built trace with three tool calls: one unrelated
    (search_knowledge, must be ignored regardless of its content), one
    successful read_evidence (must be kept, chunk_id extracted), and one
    failed read_evidence (an "error" JSON body, must be ignored)."""
    resolver = _FakeResolver()
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "args": {"query": "rrf"}, "id": "call_search"}],
        ),
        ToolMessage(
            content=json.dumps({"results": [{"chunk_id": "chk_abc", "score": 0.9}]}),
            tool_call_id="call_search",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "read_evidence", "args": {"chunk_id": "chk_abc"}, "id": "call_read_ok"},
                {"name": "read_evidence", "args": {"chunk_id": "chk_bogus"}, "id": "call_read_err"},
            ],
        ),
        ToolMessage(
            content=json.dumps(
                {
                    "chunk_id": "chk_abc",
                    "text": "RRF fuses ranked lists.",
                    "citation_label": "[doc.pdf #chk_abc123456]",
                    "source_display_name": "doc.pdf",
                    "heading": None,
                }
            ),
            tool_call_id="call_read_ok",
        ),
        ToolMessage(
            content=json.dumps({"error": "chunk not found: chk_bogus"}),
            tool_call_id="call_read_err",
        ),
        AIMessage(content="RRF fuses ranked lists [doc.pdf #chk_abc123456]."),
    ]

    resolved = _citations_from_agent_messages(messages, resolver)

    assert [r.chunk_id for r in resolved] == ["chk_abc"]
    assert resolver.resolve_many_calls == [["chk_abc"]]


def test_citations_from_agent_messages_dedupes_repeated_chunk_ids() -> None:
    resolver = _FakeResolver()
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_evidence", "args": {"chunk_id": "chk_abc"}, "id": "call_1"}],
        ),
        ToolMessage(
            content=json.dumps({"chunk_id": "chk_abc", "text": "t", "citation_label": "[doc.pdf #chk_abc123456]",
                                 "source_display_name": "doc.pdf", "heading": None}),
            tool_call_id="call_1",
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "read_evidence", "args": {"chunk_id": "chk_abc"}, "id": "call_2"}],
        ),
        ToolMessage(
            content=json.dumps({"chunk_id": "chk_abc", "text": "t", "citation_label": "[doc.pdf #chk_abc123456]",
                                 "source_display_name": "doc.pdf", "heading": None}),
            tool_call_id="call_2",
        ),
    ]

    resolved = _citations_from_agent_messages(messages, resolver)

    assert [r.chunk_id for r in resolved] == ["chk_abc"]
    assert resolver.resolve_many_calls == [["chk_abc"]]


def test_citations_from_agent_messages_empty_when_no_successful_read_evidence() -> None:
    resolver = _FakeResolver()
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "args": {"query": "rrf"}, "id": "call_1"}],
        ),
        ToolMessage(content=json.dumps({"results": []}), tool_call_id="call_1"),
        AIMessage(content="I don't know based on the available evidence."),
    ]

    resolved = _citations_from_agent_messages(messages, resolver)

    assert resolved == []
    assert resolver.resolve_many_calls == [[]]


# ---------------------------------------------------------------------------
# AGENT-mode `ask()` -- classifier stubbed to always route AGENT, and the
# investigation agent itself replaced with a fake object returning a canned
# message trace. No LangGraph/Ollama involved: this proves QueryService's
# AGENT branch (invoking the injected agent, extracting the final answer,
# reconstructing + validating citations from the trace) wires together
# correctly, not that a real investigation works end to end (that needs
# Ollama -- see tests/integration/test_query_service_agent_routing.py).
# ---------------------------------------------------------------------------


class _AlwaysAgentClassifier:
    def classify(self, question: str) -> QueryMode:
        return QueryMode.AGENT


class _FakeAgent:
    def __init__(self, final_messages: list) -> None:
        self._final_messages = final_messages
        self.invoke_calls: list[dict] = []

    def invoke(self, state: dict, config: dict | None = None) -> dict:
        self.invoke_calls.append(state)
        return {
            "messages": self._final_messages,
            "iterations": 1,
            "tool_calls_made": 1,
            "blocked_calls": [],
        }


def test_agent_mode_reconstructs_citations_from_real_tool_trace(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])
    label = f"[report.pdf #{built['chunk_id'][:12]}]"

    final_messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "read_evidence", "args": {"chunk_id": built["chunk_id"]}, "id": "call_1"}
            ],
        ),
        ToolMessage(
            content=json.dumps(
                {
                    "chunk_id": built["chunk_id"],
                    "text": CHUNK_TEXT,
                    "citation_label": label,
                    "source_display_name": "report.pdf",
                    "heading": "Introduction",
                }
            ),
            tool_call_id="call_1",
        ),
        AIMessage(content=f"RRF fuses ranked lists {label}."),
    ]
    fake_agent = _FakeAgent(final_messages)

    service = QueryService(
        engine=migrated_sqlite_engine,
        table=table,
        gateway=gateway,
        resolver=resolver,
        classifier=_AlwaysAgentClassifier(),
        agent=fake_agent,
    )

    result = service.ask("Compare the retrieval methods described here.")

    assert result.mode == QueryMode.AGENT.value
    assert result.answer == f"RRF fuses ranked lists {label}."
    assert len(result.citations) == 1
    assert result.citations[0].chunk_id == built["chunk_id"]
    assert result.citations[0].citation_label == label
    assert result.validation_warnings == []
    assert result.abstained is False
    # The agent branch never calls the fast path's generate() -- the answer
    # comes straight from the injected agent's own final message.
    assert len(gateway.generate_calls) == 0
    assert len(fake_agent.invoke_calls) == 1


def test_agent_mode_abstains_when_final_answer_has_no_citation(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway(canned_response="RRF fuses ranked lists without attribution.")
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    trace = [
        AIMessage(content="", tool_calls=[{"name": "read_evidence",
            "args": {"chunk_id": built["chunk_id"]}, "id": "read_1"}]),
        ToolMessage(content=json.dumps({"chunk_id": built["chunk_id"],
            "text": CHUNK_TEXT, "citation_label": label}), tool_call_id="read_1"),
        AIMessage(content="RRF fuses ranked lists without attribution."),
    ]
    service = QueryService(engine=migrated_sqlite_engine, table=table, gateway=gateway,
                           resolver=resolver, agent=_FakeAgent(trace))

    result = service.ask(CHUNK_TEXT, mode=QueryMode.AGENT)

    assert result.abstained and result.answer == ABSTENTION_PHRASE
    assert result.citations == []
    assert len(gateway.generate_calls) == 1
    assert any("no citations" in warning for warning in result.validation_warnings)


def test_agent_mode_with_no_successful_reads_yields_empty_citations_not_a_crash(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])

    final_messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "args": {"query": "rrf"}, "id": "call_1"}],
        ),
        ToolMessage(
            content=json.dumps({"results": [{"chunk_id": built["chunk_id"], "score": 0.9}]}),
            tool_call_id="call_1",
        ),
        AIMessage(content=ABSTENTION_PHRASE),
    ]
    fake_agent = _FakeAgent(final_messages)

    service = QueryService(
        engine=migrated_sqlite_engine,
        table=table,
        gateway=gateway,
        resolver=resolver,
        classifier=_AlwaysAgentClassifier(),
        agent=fake_agent,
    )

    result = service.ask("Compare the retrieval methods described here.")

    assert result.mode == QueryMode.AGENT.value
    assert result.citations == []
    assert result.abstained is True


# ---------------------------------------------------------------------------
# Conversation history (multi-turn support).
# ---------------------------------------------------------------------------

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from docket.services.query.conversation import ConversationTurn, trim_history  # noqa: E402
from docket.services.query.prompts import (  # noqa: E402
    AGENT_SYSTEM_PROMPT,
    AGENT_SYSTEM_PROMPT_WITH_HISTORY,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_WITH_HISTORY,
)

_HISTORY = [
    ConversationTurn(question="What is RRF?", answer="A fusion method."),
    ConversationTurn(question="Who wrote it?", answer="Cormack et al."),
]


def test_trim_history_empty() -> None:
    assert trim_history([]) == []
    assert trim_history(None) == []


def test_trim_history_respects_max_turns() -> None:
    turns = [ConversationTurn(question=f"q{i}", answer=f"a{i}") for i in range(10)]
    out = trim_history(turns, max_turns=3)
    assert [t.question for t in out] == ["q7", "q8", "q9"]


def test_trim_history_respects_max_chars_dropping_oldest() -> None:
    turns = [ConversationTurn(question="q" * 10, answer="a" * 10) for _ in range(4)]
    tagged = [t.model_copy(update={"question": f"{i}" + t.question[1:]}) for i, t in enumerate(turns)]
    out = trim_history(tagged, max_chars=45)  # 20 chars each -> two fit
    assert [t.question[0] for t in out] == ["2", "3"]


def test_trim_history_truncates_oversized_latest_turn_instead_of_dropping() -> None:
    turns = [
        ConversationTurn(question="old", answer="x"),
        ConversationTurn(question="big?", answer="z" * 500),
    ]
    out = trim_history(turns, max_chars=50)
    assert len(out) == 1
    assert out[0].question == "big?"
    assert out[0].answer.endswith("...")
    assert len(out[0].question) + len(out[0].answer) <= 50


def test_fast_path_with_history_builds_history_prompt(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    gateway.canned_response = f"RRF fuses ranked lists {label}."
    from docket.core.config import Settings

    service = _service(
        migrated_sqlite_engine, table, gateway, built, settings=Settings(rewrite_enabled=False)
    )

    gateway.embed_calls.clear()
    service.ask(CHUNK_TEXT, mode=QueryMode.FAST, history=_HISTORY)

    call = gateway.generate_calls[0]
    assert call["system"] == SYSTEM_PROMPT_WITH_HISTORY
    prompt = call["prompt"]
    assert prompt.startswith("Conversation so far:\n")
    assert "User: What is RRF?\nAssistant: A fusion method.\nUser: Who wrote it?" in prompt
    assert prompt.index("Conversation so far:") < prompt.index("Context:")
    assert prompt.index("Cormack et al.") < prompt.index("Context:")
    # Retrieval used the literal latest question only.
    assert gateway.embed_calls == [CHUNK_TEXT]


@pytest.mark.parametrize("history", [None, []])
def test_fast_path_without_history_is_unchanged(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict, history
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    gateway.canned_response = f"RRF fuses ranked lists {label}."
    service = _service(migrated_sqlite_engine, table, gateway, built)

    service.ask(CHUNK_TEXT, mode=QueryMode.FAST, history=history)

    call = gateway.generate_calls[0]
    assert call["system"] == SYSTEM_PROMPT
    assert call["prompt"] == f"Context:\n{label}\nSection: Introduction\n{CHUNK_TEXT}\n\nQuestion: {CHUNK_TEXT}\n\nAnswer:"
    assert "Conversation so far" not in call["prompt"]


def test_agent_path_with_history_prepends_turns(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    final_messages = [
        AIMessage(content="", tool_calls=[
            {"name": "read_evidence", "args": {"chunk_id": built["chunk_id"]}, "id": "call_1"}
        ]),
        ToolMessage(
            content=json.dumps({"chunk_id": built["chunk_id"], "text": CHUNK_TEXT,
                                "citation_label": label, "source_display_name": "report.pdf",
                                "heading": None}),
            tool_call_id="call_1",
        ),
        AIMessage(content=f"RRF fuses ranked lists {label}."),
    ]
    fake_agent = _FakeAgent(final_messages)
    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway,
        resolver=resolver, agent=fake_agent,
    )

    result = service.ask("What about X instead?", mode=QueryMode.AGENT, history=_HISTORY)

    msgs = fake_agent.invoke_calls[0]["messages"]
    assert [type(m) for m in msgs] == [
        SystemMessage, HumanMessage, AIMessage, HumanMessage, AIMessage, HumanMessage,
    ]
    assert msgs[0].content == AGENT_SYSTEM_PROMPT_WITH_HISTORY
    assert [m.content for m in msgs[1:]] == [
        "What is RRF?", "A fusion method.", "Who wrote it?", "Cormack et al.", "What about X instead?",
    ]
    assert all(not m.tool_calls for m in msgs if isinstance(m, AIMessage))
    # Injected turns don't pollute citation reconstruction.
    resolved = _citations_from_agent_messages(list(msgs) + final_messages, _FakeResolver())
    assert [r.chunk_id for r in resolved] == [built["chunk_id"]]
    assert [c.chunk_id for c in result.citations] == [built["chunk_id"]]


@pytest.mark.parametrize("history", [None, []])
def test_agent_path_without_history_is_unchanged(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict, history
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    fake_agent = _FakeAgent([AIMessage(content=ABSTENTION_PHRASE)])
    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway,
        resolver=EvidenceResolver(built["session_factory"]), agent=fake_agent,
    )

    service.ask("q?", mode=QueryMode.AGENT, history=history)

    msgs = fake_agent.invoke_calls[0]["messages"]
    assert len(msgs) == 2
    assert msgs[0].content == AGENT_SYSTEM_PROMPT


def test_revocation_during_generation_abstains(migrated_sqlite_engine, tmp_path, built):
    label = f"[report.pdf #{built['chunk_id'][:12]}]"

    class RevokingGateway(FakeInferenceGateway):
        def generate(self, **kwargs):
            with built["session_factory"]() as session:
                session.get(Source, built["source_id"]).status = SourceStatus.REVOKED
                session.commit()
            return f"RRF fuses ranked lists {label}."

    gateway = RevokingGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    result = _service(migrated_sqlite_engine, table, gateway, built).ask(CHUNK_TEXT)
    assert result.abstained and not result.citations
    assert "cited evidence is no longer available" in result.validation_warnings


def test_one_citation_repair_can_recover_valid_answer(migrated_sqlite_engine, tmp_path, built):
    label = f"[report.pdf #{built['chunk_id'][:12]}]"

    class RepairGateway(FakeInferenceGateway):
        def generate(self, **kwargs):
            super().generate(**kwargs)
            return "Missing citation." if len(self.generate_calls) == 1 else f"RRF fuses lists {label}."

    gateway = RepairGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    result = _service(migrated_sqlite_engine, table, gateway, built).ask(CHUNK_TEXT)
    assert not result.abstained and len(result.citations) == 1
    assert len(gateway.generate_calls) == 2


def test_agent_pending_tool_call_is_not_a_final_answer(migrated_sqlite_engine, tmp_path, built):
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    agent = _FakeAgent([AIMessage(content="I will read it.", tool_calls=[
        {"name": "read_evidence", "args": {"chunk_id": built["chunk_id"]}, "id": "unfinished"}
    ])])
    service = QueryService(engine=migrated_sqlite_engine, table=table, gateway=gateway,
                           resolver=EvidenceResolver(built["session_factory"]), agent=agent)
    result = service.ask(CHUNK_TEXT, mode=QueryMode.AGENT)
    assert result.abstained and result.answer == ABSTENTION_PHRASE
    assert not gateway.generate_calls


# ---------------------------------------------------------------------------
# Visual retrieval checkpoint 3 -- `page_table` threading on the FAST path.
# ---------------------------------------------------------------------------


def test_ask_fast_default_page_table_is_none_threaded_into_hybrid_search(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict, monkeypatch
) -> None:
    """`QueryService`'s `page_table` param defaults to `None`. Every
    existing caller (that doesn't know about visual retrieval) constructs
    `QueryService` without it, so `_ask_fast` must call `hybrid_search` with
    `page_table=None` -- exactly the argument that makes `hybrid_search` skip
    `visual_search` entirely (see `test_hybrid_retrieval.py`)."""
    import docket.services.query.service as service_module

    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])

    captured: dict = {}

    def _fake_hybrid_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(service_module, "hybrid_search", _fake_hybrid_search)

    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, resolver=resolver
    )
    service.ask(CHUNK_TEXT)

    assert "page_table" in captured
    assert captured["page_table"] is None


def test_ask_fast_threads_supplied_page_table_into_hybrid_search(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict, monkeypatch
) -> None:
    """When a caller does supply `page_table` (visual retrieval enabled),
    `_ask_fast` must pass that exact object through to `hybrid_search`,
    unmodified."""
    import docket.services.query.service as service_module

    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])
    sentinel_page_table = object()

    captured: dict = {}

    def _fake_hybrid_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(service_module, "hybrid_search", _fake_hybrid_search)

    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, resolver=resolver,
        page_table=sentinel_page_table,
    )
    service.ask(CHUNK_TEXT)

    assert captured["page_table"] is sentinel_page_table


# ---------------------------------------------------------------------------
# Answer-path hygiene: sampling options and token budget.
# ---------------------------------------------------------------------------


def test_fast_path_and_repair_pass_explicit_sampling(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway(canned_response="no citation here")
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    service = _service(migrated_sqlite_engine, table, gateway, built)

    service.ask(CHUNK_TEXT, mode=QueryMode.FAST)

    assert len(gateway.generate_calls) == 2  # answer + citation repair
    for call in gateway.generate_calls:
        # Qwen3's recommended thinking-mode temperature; greedy decoding (0)
        # measured worse on the public gold set (see core/config.py).
        assert call["options"] == {"temperature": 0.6}
        # Thinking stays on by default: turning it off measurably lost correct
        # answers on the public gold set (see `answer_think` in core/config.py).
        assert call["think"] is True


def _chunk(n: int, words: int = 100) -> ResolvedEvidence:
    return ResolvedEvidence(
        chunk_id=f"chk_{n}", text=("word " * words).strip(),
        source_display_name="a.pdf", evidence_version_id="ev", heading=None,
        citation_label=f"[a.pdf #chk{n}]",
    )


def _budget_service(num_ctx: int, reserve: int = 0) -> QueryService:
    from docket.core.config import Settings

    return QueryService(
        engine=None, table=None, gateway=FakeInferenceGateway(), resolver=None,  # type: ignore[arg-type]
        settings=Settings(num_ctx=num_ctx, answer_token_reserve=reserve),
    )


def test_budget_drops_lowest_ranked_chunks_first_and_keeps_order() -> None:
    chunks = [_chunk(i) for i in range(6)]
    # test counter is one token per word: ~100 per chunk, ~300 for the system prompt
    service = _budget_service(num_ctx=3000)
    _, _, included, dropped = service._fit_prompt("q?", None, chunks)
    assert included == chunks and dropped == []

    service = _budget_service(num_ctx=600)
    system, prompt, included, dropped = service._fit_prompt("q?", None, chunks)
    assert 0 < len(included) < 6
    assert included == chunks[: len(included)]
    assert dropped == [c.chunk_id for c in chunks[len(included):]]
    assert all(c.citation_label in prompt for c in included)
    assert not any(chunks[i].citation_label in prompt for i in range(len(included), 6))


def test_budget_keeps_at_least_one_chunk() -> None:
    chunks = [_chunk(i) for i in range(4)]
    _, prompt, included, dropped = _budget_service(num_ctx=10)._fit_prompt("q?", None, chunks)
    assert included == chunks[:1]
    assert len(dropped) == 3
    assert chunks[0].citation_label in prompt


def test_budget_counts_history_tokens() -> None:
    chunks = [_chunk(i) for i in range(4)]
    service = _budget_service(num_ctx=600)
    included_no_history = service._fit_prompt("q?", None, chunks)[2]
    history = [ConversationTurn(question="q " * 300, answer="a " * 300)]
    included_history = service._fit_prompt("q?", history, chunks)[2]
    assert len(included_history) < len(included_no_history)


def test_dropped_chunk_is_not_a_valid_citation() -> None:
    chunks = [_chunk(i) for i in range(4)]
    service = _budget_service(num_ctx=10)
    _, _, included, dropped = service._fit_prompt("q?", None, chunks)
    service._gateway.canned_response = f"Claim {chunks[3].citation_label}."
    result = service._finalize_answer(
        question="q?", answer=f"Claim {chunks[3].citation_label}.", resolved=included,
        mode=QueryMode.FAST, retry_system="s", retry_prompt="p", dropped_chunk_ids=dropped,
    )
    assert result.abstained and result.citations == []
    assert result.dropped_chunk_ids == dropped
    assert any("unknown citation" in w for w in result.validation_warnings)


# ---------------------------------------------------------------------------
# Follow-up query rewrite (doc 05 section 5).
# ---------------------------------------------------------------------------


class _RewriteGateway(FakeInferenceGateway):
    """Answers the rewrite call (recognised by its system prompt) separately
    from the answering call."""

    def __init__(self, rewrite=None, rewrite_error=None, **kwargs):
        super().__init__(**kwargs)
        self.rewrite = rewrite
        self.rewrite_error = rewrite_error

    def generate(self, *, system, prompt, **opts):
        from docket.services.query.prompts import REWRITE_SYSTEM_PROMPT

        if system == REWRITE_SYSTEM_PROMPT:
            self.generate_calls.append({"system": system, "prompt": prompt, **opts})
            if self.rewrite_error is not None:
                raise self.rewrite_error
            return self.rewrite
        return super().generate(system=system, prompt=prompt, **opts)


_FOLLOWUP = "And who wrote reciprocal rank fusion?"


def _rewrite_setup(engine, tmp_path, built, **gw_kwargs):
    gateway = _RewriteGateway(**gw_kwargs)
    table = _index_chunk(engine, tmp_path, gateway, built)
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    gateway.canned_response = f"RRF fuses ranked lists {label}."
    return gateway, table


def _answer_calls(gateway):
    from docket.services.query.prompts import REWRITE_SYSTEM_PROMPT

    return [c for c in gateway.generate_calls if c["system"] != REWRITE_SYSTEM_PROMPT]


def _rewrite_calls(gateway):
    from docket.services.query.prompts import REWRITE_SYSTEM_PROMPT

    return [c for c in gateway.generate_calls if c["system"] == REWRITE_SYSTEM_PROMPT]


def test_rewrite_runs_with_history_and_both_queries_are_searched(
    migrated_sqlite_engine, tmp_path, built
) -> None:
    gateway, table = _rewrite_setup(
        migrated_sqlite_engine, tmp_path, built, rewrite="Who wrote Reciprocal Rank Fusion?"
    )
    service = _service(migrated_sqlite_engine, table, gateway, built)
    gateway.embed_calls.clear()

    result = service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)

    assert result.standalone_query == "Who wrote Reciprocal Rank Fusion?"
    assert len(_rewrite_calls(gateway)) == 1
    assert gateway.embed_calls == [_FOLLOWUP, "Who wrote Reciprocal Rank Fusion?"]
    assert result.question == _FOLLOWUP


def test_rewrite_is_not_shown_to_the_answering_model(
    migrated_sqlite_engine, tmp_path, built
) -> None:
    rewritten = "Who wrote Reciprocal Rank Fusion?"
    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite=rewritten)
    service = _service(migrated_sqlite_engine, table, gateway, built)
    service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)

    (answer_call,) = _answer_calls(gateway)
    assert answer_call["system"] == SYSTEM_PROMPT_WITH_HISTORY
    assert rewritten not in answer_call["prompt"]
    assert f"Question: {_FOLLOWUP}\n\nAnswer:" in answer_call["prompt"]
    assert answer_call["prompt"].startswith("Conversation so far:\n")

    # And the context/prompt equal what the same call gives with rewrite off.
    from docket.core.config import Settings

    off_gateway = _RewriteGateway(rewrite=rewritten, canned_response=gateway.canned_response)
    off = _service(
        migrated_sqlite_engine, table, off_gateway, built, settings=Settings(rewrite_enabled=False)
    )
    off.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)
    assert _rewrite_calls(off_gateway) == []
    assert _answer_calls(off_gateway)[0]["prompt"] == answer_call["prompt"]


@pytest.mark.parametrize("history", [None, []])
def test_first_turn_is_never_rewritten(migrated_sqlite_engine, tmp_path, built, history) -> None:
    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="x y z")
    service = _service(migrated_sqlite_engine, table, gateway, built)
    gateway.embed_calls.clear()

    result = service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=history)

    assert _rewrite_calls(gateway) == []
    assert result.standalone_query is None
    assert gateway.embed_calls == [_FOLLOWUP]


def test_rewrite_disabled_setting_skips_the_call(migrated_sqlite_engine, tmp_path, built) -> None:
    from docket.core.config import Settings

    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="x y z")
    service = _service(
        migrated_sqlite_engine, table, gateway, built, settings=Settings(rewrite_enabled=False)
    )
    result = service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)
    assert _rewrite_calls(gateway) == []
    assert result.standalone_query is None


@pytest.mark.parametrize(
    "rewrite",
    [
        "",
        "   \n ",
        ABSTENTION_PHRASE,
        "line one\nline two",
        "w" * 301,
        _FOLLOWUP,
        f"  {_FOLLOWUP}  ",
    ],
)
def test_unusable_rewrite_falls_back_to_the_original_question(
    migrated_sqlite_engine, tmp_path, built, rewrite
) -> None:
    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite=rewrite)
    service = _service(migrated_sqlite_engine, table, gateway, built)
    gateway.embed_calls.clear()

    result = service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)

    assert result.standalone_query is None
    assert gateway.embed_calls == [_FOLLOWUP]
    assert result.citations  # the query still answered normally


def test_rewrite_exception_falls_back_to_the_original_question(
    migrated_sqlite_engine, tmp_path, built
) -> None:
    gateway, table = _rewrite_setup(
        migrated_sqlite_engine, tmp_path, built, rewrite_error=RuntimeError("boom")
    )
    service = _service(migrated_sqlite_engine, table, gateway, built)
    gateway.embed_calls.clear()

    result = service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)

    assert result.standalone_query is None
    assert gateway.embed_calls == [_FOLLOWUP]
    assert not result.abstained


def test_rewriter_sees_history_without_citation_tags(
    migrated_sqlite_engine, tmp_path, built
) -> None:
    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="Who wrote RRF?")
    service = _service(migrated_sqlite_engine, table, gateway, built)
    history = [
        ConversationTurn(
            question="What is RRF?",
            answer="A fusion method [report.pdf #a1b2c3d4e5f6] from 2009 [x.docx #ffff00001111].",
        )
    ]
    service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=history)

    (call,) = _rewrite_calls(gateway)
    assert "#" not in call["prompt"]
    assert "[" not in call["prompt"]
    assert "A fusion method from 2009." in call["prompt"]  # answer text kept
    assert f"Latest question: {_FOLLOWUP}" in call["prompt"]


def test_rewrite_sampling_and_model_override(migrated_sqlite_engine, tmp_path, built) -> None:
    from docket.core.config import Settings

    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="Who wrote RRF?")
    default = _service(migrated_sqlite_engine, table, gateway, built)
    default.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)
    (call,) = _rewrite_calls(gateway)
    assert call["think"] is Settings().rewrite_think
    assert call["options"] == {
        "temperature": Settings().rewrite_temperature,
        "num_predict": Settings().rewrite_num_predict,
    }
    assert "model" not in call  # None -> the generation model

    gateway.generate_calls.clear()
    custom = _service(
        migrated_sqlite_engine, table, gateway, built,
        settings=Settings(
            rewrite_model="qwen3:8b", rewrite_temperature=0.6, rewrite_think=True,
            rewrite_num_predict=999,
        ),
    )
    custom.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)
    (call,) = _rewrite_calls(gateway)
    assert call["model"] == "qwen3:8b"
    assert call["think"] is True
    assert call["options"] == {"temperature": 0.6, "num_predict": 999}
    # The answering call carries no rewrite settings.
    assert all("model" not in c for c in _answer_calls(gateway))


def test_agent_mode_computes_the_rewrite_once_and_records_it(
    migrated_sqlite_engine, tmp_path, built
) -> None:
    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="Who wrote RRF?")
    agent = _FakeAgent([AIMessage(content=ABSTENTION_PHRASE)])
    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway,
        resolver=EvidenceResolver(built["session_factory"]), agent=agent,
    )
    result = service.ask(_FOLLOWUP, mode=QueryMode.AGENT, history=_HISTORY)
    assert len(_rewrite_calls(gateway)) == 1
    assert result.standalone_query == "Who wrote RRF?"
    # cleared once the run is over, so the next question never inherits it
    assert service._run_extra_queries == []


def test_agent_mode_without_history_does_not_rewrite(migrated_sqlite_engine, tmp_path, built) -> None:
    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="Who wrote RRF?")
    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway,
        resolver=EvidenceResolver(built["session_factory"]),
        agent=_FakeAgent([AIMessage(content=ABSTENTION_PHRASE)]),
    )
    result = service.ask(_FOLLOWUP, mode=QueryMode.AGENT)
    assert _rewrite_calls(gateway) == [] and result.standalone_query is None


def test_agent_search_gets_the_rewrite_page_table_and_manifest_guard(
    migrated_sqlite_engine, tmp_path, built, monkeypatch
) -> None:
    """Agent-path parity: the lazily built agent's `search_knowledge` receives
    the same retrieval stack as the fast path (rewrite as an extra query,
    visual leg, manifest guard)."""
    import docket.services.agent.tools as tools_module
    import docket.services.query.service as service_module

    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="Who wrote RRF?")
    captured: list[dict] = []
    monkeypatch.setattr(tools_module, "hybrid_search", lambda **kw: captured.append(kw) or [])

    class _SearchingAgent:
        def __init__(self, provider_holder):
            self.holder = provider_holder

        def invoke(self, state, config=None):
            tool = tools_module.make_search_knowledge_tool(
                engine=None, table=None, gateway=None, page_table=self.holder["page_table"],
                manifest_guard=self.holder["guard"],
                extra_queries_provider=self.holder["provider"],
            )
            tool.invoke({"query": "first"})
            tool.invoke({"query": "second"})
            return {"messages": [AIMessage(content=ABSTENTION_PHRASE)], "iterations": 1,
                    "tool_calls_made": 2, "blocked_calls": []}

    holder: dict = {}

    def _fake_build(**kwargs):
        holder.update(page_table=kwargs["page_table"], guard=kwargs["manifest_guard"],
                      provider=kwargs["extra_queries_provider"])
        return _SearchingAgent(holder)

    monkeypatch.setattr(service_module, "build_investigation_agent", _fake_build)
    page_table, guard = object(), object()
    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway,
        resolver=EvidenceResolver(built["session_factory"]),
        page_table=page_table, manifest_guard=guard,
    )
    result = service.ask(_FOLLOWUP, mode=QueryMode.AGENT, history=_HISTORY)

    assert [c["query"] for c in captured] == ["first", "second"]
    assert all(c["extra_queries"] == ["Who wrote RRF?"] for c in captured)  # every call
    assert all(c["page_table"] is page_table and c["manifest_guard"] is guard for c in captured)
    assert result.standalone_query == "Who wrote RRF?"

    # a following question without history must not inherit the old rewrite
    captured.clear()
    service.ask("Plain question", mode=QueryMode.AGENT)
    assert all("extra_queries" not in c for c in captured)


def test_citations_from_agent_messages_include_read_range_and_calculate_chunks() -> None:
    resolver = _FakeResolver()
    messages = [
        AIMessage(content="", tool_calls=[
            {"name": "read_range", "args": {}, "id": "rr_ok"},
            {"name": "read_range", "args": {}, "id": "rr_err"},
            {"name": "calculate", "args": {}, "id": "calc_ok"},
            {"name": "calculate", "args": {}, "id": "calc_err"},
            {"name": "search_knowledge", "args": {}, "id": "search"},
        ]),
        ToolMessage(content=json.dumps({"rows": [], "chunk_ids": ["chk_r1", "chk_r2"],
                                        "citation_labels": ["[a.xlsx #chk_r1]", "[a.xlsx #chk_r2]"]}),
                    tool_call_id="rr_ok"),
        ToolMessage(content=json.dumps({"error": "bad range", "chunk_ids": ["chk_bad"]}), tool_call_id="rr_err"),
        ToolMessage(content=json.dumps({"result": 3, "chunk_ids": ["chk_r2", "chk_c1"]}), tool_call_id="calc_ok"),
        ToolMessage(content=json.dumps({"error": "refused", "chunk_ids": ["chk_bad2"]}), tool_call_id="calc_err"),
        ToolMessage(content=json.dumps({"chunk_ids": ["chk_search_only"]}), tool_call_id="search"),
        AIMessage(content="done"),
    ]
    _citations_from_agent_messages(messages, resolver)
    assert resolver.resolve_many_calls == [["chk_r1", "chk_r2", "chk_c1"]]  # first-seen order, deduped


def test_agent_answer_cites_a_read_range_chunk(migrated_sqlite_engine, tmp_path, built) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])
    label = f"[report.pdf #{built['chunk_id'][:12]}]"
    messages = [
        AIMessage(content="", tool_calls=[{"name": "read_range", "args": {}, "id": "c1"}]),
        ToolMessage(content=json.dumps({"chunk_ids": [built["chunk_id"]], "citation_labels": [label]}),
                    tool_call_id="c1"),
        AIMessage(content=f"The total is 7. {label}"),
    ]
    service = QueryService(
        engine=migrated_sqlite_engine, table=table, gateway=gateway, resolver=resolver,
        agent=_FakeAgent(messages),
    )
    result = service.ask("total?", mode=QueryMode.AGENT)
    assert not result.abstained and result.validation_warnings == []
    assert [c.chunk_id for c in result.citations] == [built["chunk_id"]]


def test_rewrite_recorded_when_nothing_is_retrieved(
    migrated_sqlite_engine, tmp_path, built, monkeypatch
) -> None:
    import docket.services.query.service as service_module

    gateway, table = _rewrite_setup(migrated_sqlite_engine, tmp_path, built, rewrite="Who wrote RRF?")
    captured: dict = {}

    def _fake_hybrid_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(service_module, "hybrid_search", _fake_hybrid_search)
    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(_FOLLOWUP, mode=QueryMode.FAST, history=_HISTORY)

    assert captured["extra_queries"] == ["Who wrote RRF?"]
    assert captured["query"] == _FOLLOWUP
    assert result.abstained and result.standalone_query == "Who wrote RRF?"
