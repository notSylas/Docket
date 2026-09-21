"""Unit tests for `attest.query.service.QueryService`.

Uses `FakeInferenceGateway` throughout -- no Ollama/GPU dependency. The
retrieval side is real (a `migrated_sqlite_engine` SQLite DB with CP1's
migration applied, populated with a real `Chunk`/`Source` chain plus matching
FTS5 and LanceDB rows), following the same fixture pattern as
`test_resolver.py` (ORM chain) and `test_hybrid_retrieval.py` (FTS/vector
population).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
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
from attest.query.prompts import ABSTENTION_PHRASE
from attest.query.service import QueryService
from attest.retrieval.resolver import EvidenceResolver

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
