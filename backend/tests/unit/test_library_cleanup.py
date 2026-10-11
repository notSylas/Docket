from datetime import datetime, timedelta, timezone
import os
import select
import subprocess
import sys

import pytest
from typer.testing import CliRunner
from docket.core.db.models import SourceStatus
from docket.core.writer_lock import WriterLock, WriterLockBusyError
from docket.interfaces.cli.context import AppContext
from docket.services.library_cleanup import LibraryCleanup, CleanupError
from docket.services.sources.purge import SourcePurgeService


@pytest.fixture
def env(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data")
    ctx.__dict__["purge_service"] = SourcePurgeService(ctx.session_factory, ctx.store)
    cleanup = LibraryCleanup(ctx.session_factory, ctx.source_manager, ctx.purge_service, ctx.store,
                             ctx.settings, writer_lock=WriterLock(ctx.settings.data_dir, timeout=.05))
    folder = tmp_path / "originals"
    folder.mkdir(); (folder / "untouched.txt").write_text("Private original")
    src = ctx.source_manager.register_source(folder, display_name="Reports")
    yield ctx, cleanup, src, folder
    ctx.engine.dispose()


def test_purge_requires_disconnect_preserves_originals_and_history_tombstone(env):
    ctx, cleanup, src, folder = env
    with pytest.raises(CleanupError, match="Disconnect"):
        cleanup.purge_disconnected(src.id)
    ctx.source_manager.deactivate_source(src.id)
    cleanup.purge_disconnected(src.id)
    assert ctx.source_manager.get_source(src.id).status == SourceStatus.DELETED
    assert (folder / "untouched.txt").read_text() == "Private original"


def test_clean_is_dry_by_default_and_does_not_purge_disconnected_or_live_sources(env):
    ctx, cleanup, src, folder = env
    ctx.source_manager.mark_unreachable(src.id)
    ctx.source_manager.mark_tombstoned(src.id, retention_days=-1)
    other = folder.parent / "other"; other.mkdir()
    retained = ctx.source_manager.register_source(other)
    ctx.source_manager.deactivate_source(retained.id)
    old = ctx.store.put(b"old trash")
    ctx.store.move_to_trash(old)
    path = ctx.store.trash_dir / old
    old_time = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(path, (old_time, old_time))
    plan = cleanup.clean()
    assert plan.source_ids == (src.id,) and plan.trash_objects == (old,) and not plan.applied
    assert path.exists() and ctx.source_manager.get_source(src.id).status == SourceStatus.TOMBSTONED
    done = cleanup.clean(apply=True)
    assert done.applied and not path.exists()
    assert ctx.source_manager.get_source(src.id).status == SourceStatus.DELETED
    assert ctx.source_manager.get_source(retained.id).status == SourceStatus.REVOKED


def test_cleanup_wait_is_bounded_and_does_not_change_locked_library(env):
    ctx, cleanup, src, _ = env
    code = "from pathlib import Path; import sys; from docket.core.writer_lock import WriterLock\nwith WriterLock(Path(sys.argv[1])).hold('another library update'):\n print('ready',flush=True)\n sys.stdin.read()"
    proc = subprocess.Popen([sys.executable, "-c", code, str(ctx.settings.data_dir)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 5)[0]
        assert proc.stdout.readline().strip() == "ready"
        with pytest.raises(WriterLockBusyError):
            cleanup.clean(apply=True)
        assert ctx.source_manager.get_source(src.id).status == SourceStatus.ACTIVE
    finally:
        proc.terminate(); proc.wait(timeout=5)


def test_cli_purge_confirmation_and_clean_preview(env, monkeypatch):
    from docket.interfaces.cli import main
    ctx, _, src, folder = env
    monkeypatch.setattr(main, "build_context", lambda: ctx)
    runner = CliRunner()
    refused = runner.invoke(main.app, ["sources", "purge", "Reports", "--yes"])
    assert refused.exit_code == 1 and "Disconnect" in refused.output
    ctx.source_manager.deactivate_source(src.id)
    cancelled = runner.invoke(main.app, ["sources", "purge", "Reports"], input="n\n")
    assert "Original user files are never touched" in cancelled.output
    assert ctx.source_manager.get_source(src.id).status == SourceStatus.REVOKED
    done = runner.invoke(main.app, ["sources", "purge", "Reports", "--yes"])
    assert done.exit_code == 0 and "Purged" in done.output
    preview = runner.invoke(main.app, ["library", "clean"])
    assert preview.exit_code == 0 and "Preview" in preview.output and "--apply" in preview.output
    assert (folder / "untouched.txt").exists()


def test_cli_cleanup_failure_is_plain_without_exception_payload(env, monkeypatch):
    from docket.interfaces.cli import main
    ctx, _, src, _ = env
    ctx.source_manager.deactivate_source(src.id)
    monkeypatch.setattr(main, "build_context", lambda: ctx)
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE internal failure details")
    monkeypatch.setattr(main, "_cleanup", lambda ctx: type("Broken", (), {"purge_disconnected": fail, "clean": fail})())
    for args in (["library", "clean", "--apply"], ["sources", "purge", "Reports", "--yes"]):
        result = CliRunner().invoke(main.app, args)
        assert result.exit_code == 1 and "could not finish" in result.output
        assert "PRIVATE" not in result.output and "Traceback" not in result.output
