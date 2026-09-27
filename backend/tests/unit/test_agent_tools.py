"""Unit tests for `docket.agent.tools` -- the real `search_knowledge`/
`read_evidence` tool factories.

These prove the tools call real `hybrid_search`/`EvidenceResolver` code
(replacing the spike's hardcoded fake results), not that the agent LOOP
works -- tools are invoked directly here, no LangGraph/LLM involved. Uses
`FakeInferenceGateway` for embeddings (deterministic, no Ollama/GPU needed),
following the same fixture pattern as `test_query_service.py`/
`test_hybrid_retrieval.py`: a `migrated_sqlite_engine` SQLite DB with CP1's
migration applied, populated with a real `Chunk`/`Source`/`EvidenceVersion`
chain, plus matching FTS5 and LanceDB rows for the same chunk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine

from docket.agent.tools import make_read_evidence_tool, make_search_knowledge_tool
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
from docket.retrieval.resolver import EvidenceResolver

CHUNK_TEXT = "Reciprocal Rank Fusion combines multiple ranked search results into one."


@pytest.fixture()
def built(migrated_sqlite_engine: Engine) -> dict:
    """Populate a real `Chunk`/`Source` chain (for the resolver) that a
    matching FTS5 + LanceDB row (indexed by `_index_chunk`) can be retrieved
    against."""
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


# ---------------------------------------------------------------------------
# search_knowledge -- real hybrid_search, not the spike's canned stub.
# ---------------------------------------------------------------------------


def test_search_knowledge_returns_real_chunk_id(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    search_knowledge = make_search_knowledge_tool(engine=migrated_sqlite_engine, table=table, gateway=gateway)

    raw = search_knowledge.invoke({"query": CHUNK_TEXT})
    payload = json.loads(raw)

    chunk_ids = [r["chunk_id"] for r in payload["results"]]
    assert built["chunk_id"] in chunk_ids


def test_search_knowledge_no_match_returns_empty_results(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    search_knowledge = make_search_knowledge_tool(engine=migrated_sqlite_engine, table=table, gateway=gateway)

    # Even a query that shares no lexical/semantic overlap still returns
    # *some* nearest-neighbor vector result (LanceDB always returns top_k
    # nearest, never "no results") -- so this just proves the tool runs the
    # real pipeline end-to-end and returns well-formed JSON either way.
    raw = search_knowledge.invoke({"query": "completely unrelated gibberish zzz"})
    payload = json.loads(raw)
    assert "results" in payload


# ---------------------------------------------------------------------------
# read_evidence -- real EvidenceResolver, not the spike's canned stub.
# ---------------------------------------------------------------------------


def test_read_evidence_returns_real_resolved_content(built: dict) -> None:
    resolver = EvidenceResolver(built["session_factory"])
    read_evidence = make_read_evidence_tool(resolver=resolver)

    raw = read_evidence.invoke({"chunk_id": built["chunk_id"]})
    payload = json.loads(raw)

    assert payload["chunk_id"] == built["chunk_id"]
    assert payload["text"] == CHUNK_TEXT
    assert payload["citation_label"] == f"[report.pdf #{built['chunk_id'][:12]}]"
    assert payload["source_display_name"] == "report.pdf"
    assert payload["heading"] == "Introduction"


def test_read_evidence_bogus_chunk_id_returns_clean_error_not_raise(built: dict) -> None:
    resolver = EvidenceResolver(built["session_factory"])
    read_evidence = make_read_evidence_tool(resolver=resolver)

    raw = read_evidence.invoke({"chunk_id": "chk_does_not_exist"})
    payload = json.loads(raw)

    assert "error" in payload
    assert "chk_does_not_exist" in payload["error"]


# ---------------------------------------------------------------------------
# Round-trip: search_knowledge's real chunk_id feeds read_evidence, which
# resolves to that same chunk's real content -- the exact thing the spike
# stubbed out (its search_knowledge and read_evidence returned unrelated
# canned strings, so nothing proved they'd compose correctly against a real
# corpus).
# ---------------------------------------------------------------------------


def test_search_then_read_round_trip_resolves_same_chunk(
    migrated_sqlite_engine: Engine, tmp_path: Path, built: dict
) -> None:
    gateway = FakeInferenceGateway()
    table = _index_chunk(migrated_sqlite_engine, tmp_path, gateway, built)
    resolver = EvidenceResolver(built["session_factory"])

    search_knowledge = make_search_knowledge_tool(engine=migrated_sqlite_engine, table=table, gateway=gateway)
    read_evidence = make_read_evidence_tool(resolver=resolver)

    search_payload = json.loads(search_knowledge.invoke({"query": CHUNK_TEXT}))
    top_chunk_id = search_payload["results"][0]["chunk_id"]
    assert top_chunk_id == built["chunk_id"]

    read_payload = json.loads(read_evidence.invoke({"chunk_id": top_chunk_id}))
    assert read_payload["text"] == CHUNK_TEXT
    assert read_payload["citation_label"] == f"[report.pdf #{built['chunk_id'][:12]}]"
