"""Tests for `docket.services.sources.purge.SourcePurgeService` -- the
`HARD_DELETE_PENDING -> DELETED` purge job tying together Upgrade doc 03
sections 6 (source lifecycle) and 8 (blob reference counting / safe GC).

Builds a minimal but real end-to-end graph for each source under test
(`SourceManager.register_source` + `EvidenceManager.ingest_file` +
`chunk_document`/`ChunkWriter.persist_units_and_chunks` +
`IndexManager.upsert_chunks`) rather than hand-inserting ORM rows, so these
tests prove the ORM cascade-delete behavior and the real FTS5/vector index
removal, not just that `SourcePurgeService`'s own SQL runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from docket.core.db.engine import get_session_factory
from docket.core.db.models import (
    Chunk,
    EvidenceBlobReference,
    EvidenceUnit,
    EvidenceVersion,
    SourceStatus,
    VersionStatus,
)
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.references import count_blob_references
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.parsing.chunker import chunk_document
from docket.infra.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.services.ingestion.chunk_writer import ChunkWriter
from docket.services.sources.manager import SourceManager, SourceNotFoundError
from docket.services.sources.purge import SourceNotPurgeableError, SourcePurgeService


@pytest.fixture()
def store(tmp_path: Path) -> ContentAddressedStore:
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    return ContentAddressedStore(store_root)


@pytest.fixture()
def session_factory(migrated_sqlite_engine: Engine) -> sessionmaker:
    return get_session_factory(migrated_sqlite_engine)


@pytest.fixture()
def source_manager(session_factory: sessionmaker) -> SourceManager:
    return SourceManager(session_factory)


@pytest.fixture()
def evidence_manager(store: ContentAddressedStore, session_factory: sessionmaker) -> EvidenceManager:
    return EvidenceManager(store, session_factory)


@pytest.fixture()
def index_manager(migrated_sqlite_engine: Engine, tmp_path: Path) -> IndexManager:
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    gateway = FakeInferenceGateway()
    return IndexManager(fts, vector, gateway)


_RECIPE = ChunkRecipe(
    chunk_size=200, overlap=40, splitter=DEFAULT_SPLITTER, parser_name="plain", parser_version="1.0"
)


def _ingest_and_index_one_file(
    *,
    evidence_manager: EvidenceManager,
    session_factory: sessionmaker,
    index_manager: IndexManager,
    source_id: str,
    path: Path,
    text: str,
) -> EvidenceVersion:
    """Minimal real end-to-end path from bytes on disk to a `READY`
    `EvidenceVersion` with real `EvidenceUnit`/`Chunk` rows, indexed in both
    FTS5 and the vector index -- everything a purge needs to clean up."""
    path.write_text(text)
    version = evidence_manager.ingest_file(
        source_id, path, parser_name="plain", parser_version="1.0"
    )

    chunk_writer = ChunkWriter(session_factory=session_factory, chunk_recipe=_RECIPE)
    chunk_writer.ensure_recipe_row()
    units, chunks = chunk_document(text, _RECIPE)
    records = chunk_writer.persist_units_and_chunks(
        source_id=source_id, evidence_version_id=version.id, units=units, chunks=chunks
    )
    index_manager.upsert_chunks(records)
    evidence_manager.mark_version_status(version.id, VersionStatus.READY)

    with session_factory() as session:
        return session.get(EvidenceVersion, version.id)


def _register_and_queue_for_delete(
    source_manager: SourceManager, tmp_path: Path, name: str
) -> str:
    folder = tmp_path / name
    folder.mkdir()
    source = source_manager.register_source(folder)
    source_manager.deactivate_source(source.id)
    source_manager.request_hard_delete(source.id)
    return source.id


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_purge_source_unknown_id_raises_source_not_found(
    session_factory: sessionmaker, store: ContentAddressedStore
) -> None:
    service = SourcePurgeService(session_factory, store)
    with pytest.raises(SourceNotFoundError):
        service.purge_source("src_does_not_exist")


def test_purge_source_requires_hard_delete_pending_status(
    source_manager: SourceManager, session_factory: sessionmaker, store: ContentAddressedStore, tmp_path: Path
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)  # still ACTIVE
    service = SourcePurgeService(session_factory, store)

    with pytest.raises(SourceNotPurgeableError):
        service.purge_source(source.id)


# ---------------------------------------------------------------------------
# Core purge behavior: evidence rows gone, cascade to units/chunks, tombstone
# left behind on the Source row.
# ---------------------------------------------------------------------------


def test_purge_source_deletes_evidence_version_and_cascades_to_units_and_chunks(
    source_manager: SourceManager,
    evidence_manager: EvidenceManager,
    session_factory: sessionmaker,
    index_manager: IndexManager,
    store: ContentAddressedStore,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)
    version = _ingest_and_index_one_file(
        evidence_manager=evidence_manager,
        session_factory=session_factory,
        index_manager=index_manager,
        source_id=source.id,
        path=folder / "report.txt",
        text="# Heading\nSome real content about penguins and glaciers for chunking.",
    )
    with session_factory() as session:
        assert session.query(EvidenceUnit).filter_by(evidence_version_id=version.id).count() > 0
        assert session.query(Chunk).filter_by(evidence_version_id=version.id).count() > 0

    source_manager.deactivate_source(source.id)
    source_manager.request_hard_delete(source.id)
    service = SourcePurgeService(session_factory, store, index_manager=index_manager)

    result = service.purge_source(source.id)

    assert result.versions_deleted == 1
    with session_factory() as session:
        assert session.get(EvidenceVersion, version.id) is None
        assert session.query(EvidenceUnit).filter_by(evidence_version_id=version.id).count() == 0
        assert session.query(Chunk).filter_by(evidence_version_id=version.id).count() == 0
        assert session.query(Chunk).filter_by(source_id=source.id).count() == 0


def test_purge_source_sets_status_deleted_and_keeps_the_source_row_as_tombstone(
    source_manager: SourceManager, evidence_manager: EvidenceManager, session_factory: sessionmaker,
    store: ContentAddressedStore, tmp_path: Path,
) -> None:
    source_id = _register_and_queue_for_delete(source_manager, tmp_path, "docs")
    service = SourcePurgeService(session_factory, store)

    service.purge_source(source_id, reason="user_confirmed_delete")

    tombstone = source_manager.get_source(source_id)  # row still exists
    assert tombstone.status == SourceStatus.DELETED
    assert tombstone.status_reason == "user_confirmed_delete"
    assert tombstone.updated_at is not None  # doubles as the deletion timestamp


def test_purge_source_without_explicit_reason_falls_back_to_purged(
    source_manager: SourceManager, session_factory: sessionmaker, store: ContentAddressedStore, tmp_path: Path
) -> None:
    source_id = _register_and_queue_for_delete(source_manager, tmp_path, "docs")
    service = SourcePurgeService(session_factory, store)

    service.purge_source(source_id)

    assert source_manager.get_source(source_id).status_reason == "purged"


# ---------------------------------------------------------------------------
# Blob refcounting + two-phase delete tie-in (section 8), including the
# multi-reference case across two different sources.
# ---------------------------------------------------------------------------


def test_purge_source_trashes_a_blob_with_no_remaining_references(
    source_manager: SourceManager,
    evidence_manager: EvidenceManager,
    session_factory: sessionmaker,
    index_manager: IndexManager,
    store: ContentAddressedStore,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)
    version = _ingest_and_index_one_file(
        evidence_manager=evidence_manager,
        session_factory=session_factory,
        index_manager=index_manager,
        source_id=source.id,
        path=folder / "report.txt",
        text="# Heading\nUnique content only one source ever has.",
    )
    content_hash = version.content_hash
    assert store.exists(content_hash)

    source_manager.deactivate_source(source.id)
    source_manager.request_hard_delete(source.id)
    service = SourcePurgeService(session_factory, store, index_manager=index_manager)

    result = service.purge_source(source.id)

    assert content_hash in result.blobs_trashed
    assert content_hash not in result.blobs_retained
    assert not store.exists(content_hash)
    assert store.is_in_trash(content_hash)


def test_purge_source_retains_blob_still_referenced_by_another_source(
    source_manager: SourceManager,
    evidence_manager: EvidenceManager,
    session_factory: sessionmaker,
    index_manager: IndexManager,
    store: ContentAddressedStore,
    tmp_path: Path,
) -> None:
    """The multi-reference case: two sources' versions share a content_hash
    (identical bytes). Purging one must NOT remove the blob while the other
    source's version still references it -- only once BOTH are purged does
    the blob become eligible for trash."""
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    source_a = source_manager.register_source(folder_a)
    source_b = source_manager.register_source(folder_b)

    shared_text = "# Shared\nIdentical content shared across two sources."
    version_a = _ingest_and_index_one_file(
        evidence_manager=evidence_manager,
        session_factory=session_factory,
        index_manager=index_manager,
        source_id=source_a.id,
        path=folder_a / "report.txt",
        text=shared_text,
    )
    version_b = _ingest_and_index_one_file(
        evidence_manager=evidence_manager,
        session_factory=session_factory,
        index_manager=index_manager,
        source_id=source_b.id,
        path=folder_b / "report.txt",
        text=shared_text,
    )
    assert version_a.content_hash == version_b.content_hash
    content_hash = version_a.content_hash

    service = SourcePurgeService(session_factory, store, index_manager=index_manager)

    # Purge source A first: blob must survive because source B still
    # references it.
    source_manager.deactivate_source(source_a.id)
    source_manager.request_hard_delete(source_a.id)
    result_a = service.purge_source(source_a.id)

    assert content_hash in result_a.blobs_retained
    assert content_hash not in result_a.blobs_trashed
    assert store.exists(content_hash)
    assert not store.is_in_trash(content_hash)
    with session_factory() as session:
        assert count_blob_references(session, content_hash) == 1

    # Now purge source B too: nothing references it any more, so it's safe
    # to trash.
    source_manager.deactivate_source(source_b.id)
    source_manager.request_hard_delete(source_b.id)
    result_b = service.purge_source(source_b.id)

    assert content_hash in result_b.blobs_trashed
    assert not store.exists(content_hash)
    assert store.is_in_trash(content_hash)
    with session_factory() as session:
        assert count_blob_references(session, content_hash) == 0


def test_purge_source_also_removes_page_image_blob_references(
    source_manager: SourceManager,
    evidence_manager: EvidenceManager,
    session_factory: sessionmaker,
    store: ContentAddressedStore,
    tmp_path: Path,
) -> None:
    """A version's `page_images_json` hashes are a second set of references
    into the same store (section 8) -- purge must account for and trash
    those too, not just `EvidenceVersion.content_hash`."""
    from docket.services.ingestion.visual_indexer import VisualIndexer
    from docket.core.config import Settings

    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)
    path = folder / "doc.bin"
    path.write_bytes(b"primary document bytes")
    version = evidence_manager.ingest_file(source.id, path, parser_name="fixture", parser_version="1")

    indexer = VisualIndexer(
        session_factory=session_factory,
        store=store,
        gateway=None,
        visual_index_writer=None,
        settings=Settings(data_dir=tmp_path),
    )
    page_hashes = indexer.save_page_images(version.id, {1: b"page one bytes", 2: b"page two bytes"})
    evidence_manager.mark_version_status(version.id, VersionStatus.READY)

    source_manager.deactivate_source(source.id)
    source_manager.request_hard_delete(source.id)
    service = SourcePurgeService(session_factory, store)

    result = service.purge_source(source.id)

    for content_hash in page_hashes.values():
        assert content_hash in result.blobs_trashed
        assert store.is_in_trash(content_hash)
    with session_factory() as session:
        assert session.query(EvidenceBlobReference).count() == 0


# ---------------------------------------------------------------------------
# Index cleanup (FTS5 + vector)
# ---------------------------------------------------------------------------


def test_purge_source_removes_indexed_chunks_from_both_indexes(
    source_manager: SourceManager,
    evidence_manager: EvidenceManager,
    session_factory: sessionmaker,
    index_manager: IndexManager,
    store: ContentAddressedStore,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)
    _ingest_and_index_one_file(
        evidence_manager=evidence_manager,
        session_factory=session_factory,
        index_manager=index_manager,
        source_id=source.id,
        path=folder / "report.txt",
        text="# Heading\nContent that should end up indexed in both backends.",
    )
    with session_factory() as session:
        chunk_ids = set(
            session.execute(select(Chunk.id).where(Chunk.source_id == source.id)).scalars().all()
        )
    assert chunk_ids
    assert index_manager._vector.existing_chunk_ids(chunk_ids) == chunk_ids
    assert index_manager._fts.existing_chunk_ids(chunk_ids) == chunk_ids

    source_manager.deactivate_source(source.id)
    source_manager.request_hard_delete(source.id)
    service = SourcePurgeService(session_factory, store, index_manager=index_manager)
    service.purge_source(source.id)

    assert index_manager._vector.existing_chunk_ids(chunk_ids) == set()
    assert index_manager._fts.existing_chunk_ids(chunk_ids) == set()


def test_purge_source_without_index_manager_still_deletes_db_rows(
    source_manager: SourceManager,
    evidence_manager: EvidenceManager,
    session_factory: sessionmaker,
    store: ContentAddressedStore,
    tmp_path: Path,
) -> None:
    """`index_manager` is optional -- a purge must still succeed (and clean
    up the DB/blob side) for a caller that doesn't wire one up."""
    source_id = _register_and_queue_for_delete(source_manager, tmp_path, "docs")
    service = SourcePurgeService(session_factory, store)

    result = service.purge_source(source_id)  # must not raise

    assert source_manager.get_source(source_id).status == SourceStatus.DELETED
    assert result.versions_deleted == 0
