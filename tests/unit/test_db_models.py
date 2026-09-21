"""Round-trip and identity tests for the Evidence Core ORM models."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attest.db.engine import get_engine, get_session_factory
from attest.db.identity import compute_chunk_id, compute_recipe_id
from attest.db.models import (
    Base,
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    IngestionJob,
    IngestionJobStatus,
    Source,
    SourceStatus,
    Workspace,
)


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    engine = get_engine(tmp_path / "attest.sqlite3")
    Base.metadata.create_all(engine)
    factory = get_session_factory(engine)
    with factory() as sess:
        yield sess
    engine.dispose()


def _build_chain(session: Session) -> dict:
    workspace = Workspace(name="Default Workspace")
    session.add(workspace)
    session.flush()

    from attest.db.models import AuthorizedSource

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
        chunk_size=200,
        overlap=40,
        splitter="words",
        parser_name="docling",
        parser_version="1.0.0",
    )
    recipe = ChunkRecipe(
        id=recipe_id,
        chunk_size=200,
        overlap=40,
        splitter="words",
        parser_name="docling",
        parser_version="1.0.0",
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
        text="This is the first chunk of the introduction.",
        content_hash=chunk_content_hash,
    )
    session.add(chunk)
    session.flush()

    job = IngestionJob(
        source_id=source.id,
        status=IngestionJobStatus.SUCCEEDED,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        stats_json='{"chunks": 1}',
    )
    session.add(job)
    session.commit()

    return {
        "workspace": workspace,
        "authorized_source": authorized_source,
        "source": source,
        "evidence_version": evidence_version,
        "evidence_unit": evidence_unit,
        "recipe": recipe,
        "chunk": chunk,
        "job": job,
    }


def test_full_chain_round_trip(session: Session) -> None:
    built = _build_chain(session)

    fetched_chunk = session.get(Chunk, built["chunk"].id)
    assert fetched_chunk is not None
    assert fetched_chunk.text == "This is the first chunk of the introduction."
    assert fetched_chunk.source_id == built["source"].id
    assert fetched_chunk.evidence_version_id == built["evidence_version"].id
    assert fetched_chunk.evidence_unit_id == built["evidence_unit"].id
    assert fetched_chunk.chunk_recipe_id == built["recipe"].id

    fetched_job = session.get(IngestionJob, built["job"].id)
    assert fetched_job is not None
    assert fetched_job.status == IngestionJobStatus.SUCCEEDED
    assert fetched_job.source_id == built["source"].id

    fetched_source = session.get(Source, built["source"].id)
    assert fetched_source is not None
    assert fetched_source.status == SourceStatus.ACTIVE
    assert len(fetched_source.evidence_versions) == 1
    assert len(fetched_source.chunks) == 1

    fetched_workspace = session.get(Workspace, built["workspace"].id)
    assert fetched_workspace is not None
    assert len(fetched_workspace.sources) == 1


def test_duplicate_chunk_id_raises_integrity_error(session: Session) -> None:
    built = _build_chain(session)
    chunk = built["chunk"]

    # Use a Core-style insert (bypasses the ORM identity map) to prove the
    # database itself, not just SQLAlchemy bookkeeping, rejects the dup.
    with pytest.raises(IntegrityError):
        session.execute(
            insert(Chunk).values(
                id=chunk.id,  # same content-derived id -> same PK
                source_id=chunk.source_id,
                evidence_version_id=chunk.evidence_version_id,
                evidence_unit_id=chunk.evidence_unit_id,
                chunk_recipe_id=chunk.chunk_recipe_id,
                ordinal=0,
                heading=chunk.heading,
                text=chunk.text,
                content_hash=chunk.content_hash,
            )
        )
        session.commit()
    session.rollback()


def test_compute_recipe_id_is_deterministic() -> None:
    kwargs = dict(
        chunk_size=200,
        overlap=40,
        splitter="words",
        parser_name="docling",
        parser_version="1.0.0",
    )
    assert compute_recipe_id(**kwargs) == compute_recipe_id(**kwargs)


def test_compute_recipe_id_changes_with_inputs() -> None:
    base = dict(
        chunk_size=200,
        overlap=40,
        splitter="words",
        parser_name="docling",
        parser_version="1.0.0",
    )
    baseline_id = compute_recipe_id(**base)

    changed = dict(base, chunk_size=300)
    assert compute_recipe_id(**changed) != baseline_id

    changed_overlap = dict(base, overlap=50)
    assert compute_recipe_id(**changed_overlap) != baseline_id

    changed_parser_version = dict(base, parser_version="1.0.1")
    assert compute_recipe_id(**changed_parser_version) != baseline_id


def test_recipe_id_has_expected_prefix() -> None:
    recipe_id = compute_recipe_id(
        chunk_size=200,
        overlap=40,
        splitter="words",
        parser_name="docling",
        parser_version="1.0.0",
    )
    assert recipe_id.startswith("rcp_")
    assert len(recipe_id) == len("rcp_") + 24


def test_compute_chunk_id_is_deterministic() -> None:
    args = ("ev_abc123", "rcp_def456", 0, "c" * 64)
    assert compute_chunk_id(*args) == compute_chunk_id(*args)


def test_compute_chunk_id_changes_with_inputs() -> None:
    baseline = compute_chunk_id("ev_abc123", "rcp_def456", 0, "c" * 64)

    assert compute_chunk_id("ev_other", "rcp_def456", 0, "c" * 64) != baseline
    assert compute_chunk_id("ev_abc123", "rcp_other", 0, "c" * 64) != baseline
    assert compute_chunk_id("ev_abc123", "rcp_def456", 1, "c" * 64) != baseline
    assert compute_chunk_id("ev_abc123", "rcp_def456", 0, "d" * 64) != baseline


def test_chunk_id_has_expected_prefix() -> None:
    chunk_id = compute_chunk_id("ev_abc123", "rcp_def456", 0, "c" * 64)
    assert chunk_id.startswith("chk_")
