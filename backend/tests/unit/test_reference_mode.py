"""Reference mode: originals stay in the user's folder; changes are detected, never hidden."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from docket.core.config import Settings
from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import Base, EvidenceVersion
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.originals import OriginalState, check_original, read_original_bytes
from docket.infra.evidence.store import ContentAddressedStore, ObjectNotFoundError
from test_evidence_manager import _make_source


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = get_engine(tmp_path / "docket.sqlite3")
    Base.metadata.create_all(engine)
    yield get_session_factory(engine)
    engine.dispose()


@pytest.fixture()
def store(tmp_path: Path) -> ContentAddressedStore:
    root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return ContentAddressedStore(root)


def _object_count(store: ContentAddressedStore) -> int:
    return sum(1 for p in store.objects_dir.rglob("*") if p.is_file())


def _ingest(manager: EvidenceManager, session_factory, path: Path) -> EvidenceVersion:
    source_id = _make_source(session_factory, path=str(path.parent))
    return manager.ingest_file(source_id, path, parser_name="plain", parser_version="1")


def test_default_is_off_and_copies_original(session_factory, store, tmp_path):
    assert Settings().reference_mode is False
    doc = tmp_path / "a.txt"
    doc.write_text("hello")
    manager = EvidenceManager(store, session_factory)
    version = _ingest(manager, session_factory, doc)
    assert store.exists(version.content_hash)
    assert check_original(
        store, content_hash=version.content_hash, file_path=str(doc),
        byte_size=version.byte_size, source_mtime=version.source_mtime,
    ).state is OriginalState.STORED


def test_reference_mode_does_not_copy_but_records_facts(session_factory, store, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("hello reference")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    version = _ingest(manager, session_factory, doc)
    assert _object_count(store) == 0
    assert not store.exists(version.content_hash)
    assert version.file_path == str(doc)
    assert version.byte_size == len("hello reference")
    assert version.source_mtime == doc.stat().st_mtime
    assert len(version.content_hash) == 64
    assert store.read_manifest(version.content_hash)["storage"] == "reference"
    assert manager.get_evidence_bytes(version) == b"hello reference"
    assert manager.original_status(version).state is OriginalState.UNCHANGED


def test_reference_mode_existing_copy_is_not_marked_reference(session_factory, store, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("same bytes")
    _ingest(EvidenceManager(store, session_factory), session_factory, doc)
    other = tmp_path / "b.txt"
    other.write_text("same bytes")
    version = _ingest(EvidenceManager(store, session_factory, reference_mode=True), session_factory, other)
    assert store.exists(version.content_hash)
    assert "storage" not in store.read_manifest(version.content_hash)


def test_deleted_file_is_missing_but_row_survives(session_factory, store, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("hello")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    version = _ingest(manager, session_factory, doc)
    doc.unlink()
    status = manager.original_status(version)
    assert status.state is OriginalState.MISSING
    assert not status.matches_indexed and "no longer at" in status.note
    with pytest.raises(ObjectNotFoundError):
        manager.get_evidence_bytes(version)
    with session_factory() as s:
        assert s.get(EvidenceVersion, version.id) is not None


def test_changed_size_and_changed_same_size_are_detected(session_factory, store, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("aaaa")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    version = _ingest(manager, session_factory, doc)

    doc.write_text("bbbb")  # same size, new mtime -> needs the hash
    os.utime(doc, (version.source_mtime + 10, version.source_mtime + 10))
    status = manager.original_status(version)
    assert status.state is OriginalState.CHANGED
    assert "changed since it was indexed" in status.note

    doc.write_text("longer content now")
    assert manager.original_status(version).state is OriginalState.CHANGED


def test_touched_file_with_same_bytes_is_unchanged(session_factory, store, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("stable")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    version = _ingest(manager, session_factory, doc)
    os.utime(doc, (version.source_mtime + 99, version.source_mtime + 99))
    assert manager.original_status(version).state is OriginalState.UNCHANGED


def test_same_mtime_and_size_skips_hashing(session_factory, store, tmp_path, monkeypatch):
    doc = tmp_path / "a.txt"
    doc.write_text("stable")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    version = _ingest(manager, session_factory, doc)

    def boom(*a, **k):
        raise AssertionError("must not hash when size and mtime match")

    monkeypatch.setattr(ContentAddressedStore, "hash_file", staticmethod(boom))
    assert manager.original_status(version).state is OriginalState.UNCHANGED


def test_read_original_bytes_never_returns_changed_content(session_factory, store, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("one")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    version = _ingest(manager, session_factory, doc)
    kwargs = dict(
        content_hash=version.content_hash, file_path=str(doc),
        byte_size=None, source_mtime=None,
    )
    assert read_original_bytes(store, **kwargs) == b"one"
    doc.write_text("two")
    assert read_original_bytes(store, **kwargs) is None


def test_reingest_after_change_creates_new_version_and_supersedes(session_factory, store, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("v1")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    first = _ingest(manager, session_factory, doc)
    doc.write_text("v2 longer")
    second = manager.ingest_file(first.source_id, doc, parser_name="plain", parser_version="1")
    assert second.id != first.id and second.content_hash != first.content_hash
    assert manager.original_status(second).state is OriginalState.UNCHANGED
    with session_factory() as s:
        assert s.get(EvidenceVersion, first.id).status.name == "SUPERSEDED"


def test_file_changing_during_ingest_is_rejected(session_factory, store, tmp_path, monkeypatch):
    doc = tmp_path / "a.txt"
    doc.write_text("original")
    manager = EvidenceManager(store, session_factory, reference_mode=True)
    real = ContentAddressedStore.hash_file

    def hash_then_modify(path, **kw):
        result = real(path, **kw)
        Path(path).write_text("modified while hashing")
        return result

    monkeypatch.setattr(ContentAddressedStore, "hash_file", staticmethod(hash_then_modify))
    source_id = _make_source(session_factory, path=str(tmp_path))
    with pytest.raises(OSError, match="changed while"):
        manager.ingest_file(source_id, doc, parser_name="plain", parser_version="1")


def test_missing_object_without_file_path_is_reported(store):
    status = check_original(store, content_hash="0" * 64, file_path=None, byte_size=None, source_mtime=None)
    assert status.state is OriginalState.MISSING


# -- end to end through the real pipeline (fake gateway, scratch data dir) ----

from types import SimpleNamespace  # noqa: E402

from sqlalchemy import select  # noqa: E402

from docket.infra.inference.gateway import FakeInferenceGateway  # noqa: E402
from docket.interfaces.cli.context import AppContext  # noqa: E402
from docket.services import backup as snapshots  # noqa: E402
from docket.services.ingestion.pipeline import RechunkCandidate  # noqa: E402


@pytest.fixture
def ref_library(tmp_path):
    ctx = AppContext.for_testing(
        data_dir=tmp_path / "library", gateway=FakeInferenceGateway(),
        parser=SimpleNamespace(parser_name="unused", parser_version="1"))
    ctx.settings.reference_mode = True
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "reserve.txt").write_bytes(b"The approved shipping reserve is 420 rupees.\n")
    source = ctx.source_manager.register_source(docs)
    ctx.pipeline.run_ingestion_for_source(source.id)
    ctx._docs, ctx._source = docs, source
    yield ctx
    ctx.engine.dispose()


def _only_version(ctx) -> EvidenceVersion:
    with ctx.session_factory() as s:
        return s.execute(select(EvidenceVersion)).scalar_one()


def test_pipeline_ingest_in_reference_mode_keeps_text_but_no_copy(ref_library):
    version = _only_version(ref_library)
    assert version.status.name == "READY"
    assert _object_count(ref_library.store) == 0
    from docket.core.db.models import Chunk
    with ref_library.session_factory() as s:
        assert s.execute(select(Chunk)).scalars().all()


def test_rechunk_uses_live_file_while_unchanged_and_refuses_when_changed(ref_library):
    version = _only_version(ref_library)
    cand = RechunkCandidate(version.id, Path(version.file_path), "stale")
    assert ref_library.pipeline.rechunk_version(ref_library._source.id, cand).status == "ingested"
    Path(version.file_path).write_bytes(b"Totally different text now.\n")
    result = ref_library.pipeline.rechunk_version(ref_library._source.id, cand)
    assert result.status == "failed" and "no longer as it was indexed" in result.error
    assert _only_version(ref_library).status.name == "READY"  # untouched


def test_backup_restore_round_trip_in_reference_mode(ref_library, tmp_path):
    dest = snapshots.backup(ref_library.settings.data_dir, tmp_path / "snap")
    target = tmp_path / "restored"
    snapshots.restore(target, dest)
    assert (target / "docket.sqlite3").is_file()
    assert not any((target / "evidence_store" / "objects").rglob("*"))
