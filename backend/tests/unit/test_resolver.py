"""Unit tests for `docket.retrieval.resolver.EvidenceResolver`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from docket.db.engine import get_engine, get_session_factory
from docket.db.identity import compute_chunk_id, compute_recipe_id
from docket.db.models import (
    AuthorizedSource,
    Base,
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    Source,
    SourceStatus,
    Workspace,
)
from docket.retrieval.resolver import ChunkNotFoundError, EvidenceResolver, ResolvedEvidence


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    engine = get_engine(tmp_path / "docket.sqlite3")
    Base.metadata.create_all(engine)
    factory = get_session_factory(engine)
    with factory() as sess:
        yield sess
    engine.dispose()


def _build_chain(session: Session, *, path: str = "/home/user/Documents/report.pdf") -> dict:
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
        path=path,
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
    session.commit()

    return {
        "workspace": workspace,
        "authorized_source": authorized_source,
        "source": source,
        "evidence_version": evidence_version,
        "evidence_unit": evidence_unit,
        "recipe": recipe,
        "chunk": chunk,
    }


def _add_second_chunk(session: Session, built: dict, *, ordinal: int = 1) -> Chunk:
    chunk_content_hash = "d" * 64
    chunk_id = compute_chunk_id(
        built["evidence_version"].id, built["recipe"].id, ordinal, chunk_content_hash
    )
    chunk = Chunk(
        id=chunk_id,
        source_id=built["source"].id,
        evidence_version_id=built["evidence_version"].id,
        evidence_unit_id=built["evidence_unit"].id,
        chunk_recipe_id=built["recipe"].id,
        ordinal=ordinal,
        heading=None,
        text="A second chunk with different content.",
        content_hash=chunk_content_hash,
    )
    session.add(chunk)
    session.commit()
    return chunk


def _resolver(session: Session) -> EvidenceResolver:
    # EvidenceResolver takes a session_factory (callable -> Session), so wrap
    # this fixture's already-open session in a trivial factory that returns
    # it (tests use a single session for setup + resolution, same pattern as
    # test_db_models.py).
    class _Factory:
        def __call__(self) -> Session:
            return session

    return EvidenceResolver(_Factory())


def test_resolve_returns_expected_evidence(session: Session) -> None:
    built = _build_chain(session)
    resolver = _resolver(session)

    resolved = resolver.resolve(built["chunk"].id)

    assert isinstance(resolved, ResolvedEvidence)
    assert resolved.chunk_id == built["chunk"].id
    assert resolved.text == "This is the first chunk of the introduction."
    assert resolved.heading == "Introduction"
    assert resolved.source_display_name == "report.pdf"
    assert resolved.evidence_version_id == built["evidence_version"].id


def test_resolve_unknown_chunk_id_raises(session: Session) -> None:
    _build_chain(session)
    resolver = _resolver(session)

    with pytest.raises(ChunkNotFoundError) as exc_info:
        resolver.resolve("chk_does_not_exist")
    assert exc_info.value.chunk_id == "chk_does_not_exist"


def test_citation_label_exact_format(session: Session) -> None:
    built = _build_chain(session)
    resolver = _resolver(session)

    resolved = resolver.resolve(built["chunk"].id)

    expected = f"[report.pdf #{built['chunk'].id[:12]}]"
    assert resolved.citation_label == expected


def test_resolve_many_preserves_input_order(session: Session) -> None:
    built = _build_chain(session)
    second = _add_second_chunk(session, built)
    resolver = _resolver(session)

    # Request in reverse-of-insertion order to prove order isn't incidental.
    ordered_ids = [second.id, built["chunk"].id]
    resolved = resolver.resolve_many(ordered_ids)

    assert [r.chunk_id for r in resolved] == ordered_ids


def test_resolve_many_raises_on_first_missing_id(session: Session) -> None:
    built = _build_chain(session)
    resolver = _resolver(session)

    with pytest.raises(ChunkNotFoundError) as exc_info:
        resolver.resolve_many([built["chunk"].id, "chk_missing"])
    assert exc_info.value.chunk_id == "chk_missing"


def test_resolve_many_empty_list_returns_empty(session: Session) -> None:
    _build_chain(session)
    resolver = _resolver(session)
    assert resolver.resolve_many([]) == []


def test_revoked_chunk_cannot_be_resolved_directly(session: Session) -> None:
    built = _build_chain(session)
    chunk_id = built["chunk"].id
    built["source"].status = SourceStatus.REVOKED
    session.commit()

    with pytest.raises(ChunkNotFoundError):
        _resolver(session).resolve(chunk_id)


def test_superseded_chunk_cannot_be_resolved_directly(session: Session) -> None:
    built = _build_chain(session)
    chunk_id = built["chunk"].id
    built["evidence_version"].is_current = False
    session.commit()

    with pytest.raises(ChunkNotFoundError):
        _resolver(session).resolve(chunk_id)


def test_read_evidence_tool_rejects_revoked_id(session: Session) -> None:
    import json
    from docket.agent.tools import make_read_evidence_tool
    built = _build_chain(session)
    chunk_id = built["chunk"].id
    built["source"].status = SourceStatus.REVOKED
    session.commit()
    payload = json.loads(make_read_evidence_tool(resolver=_resolver(session)).invoke({"chunk_id": chunk_id}))
    assert "error" in payload and "text" not in payload
