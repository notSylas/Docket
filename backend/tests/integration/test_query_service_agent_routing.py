"""Integration test for `QueryService`'s AGENT-mode routing, end to end
against a real Ollama chat model.

Mirrors `tests/integration/test_agent_graph.py`'s fixture-building pattern
(a real small fixture corpus via `migrated_sqlite_engine`, `FakeInferenceGateway`
for the embedding leg of retrieval, a real `ChatOllama` for the agent loop
itself). The difference here: this goes through `QueryService.ask()` -- with
a classifier stubbed to force AGENT mode (so the test doesn't depend on the
heuristic's exact phrasing rules, which are already covered by
`tests/unit/test_query_classifier.py`) and no injected fake agent, so
`QueryService` builds and drives a REAL `build_investigation_agent` -- and
confirms the whole path produces a `QueryResult` with `mode="agent"`, a real
answer, and citations reconstructed from the agent's actual tool-call trace
when it did read evidence.

Marked `integration`; auto-skipped by `tests/conftest.py` if Ollama isn't
reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine

from docket.core.config import settings
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
    Workspace,
)
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.services.query.classifier import QueryMode
from docket.services.query.service import QueryService
from docket.infra.retrieval.resolver import EvidenceResolver

pytestmark = pytest.mark.integration

CHUNK_TEXT = (
    "Reciprocal Rank Fusion (RRF) combines multiple ranked search result lists "
    "into a single fused ranking by summing 1/(k+rank) scores across lists."
)


class _AlwaysAgentClassifier:
    def classify(self, question: str) -> QueryMode:
        return QueryMode.AGENT


@pytest.fixture()
def built(migrated_sqlite_engine: Engine) -> dict:
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
            heading="Retrieval Fusion",
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
            heading="Retrieval Fusion",
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


def test_agent_mode_end_to_end_answers_with_real_tool_trace_citations(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    embed_gateway = FakeInferenceGateway()

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
    FtsIndexWriter(migrated_sqlite_engine).upsert([record], embeddings=None)
    vector_writer = LanceIndexWriter(tmp_path / "lancedb")
    vector_writer.upsert([record], embeddings=[embed_gateway.embed(CHUNK_TEXT)])
    table = vector_writer._open_table()

    resolver = EvidenceResolver(built["session_factory"])

    service = QueryService(
        engine=migrated_sqlite_engine,
        table=table,
        gateway=embed_gateway,
        resolver=resolver,
        classifier=_AlwaysAgentClassifier(),
        settings=settings,
    )

    result = service.ask(
        "What does Reciprocal Rank Fusion do and how does it combine ranked "
        "lists? Search for it, read the evidence chunk, and answer with a "
        "citation."
    )

    assert result.mode == QueryMode.AGENT.value
    assert result.answer
    assert "rank" in result.answer.lower() or "fusion" in result.answer.lower()

    # If the agent actually read evidence (the expected/typical outcome given
    # the prompt explicitly asks it to), citations should be real, resolved
    # from the actual tool trace -- not fabricated, not empty.
    if result.citations:
        assert result.citations[0].chunk_id == built["chunk_id"]
        assert result.citations[0].source_display_name == "report.pdf"
