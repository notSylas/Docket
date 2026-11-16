import hashlib
import json
import os
import selectors
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from docket.core.db.models import Chunk, EvidenceVersion
from docket.core.writer_lock import LOCK_FILENAME, LibraryReplacedError, WriterLock, WriterLockBusyError, lock_for_sessions
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.interfaces.cli.context import AppContext
from docket.services import backup as snapshots


@pytest.fixture
def library(tmp_path):
    context = AppContext.for_testing(
        data_dir=tmp_path / "library", gateway=FakeInferenceGateway(),
        parser=SimpleNamespace(parser_name="unused", parser_version="1"))
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "reserve.txt").write_bytes(b"The approved shipping reserve is 420 rupees.\n")
    source = context.source_manager.register_source(docs)
    context.pipeline.run_ingestion_for_source(source.id)
    (context.settings.data_dir / "preferences.json").write_text('{"consent": true}')
    yield context
    context.engine.dispose()


@contextmanager
def held(root):
    script = """
import sys
from docket.core.writer_lock import WriterLock
with WriterLock(sys.argv[1]).hold('indexing temporary fixtures'):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(root)],
        env={**os.environ, "DOCKET_DATA_DIR": str(root)}, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(5), "holder did not start"
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("suffix", ["", ".zip"])
def test_feedback_round_trip_preserves_private_permissions(library, tmp_path, suffix):
    from docket.services.feedback import FeedbackStore
    store = FeedbackStore(library.settings.data_dir, True)
    fid = store.capture("Private question", "Private answer", [], "good")
    destination = snapshots.backup(library.settings.data_dir, tmp_path / ("private" + suffix))
    assert destination.stat().st_mode & 0o077 == 0
    target = tmp_path / "restored-private"
    snapshots.restore(target, destination)
    restored = FeedbackStore(target)
    assert restored.items()[0]["id"] == fid
    assert restored.path.stat().st_mode & 0o777 == 0o600
    assert restored.path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("suffix", ["", ".zip"])
def test_round_trip_with_wal_originals_vectors_preferences_and_lock(library, tmp_path, suffix):
    root = library.settings.data_dir
    # This row is committed into the live WAL, which a raw main-file copy
    # could miss. Keep the original connection open throughout backup.
    with sqlite3.connect(root / "docket.sqlite3") as live:
        live.execute("CREATE TABLE backup_probe (value TEXT)")
        live.execute("INSERT INTO backup_probe VALUES ('WAL snapshot')")
        live.commit()
        destination = snapshots.backup(root, tmp_path / ("snapshot" + suffix))
    target = tmp_path / "restored"
    previous = snapshots.restore(target, destination)
    assert previous.is_dir()
    assert (target / "preferences.json").read_text() == '{"consent": true}'
    with sqlite3.connect(target / "docket.sqlite3") as db:
        assert db.execute("SELECT value FROM backup_probe").fetchone() == ("WAL snapshot",)
    with library.session_factory() as session:
        version = session.execute(select(EvidenceVersion)).scalar_one()
        original = library.store.get(version.content_hash)
        chunk_ids = set(session.execute(select(Chunk.id)).scalars())
    assert (target / "evidence_store" / "objects" / version.content_hash[:2] / version.content_hash).read_bytes() == original
    import lancedb

    table = lancedb.connect(str(target / "lancedb")).open_table("chunks")
    assert set(table.to_arrow()["chunk_id"].to_pylist()) == chunk_ids
    assert (target / LOCK_FILENAME).stat().st_ino == (previous / LOCK_FILENAME).stat().st_ino
    with WriterLock(target, timeout=.1).hold("post-restore fixture write"):
        pass


def test_force_retains_previous_and_refuses_nonempty(library, tmp_path):
    root = library.settings.data_dir
    snapshot = snapshots.backup(root, tmp_path / "snapshot")
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_bytes(b"old bytes")
    with pytest.raises(snapshots.BackupError, match="not empty"):
        snapshots.restore(target, snapshot)
    assert (target / "old.txt").read_bytes() == b"old bytes"
    previous = snapshots.restore(target, snapshot, force=True)
    assert (previous / "old.txt").read_bytes() == b"old bytes"
    assert not (target / "old.txt").exists()


def test_interrupted_backup_never_publishes_partial(library, tmp_path, monkeypatch):
    target = tmp_path / "interrupted"
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(snapshots, "_copy_hashed", interrupt)
    with pytest.raises(KeyboardInterrupt):
        snapshots.backup(library.settings.data_dir, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".docket-backup-*"))
    with WriterLock(library.settings.data_dir, timeout=.1).hold("retry"):
        pass


def test_corrupt_archive_and_file_hash_leave_target_unchanged(library, tmp_path):
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a zip")
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_bytes(b"safe")
    with pytest.raises(snapshots.BackupError, match="corrupt"):
        snapshots.restore(target, archive, force=True)
    snapshot = snapshots.backup(library.settings.data_dir, tmp_path / "snapshot")
    (snapshot / "preferences.json").write_bytes(b"bad")
    with pytest.raises(snapshots.BackupError, match="size|damaged"):
        snapshots.restore(target, snapshot, force=True)
    assert (target / "keep").read_bytes() == b"safe"


def test_missing_blob_even_with_updated_manifest_is_refused(library, tmp_path):
    snapshot = snapshots.backup(library.settings.data_dir, tmp_path / "snapshot")
    manifest = json.loads((snapshot / snapshots.MANIFEST).read_text())
    blob = next(name for name in manifest["files"] if name.startswith("evidence_store/objects/"))
    (snapshot / blob).unlink()
    del manifest["files"][blob]
    (snapshot / snapshots.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(snapshots.BackupError, match="original is missing"):
        snapshots.restore(tmp_path / "target", snapshot)
    assert not (tmp_path / "target" / "docket.sqlite3").exists()


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_locked_library_wait_is_bounded(library, tmp_path, operation):
    root = library.settings.data_dir
    snapshot = snapshots.backup(root, tmp_path / "snapshot")
    with held(root) as owner:
        with pytest.raises(WriterLockBusyError) as error:
            if operation == "backup":
                snapshots.backup(root, tmp_path / "blocked", writer_lock=WriterLock(root, timeout=.1))
            else:
                snapshots.restore(root, snapshot, force=True, writer_lock=WriterLock(root, timeout=.1))
        assert error.value.owner["pid"] == owner.pid
    assert not (tmp_path / "blocked").exists()


def test_swap_failure_and_postcheck_failure_keep_previous(library, tmp_path, monkeypatch):
    root = library.settings.data_dir
    snapshot = snapshots.backup(root, tmp_path / "snapshot")
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("old")
    with monkeypatch.context() as m:
        m.setattr(snapshots, "_exchange", lambda *args: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(snapshots.BackupError):
            snapshots.restore(target, snapshot, force=True)
    assert (target / "keep").read_text() == "old"
    original = snapshots.check_library
    def fail_after_swap(path, **kwargs):
        if path == target:
            raise snapshots.BackupError("postcheck failed")
        return original(path, **kwargs)
    monkeypatch.setattr(snapshots, "check_library", fail_after_swap)
    with pytest.raises(snapshots.BackupError, match="postcheck"):
        snapshots.restore(target, snapshot, force=True)
    assert (target / "keep").read_text() == "old"


def test_archive_traversal_is_rejected(library, tmp_path):
    snapshot = snapshots.backup(library.settings.data_dir, tmp_path / "snapshot")
    manifest = json.loads((snapshot / snapshots.MANIFEST).read_text())
    manifest["files"]["../escaped"] = {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
    archive = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr(snapshots.MANIFEST, json.dumps(manifest))
        for name in manifest["files"]:
            out.writestr(name, b"x" if name == "../escaped" else (snapshot / name).read_bytes())
    with pytest.raises(snapshots.BackupError, match="unsafe"):
        snapshots.restore(tmp_path / "target", archive)
    assert not (tmp_path / "escaped").exists()


def test_index_dimension_metadata_mismatch_is_refused(library, tmp_path):
    snapshot = snapshots.backup(library.settings.data_dir, tmp_path / "snapshot")
    path = snapshot / "index_manifest.json"
    index = json.loads(path.read_text())
    index["embed_dimension"] += 1
    path.write_text(json.dumps(index))
    manifest = json.loads((snapshot / snapshots.MANIFEST).read_text())
    manifest["index_manifest"] = index
    manifest["files"][path.name] = {"size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (snapshot / snapshots.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(snapshots.BackupError, match="dimensions"):
        snapshots.restore(tmp_path / "target", snapshot)


def test_old_windows_cannot_write_through_stale_handles_after_restore(library, tmp_path):
    root = library.settings.data_dir
    snapshot = snapshots.backup(root, tmp_path / "snapshot")
    old_lock = WriterLock(root)
    previous = snapshots.restore(root, snapshot, force=True)
    assert previous.is_dir()
    with pytest.raises(LibraryReplacedError, match="Restart"):
        with old_lock.hold("stale window"):
            pytest.fail("stale writer ran")
    # A lazily built service still carries the original engine's identity.
    with pytest.raises(LibraryReplacedError):
        with lock_for_sessions(library.session_factory).hold("lazy stale service"):
            pytest.fail("stale service ran")
    with WriterLock(root).hold("new window"):
        pass


def test_cli_defaults_restore_force_and_plain_errors(library, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from docket.interfaces.cli.main import app

    root = library.settings.data_dir
    monkeypatch.setenv("DOCKET_DATA_DIR", str(root))
    runner = CliRunner()
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 0, result.output
    snapshot = next(tmp_path.glob("docket-backup-*"))
    result = runner.invoke(app, ["restore", str(snapshot)])
    assert result.exit_code == 1 and "--force" in result.output
    result = runner.invoke(app, ["restore", str(snapshot), "--force"])
    assert result.exit_code == 0 and "Previous data kept" in result.output
    assert "Restart" in result.output
    result = runner.invoke(app, ["restore", str(tmp_path / "missing"), "--force"])
    assert result.exit_code == 1 and "corrupt" in result.output
    assert "Traceback" not in result.output


def test_symlinks_unlisted_files_and_destination_inside_library_refused(library, tmp_path):
    root = library.settings.data_dir
    with pytest.raises(snapshots.BackupError, match="outside"):
        snapshots.backup(root, root / "nested-backup")
    snapshot = snapshots.backup(root, tmp_path / "snapshot")
    (snapshot / "unlisted").write_text("extra")
    with pytest.raises(snapshots.BackupError, match="unlisted"):
        snapshots.restore(tmp_path / "target", snapshot)
    (snapshot / "unlisted").unlink()
    (snapshot / "escape").symlink_to(root / "docket.sqlite3")
    with pytest.raises(snapshots.BackupError, match="symbolic link"):
        snapshots.restore(tmp_path / "target", snapshot)


def test_killed_backup_process_leaves_no_published_snapshot(library, tmp_path):
    root, target = library.settings.data_dir, tmp_path / "killed-snapshot"
    script = """
import sys
from pathlib import Path
from docket.services import backup as snapshots
original = snapshots._copy_hashed
def pause(*args, **kwargs):
    print('copying', flush=True)
    sys.stdin.readline()
    return original(*args, **kwargs)
snapshots._copy_hashed = pause
snapshots.backup(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(root), str(target)],
        env={**os.environ, "DOCKET_DATA_DIR": str(root)}, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(5), "backup process did not reach the copy stage"
        assert process.stdout.readline().strip() == "copying"
        process.kill()
        process.communicate(timeout=5)
        assert not target.exists()
        assert (root / "docket.sqlite3").is_file()
        with WriterLock(root, timeout=.1).hold("after killed backup"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


# -- lighter backup: lock scope, single read pass, incremental reuse ------------


def _other_thread_can_lock(root) -> str:
    """Same-thread acquisition is re-entrant, so probe from a different thread."""
    import threading
    out = []

    def attempt():
        try:
            with WriterLock(root, timeout=0.2).hold("concurrent writer"):
                out.append("acquired")
        except WriterLockBusyError:
            out.append("busy")

    t = threading.Thread(target=attempt)
    t.start()
    t.join()
    return out[0]


def test_backup_does_not_hold_writer_lock_while_copying_evidence(library, tmp_path, monkeypatch):
    root = library.settings.data_dir
    attempts = []
    real = snapshots._copy_hashed

    def probe(source, target):
        if "objects" in target.parts:
            attempts.append(_other_thread_can_lock(root))
        return real(source, target)

    monkeypatch.setattr(snapshots, "_copy_hashed", probe)
    snapshots.backup(root, tmp_path / "snap")
    assert attempts and set(attempts) == {"acquired"}


def test_backup_holds_writer_lock_while_capturing_database(library, tmp_path, monkeypatch):
    root = library.settings.data_dir
    seen = []
    real = snapshots._copy_database

    def probe(source, copied):
        seen.append(_other_thread_can_lock(root))
        return real(source, copied)

    monkeypatch.setattr(snapshots, "_copy_database", probe)
    snapshots.backup(root, tmp_path / "snap")
    assert seen == ["busy"]


def test_backup_reads_each_stored_original_once(library, tmp_path, monkeypatch):
    root = library.settings.data_dir
    reads = []
    real_hash, real_copy = snapshots._hash, snapshots._copy_hashed
    monkeypatch.setattr(snapshots, "_hash", lambda p: (reads.append(p.name), real_hash(p))[1])
    monkeypatch.setattr(snapshots, "_copy_hashed", lambda s, t: (reads.append(s.name), real_copy(s, t))[1])
    snapshots.backup(root, tmp_path / "snap")
    with library.session_factory() as session:
        digest = session.execute(select(EvidenceVersion.content_hash)).scalar_one()
    assert reads.count(digest) == 1


def test_restore_reads_each_stored_original_once(library, tmp_path, monkeypatch):
    destination = snapshots.backup(library.settings.data_dir, tmp_path / "snap")
    with library.session_factory() as session:
        digest = session.execute(select(EvidenceVersion.content_hash)).scalar_one()
    real = snapshots._hash
    reads = []
    monkeypatch.setattr(snapshots, "_hash", lambda p: (reads.append(p.name), real(p))[1])
    snapshots.restore(tmp_path / "restored", destination)
    assert digest not in reads  # proven by the manifest pass in _read_snapshot


def test_incremental_backup_links_unchanged_originals_and_round_trips(library, tmp_path, monkeypatch):
    root = library.settings.data_dir
    first = snapshots.backup(root, tmp_path / "first")
    with library.session_factory() as session:
        digest = session.execute(select(EvidenceVersion.content_hash)).scalar_one()
    rel = Path("evidence_store") / "objects" / digest[:2] / digest

    copies = []
    real = snapshots._copy_hashed
    monkeypatch.setattr(snapshots, "_copy_hashed", lambda s, t: (copies.append(s.name), real(s, t))[1])
    second = snapshots.backup(root, tmp_path / "second", reuse_from=first)
    assert digest not in copies
    assert (second / rel).stat().st_ino == (first / rel).stat().st_ino

    manifest = json.loads((second / "backup.json").read_text())
    assert manifest["files"][rel.as_posix()]["sha256"] == digest
    target = tmp_path / "restored"
    snapshots.restore(target, second)
    assert (target / rel).read_bytes() == library.store.get(digest)


def test_incremental_backup_ignores_damaged_or_missing_previous(library, tmp_path):
    root = library.settings.data_dir
    first = snapshots.backup(root, tmp_path / "first")
    (first / "backup.json").write_text("{not json")
    second = snapshots.backup(root, tmp_path / "second", reuse_from=first)
    snapshots.restore(tmp_path / "restored", second)
    third = snapshots.backup(root, tmp_path / "third", reuse_from=tmp_path / "does-not-exist")
    assert (third / "backup.json").is_file()


def test_incremental_backup_does_not_trust_previous_object_with_wrong_size(library, tmp_path):
    root = library.settings.data_dir
    first = snapshots.backup(root, tmp_path / "first")
    with library.session_factory() as session:
        digest = session.execute(select(EvidenceVersion.content_hash)).scalar_one()
    rel = Path("evidence_store") / "objects" / digest[:2] / digest
    (first / rel).write_bytes(b"truncated")
    second = snapshots.backup(root, tmp_path / "second", reuse_from=first)
    assert (second / rel).read_bytes() == library.store.get(digest)
    assert (second / rel).stat().st_ino != (first / rel).stat().st_ino


def test_backup_detects_damaged_stored_original(library, tmp_path):
    root = library.settings.data_dir
    with library.session_factory() as session:
        digest = session.execute(select(EvidenceVersion.content_hash)).scalar_one()
    (root / "evidence_store" / "objects" / digest[:2] / digest).write_bytes(b"bit rot")
    with pytest.raises(snapshots.BackupError):
        snapshots.backup(root, tmp_path / "snap")
    assert not (tmp_path / "snap").exists()


def test_backup_survives_object_trashed_after_snapshot(library, tmp_path, monkeypatch):
    root = library.settings.data_dir
    with library.session_factory() as session:
        digest = session.execute(select(EvidenceVersion.content_hash)).scalar_one()
    real = snapshots._copy_hashed
    moved = []

    def trash_first(source, target):
        if not moved and "objects" in source.parts:
            moved.append(library.store.move_to_trash(digest))
        return real(source, target)

    monkeypatch.setattr(snapshots, "_copy_hashed", trash_first)
    out = snapshots.backup(root, tmp_path / "snap")
    assert moved and (out / "evidence_store" / "objects" / digest[:2] / digest).is_file()


def test_zip_round_trip_with_stored_originals(library, tmp_path):
    destination = snapshots.backup(library.settings.data_dir, tmp_path / "snap.zip")
    with zipfile.ZipFile(destination) as z:
        infos = [i for i in z.infolist() if "/objects/" in i.filename]
        assert infos and all(i.compress_type == zipfile.ZIP_STORED for i in infos)
    snapshots.restore(tmp_path / "restored", destination)
