"""Unit tests for `attest.query.service.QueryService`.

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

from attest.db.engine import get_session_factory
from attest.db.identity import compute_chunk_id, compute_recipe_id
from attest.db.models import (
    AuthorizedSource,
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    Source,
    SourceStatus,
    Workspace,
)
from attest.index.base import ChunkRecord
from attest.index.fts_index import FtsIndexWriter
from attest.index.vector_index import LanceIndexWriter
from attest.inference.gateway import FakeInferenceGateway
from attest.query.classifier import QueryMode
from attest.query.prompts import ABSTENTION_PHRASE
from attest.query.service import QueryService, _citations_from_agent_messages
from attest.retrieval.resolver import EvidenceResolver, ResolvedEvidence

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
    migrated_sqlite_engine: Engine, table, gateway: FakeInferenceGateway, built: dict, *, top_k: int = 8
) -> QueryService:
    resolver = EvidenceResolver(built["session_factory"])
    return QueryService(
        engine=migrated_sqlite_engine,
        table=table,
        gateway=gateway,
        resolver=resolver,
        top_k=top_k,
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


def test_real_match_with_no_citations_warns_uncited(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    gateway.canned_response = "RRF combines ranked lists, no citation given."

    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(CHUNK_TEXT)

    assert result.abstained is False
    assert result.citations == []
    assert any("no citations" in warning for warning in result.validation_warnings)


def test_real_match_with_fabricated_citation_warns_unknown(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    gateway.canned_response = "RRF combines ranked lists [madeup.pdf #ffffffffffff]."

    service = _service(migrated_sqlite_engine, table, gateway, built)
    result = service.ask(CHUNK_TEXT)

    assert result.abstained is False
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
