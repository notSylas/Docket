"""Tests for EvidenceManager -- filesystem store + a tmp_path-scoped SQLite DB."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import Base, EvidenceVersion, Source, SourceStatus, Workspace
from docket.evidence.manager import EvidenceManager
from docket.evidence.store import ContentAddressedStore


@pytest.fixture()
def session_factory(tmp_path: Path) -> sessionmaker:
    engine = get_engine(tmp_path / "docket.sqlite3")
    Base.metadata.create_all(engine)
    factory = get_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture()
def store(tmp_path: Path) -> ContentAddressedStore:
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    return ContentAddressedStore(store_root)


@pytest.fixture()
def manager(store: ContentAddressedStore, session_factory: sessionmaker) -> EvidenceManager:
    return EvidenceManager(store, session_factory)


def _make_source(session_factory: sessionmaker, *, path: str = "/docs/report.txt") -> str:
    with session_factory() as session:
        workspace = Workspace(name="Default Workspace")
        session.add(workspace)
        session.flush()

        from docket.core.db.models import AuthorizedSource

        authorized_source = AuthorizedSource(workspace_id=workspace.id, scope_path="/docs")
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
        session.commit()
        return source.id


def _count_evidence_versions(session_factory: sessionmaker, source_id: str) -> int:
    with session_factory() as session:
        return (
            session.query(EvidenceVersion)
            .filter(EvidenceVersion.source_id == source_id)
            .count()
        )


def test_first_ingest_creates_current_version(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello world")

    version = manager.ingest_file(
        source_id, file_path, parser_name="plain", parser_version="1.0.0"
    )

    assert version.is_current is True
    assert version.source_id == source_id
    assert _count_evidence_versions(session_factory, source_id) == 1


def test_reingest_unchanged_file_is_noop(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello world")

    first = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")
    second = manager.ingest_file(
        source_id, file_path, parser_name="plain", parser_version="1.0.0"
    )

    assert first.id == second.id
    assert _count_evidence_versions(session_factory, source_id) == 1


def test_reingest_changed_file_creates_new_current_version(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("version one")

    first = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    file_path.write_text("version two -- changed content")
    second = manager.ingest_file(
        source_id, file_path, parser_name="plain", parser_version="1.0.0"
    )

    assert second.id != first.id
    assert second.is_current is True
    assert second.content_hash != first.content_hash
    assert _count_evidence_versions(session_factory, source_id) == 2

    with session_factory() as session:
        refreshed_first = session.get(EvidenceVersion, first.id)
        refreshed_second = session.get(EvidenceVersion, second.id)
        assert refreshed_first.is_current is False
        assert refreshed_second.is_current is True


def test_get_evidence_bytes_returns_original_content(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    original_content = b"the quick brown fox"
    file_path.write_bytes(original_content)

    version = manager.ingest_file(
        source_id, file_path, parser_name="plain", parser_version="1.0.0"
    )

    assert manager.get_evidence_bytes(version) == original_content


def test_identical_content_across_sources_dedupes_in_store_not_in_db(
    manager: EvidenceManager, session_factory: sessionmaker, store: ContentAddressedStore, tmp_path: Path
) -> None:
    source_a_id = _make_source(session_factory, path="/docs/a.txt")
    source_b_id = _make_source(session_factory, path="/docs/b.txt")

    shared_content = b"identical bytes shared across two files"
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_bytes(shared_content)
    file_b.write_bytes(shared_content)

    version_a = manager.ingest_file(
        source_a_id, file_a, parser_name="plain", parser_version="1.0.0"
    )
    version_b = manager.ingest_file(
        source_b_id, file_b, parser_name="plain", parser_version="1.0.0"
    )

    # Per-source EvidenceVersion rows are distinct...
    assert version_a.id != version_b.id
    assert version_a.content_hash == version_b.content_hash
    assert _count_evidence_versions(session_factory, source_a_id) == 1
    assert _count_evidence_versions(session_factory, source_b_id) == 1

    # ...but the underlying store only has one object on disk for that hash.
    object_files = [p for p in store.objects_dir.rglob("*") if p.is_file()]
    assert len(object_files) == 1
