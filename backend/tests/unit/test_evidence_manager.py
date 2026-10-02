"""Tests for EvidenceManager -- filesystem store + a tmp_path-scoped SQLite DB."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import (
    Base,
    EvidenceBlobReference,
    EvidenceVersion,
    Source,
    SourceStatus,
    VersionStatus,
    Workspace,
)
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.references import count_blob_references
from docket.infra.evidence.store import ContentAddressedStore


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

    # EvidenceManager only stores bytes -- it never parses/chunks/indexes --
    # so a fresh version starts PENDING, not READY. See Upgrade doc 03
    # section 4.
    assert version.status == VersionStatus.PENDING
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
    assert second.status == VersionStatus.PENDING
    assert second.content_hash != first.content_hash
    assert _count_evidence_versions(session_factory, source_id) == 2

    with session_factory() as session:
        refreshed_first = session.get(EvidenceVersion, first.id)
        refreshed_second = session.get(EvidenceVersion, second.id)
        assert refreshed_first.status == VersionStatus.SUPERSEDED
        assert refreshed_second.status == VersionStatus.PENDING


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


# ---------------------------------------------------------------------------
# VersionStatus lifecycle (Upgrade doc 03 section 4): PENDING -> READY |
# FAILED, with SUPERSEDED reachable directly from any of the three.
# ---------------------------------------------------------------------------


def _set_status(session_factory: sessionmaker, version_id: str, status: VersionStatus) -> None:
    with session_factory() as session:
        version = session.get(EvidenceVersion, version_id)
        version.status = status
        session.add(version)
        session.commit()


@pytest.mark.parametrize("prior_status", [VersionStatus.PENDING, VersionStatus.READY, VersionStatus.FAILED])
def test_reingest_changed_content_supersedes_regardless_of_prior_status(
    manager: EvidenceManager,
    session_factory: sessionmaker,
    tmp_path: Path,
    prior_status: VersionStatus,
) -> None:
    """Supersession always wins immediately, no matter where the old row was
    in its own lifecycle -- a still-PENDING or already-FAILED row must flip
    straight to SUPERSEDED the same as a READY one would, per the doc's
    explicit "reachable directly from PENDING, READY, or FAILED" rule."""
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("version one")

    first = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")
    _set_status(session_factory, first.id, prior_status)

    file_path.write_text("version two -- changed content")
    second = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    assert second.id != first.id
    assert second.status == VersionStatus.PENDING
    with session_factory() as session:
        refreshed_first = session.get(EvidenceVersion, first.id)
        assert refreshed_first.status == VersionStatus.SUPERSEDED


@pytest.mark.parametrize("prior_status", [VersionStatus.PENDING, VersionStatus.FAILED])
def test_reingest_unchanged_content_returns_same_row_without_resetting_status(
    manager: EvidenceManager,
    session_factory: sessionmaker,
    tmp_path: Path,
    prior_status: VersionStatus,
) -> None:
    """EvidenceManager itself never decides "no-op vs retry" -- it always
    hands back the same row for unchanged bytes, leaving that decision to the
    pipeline (which checks `status`). Proves the manager doesn't quietly
    reset or otherwise touch a PENDING/FAILED row's status just because the
    same content was re-ingested."""
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello world")

    first = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")
    _set_status(session_factory, first.id, prior_status)

    second = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    assert second.id == first.id
    assert second.status == prior_status
    assert _count_evidence_versions(session_factory, source_id) == 1


def test_mark_version_status_transitions_to_ready(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello world")
    version = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")
    assert version.status == VersionStatus.PENDING

    manager.mark_version_status(version.id, VersionStatus.READY)

    with session_factory() as session:
        assert session.get(EvidenceVersion, version.id).status == VersionStatus.READY


def test_mark_version_status_transitions_to_failed(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello world")
    version = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    manager.mark_version_status(version.id, VersionStatus.FAILED)

    with session_factory() as session:
        assert session.get(EvidenceVersion, version.id).status == VersionStatus.FAILED


def test_mark_version_status_noops_when_already_superseded(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    """If a processing job is still running against a row that gets
    superseded mid-flight (newer content stored for the same file while the
    old row was still being processed), the in-flight job's eventual
    READY/FAILED write must not clobber SUPERSEDED -- supersession always
    wins, regardless of how the stale processing job turns out."""
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("version one")
    first = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    file_path.write_text("version two -- changed content")
    manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    with session_factory() as session:
        assert session.get(EvidenceVersion, first.id).status == VersionStatus.SUPERSEDED

    # A (simulated) stale in-flight processing job for the now-superseded
    # `first` version finally finishes and tries to report its outcome.
    manager.mark_version_status(first.id, VersionStatus.READY)

    with session_factory() as session:
        assert session.get(EvidenceVersion, first.id).status == VersionStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# Blob reference counting (Upgrade doc 03 section 8): `ingest_file` writes an
# `EvidenceBlobReference` row in the same transaction as the blob/version
# write.
# ---------------------------------------------------------------------------


def test_ingest_file_writes_a_content_blob_reference(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello world")

    version = manager.ingest_file(
        source_id, file_path, parser_name="plain", parser_version="1.0.0"
    )

    with session_factory() as session:
        refs = (
            session.query(EvidenceBlobReference)
            .filter(EvidenceBlobReference.referencing_id == version.id)
            .all()
        )
        assert len(refs) == 1
        assert refs[0].content_hash == version.content_hash
        assert refs[0].referencing_table == "evidence_versions"
        assert refs[0].role == "content"
        assert count_blob_references(session, version.content_hash) == 1


def test_reingest_changed_content_adds_a_second_reference_not_replacing_the_first(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    """Supersession flips the old version's `status` but its blob reference
    row must survive -- the old content_hash is still "referenced" by that
    (superseded, but not yet purged) row until a purge actually removes it."""
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("version one")
    first = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    file_path.write_text("version two -- changed content")
    second = manager.ingest_file(
        source_id, file_path, parser_name="plain", parser_version="1.0.0"
    )

    with session_factory() as session:
        assert count_blob_references(session, first.content_hash) == 1
        assert count_blob_references(session, second.content_hash) == 1


def test_identical_content_across_sources_gets_one_reference_row_each(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    """Two different sources ingesting byte-identical content dedupe in the
    store (one object on disk) but each gets its OWN blob reference row --
    the multi-reference case a purge must respect (deleting one source's
    version must not remove the blob while the other source's version still
    references it)."""
    source_a_id = _make_source(session_factory, path="/docs/a.txt")
    source_b_id = _make_source(session_factory, path="/docs/b.txt")
    shared_content = b"identical bytes shared across two sources"
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

    assert version_a.content_hash == version_b.content_hash
    with session_factory() as session:
        assert count_blob_references(session, version_a.content_hash) == 2


def test_reingest_unchanged_file_does_not_duplicate_reference_rows(
    manager: EvidenceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source_id = _make_source(session_factory)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello world")

    first = manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")
    manager.ingest_file(source_id, file_path, parser_name="plain", parser_version="1.0.0")

    with session_factory() as session:
        assert count_blob_references(session, first.content_hash) == 1
