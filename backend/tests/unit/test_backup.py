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
    monkeypatch.setattr(snapshots.shutil, "copyfile", interrupt)
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
original = snapshots.shutil.copyfile
def pause(*args, **kwargs):
    print('copying', flush=True)
    sys.stdin.readline()
    return original(*args, **kwargs)
snapshots.shutil.copyfile = pause
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
