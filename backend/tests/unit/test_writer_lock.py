"""Real subprocess coordination over temporary directories, with no models."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from docket.core.writer_lock import LOCK_FILENAME, WriterLock, WriterLockBusyError

HOLDER = """
import sys
from docket.core.writer_lock import WriterLock
with WriterLock(sys.argv[1]).hold('indexing fixture documents'):
    print('locked', flush=True)
    sys.stdin.readline()
print('released', flush=True)
"""

CONTENDER = """
import json, sys, time
from docket.core.writer_lock import WriterLock, WriterLockBusyError
start = time.monotonic()
try:
    with WriterLock(sys.argv[1], timeout=float(sys.argv[2])).hold('pruning fixture documents'):
        print(json.dumps({'acquired': True, 'elapsed': time.monotonic() - start}))
except WriterLockBusyError as exc:
    print(json.dumps({'owner': exc.owner, 'message': str(exc), 'elapsed': time.monotonic() - start}))
"""


@contextmanager
def holder(data_dir):
    env = {**os.environ, "DOCKET_DATA_DIR": str(data_dir)}
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(data_dir)], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "lock holder did not start"
        assert process.stdout.readline().strip() == "locked"
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def contend(data_dir, timeout=0.15):
    result = subprocess.run(
        [sys.executable, "-c", CONTENDER, str(data_dir), str(timeout)],
        env={**os.environ, "DOCKET_DATA_DIR": str(data_dir)},
        text=True, capture_output=True, timeout=5, check=True,
    )
    return json.loads(result.stdout)


def test_two_processes_wait_bounded_and_name_owner(tmp_path):
    with holder(tmp_path) as process:
        result = contend(tmp_path)
        assert result["owner"]["pid"] == process.pid
        assert result["owner"]["started_at"]
        assert result["owner"]["operation"] == "indexing fixture documents"
        assert str(process.pid) in result["message"]
        assert "started" in result["message"] and "indexing fixture documents" in result["message"]
        assert 0.12 <= result["elapsed"] < 1.5
        # The contender must never overwrite the holder's diagnostic record.
        assert json.loads((tmp_path / LOCK_FILENAME).read_text())["pid"] == process.pid


def test_waiter_acquires_after_normal_release(tmp_path):
    with holder(tmp_path) as process:
        waiter = subprocess.Popen(
            [sys.executable, "-c", CONTENDER, str(tmp_path), "2"],
            env={**os.environ, "DOCKET_DATA_DIR": str(tmp_path)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            process.stdin.write("release\n")
            process.stdin.flush()
            out, err = waiter.communicate(timeout=5)
            assert waiter.returncode == 0, err
            assert json.loads(out)["acquired"]
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.communicate(timeout=5)


def test_killed_holder_releases_os_lock_and_stale_record_is_replaced(tmp_path):
    with holder(tmp_path) as process:
        process.kill()
        process.wait(timeout=5)
        assert json.loads((tmp_path / LOCK_FILENAME).read_text())["pid"] == process.pid
        assert contend(tmp_path)["acquired"]


@pytest.mark.parametrize("metadata", ["not json", "", '{"pid": 1}', '{"pid": 99999999}'])
def test_stale_metadata_never_blocks_acquisition(tmp_path, metadata):
    (tmp_path / LOCK_FILENAME).write_text(metadata)
    assert contend(tmp_path)["acquired"]


def test_reentrant_across_instances_and_canonical_paths(tmp_path):
    with WriterLock(tmp_path).hold("outer update"):
        original = (tmp_path / LOCK_FILENAME).read_text()
        with WriterLock(tmp_path / ".", timeout=0).hold("nested prune"):
            assert (tmp_path / LOCK_FILENAME).read_text() == original
        assert "owner" in contend(tmp_path, timeout=0)
    assert contend(tmp_path)["acquired"]


def test_other_threads_do_not_share_reentrancy(tmp_path):
    results = []
    def attempt():
        try:
            with WriterLock(tmp_path, timeout=0.05).hold("other thread"):
                results.append("acquired")
        except WriterLockBusyError:
            results.append("busy")
    with WriterLock(tmp_path).hold("main thread"):
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert results == ["busy"]


def test_different_data_directories_are_independent(tmp_path):
    with holder(tmp_path / "one"):
        assert contend(tmp_path / "two")["acquired"]


def test_exception_releases_lock(tmp_path):
    with pytest.raises(ValueError):
        with WriterLock(tmp_path).hold("failing update"):
            raise ValueError("fixture failure")
    assert contend(tmp_path)["acquired"]


@pytest.fixture
def context(tmp_path):
    from docket.interfaces.cli.context import AppContext
    from docket.infra.inference.gateway import FakeInferenceGateway
    ctx = AppContext.for_testing(
        data_dir=tmp_path, gateway=FakeInferenceGateway(),
        parser=SimpleNamespace(parser_name="fake", parser_version="1"),
    )
    yield ctx
    ctx.engine.dispose()


@pytest.mark.parametrize("operation", [
    "ingest", "refresh", "rechunk", "rechunk_version", "prune", "prune_candidates", "purge", "maintenance", "reindex",
])
def test_writer_entrypoints_lock_before_mutation(context, operation):
    from docket.infra.index.reindex import reindex
    from docket.services.sources.maintenance import MaintenancePlan
    ctx = context
    for service in (ctx.pipeline, ctx.prune_service, ctx.purge_service, ctx.maintenance):
        service._writer_lock = WriterLock(ctx.settings.data_dir, timeout=0)
    calls = {
        "ingest": lambda: ctx.pipeline.run_ingestion_for_source("missing"),
        "refresh": lambda: ctx.pipeline.run_ingestion_for_paths("missing", []),
        "rechunk": lambda: ctx.pipeline.rechunk_source("missing"),
        "rechunk_version": lambda: ctx.pipeline.rechunk_version("missing", None),
        "prune": lambda: ctx.prune_service.apply("missing"),
        "prune_candidates": lambda: ctx.prune_service.apply_candidates([]),
        "purge": lambda: ctx.purge_service.purge_source("missing"),
        "maintenance": lambda: ctx.maintenance.run(MaintenancePlan()),
        "reindex": lambda: reindex(
            engine=ctx.engine, db_path=ctx.settings.lancedb_path, gateway=ctx.gateway,
            manifest_path=ctx.settings.index_manifest_path, embed_model="fake",
            writer_lock=WriterLock(ctx.settings.data_dir, timeout=0),
        ),
    }
    with holder(ctx.settings.data_dir):
        with pytest.raises(WriterLockBusyError):
            calls[operation]()


def test_readers_and_dry_run_do_not_acquire_writer_lock(context, monkeypatch):
    from docket.interfaces.cli import main
    from docket.interfaces.cli.tui.real_backend import RealBackend
    from docket.services.query.service import QueryService
    from docket.services.query.classifier import QueryMode
    from docket.services.query import service as query_module
    ctx = context
    source_dir = ctx.settings.data_dir / "documents"
    source_dir.mkdir()
    source = ctx.source_manager.register_source(source_dir)
    monkeypatch.setattr(main, "build_context", lambda: ctx)
    monkeypatch.setattr(query_module, "hybrid_search", lambda **kwargs: [])
    query = QueryService(engine=ctx.engine, table=object(), gateway=ctx.gateway, resolver=ctx.resolver)
    ui = RealBackend(ctx, health_check=lambda: None)
    with holder(ctx.settings.data_dir):
        result = CliRunner().invoke(main.app, ["sources", "list"])
        assert result.exit_code == 0, result.output
        assert source.id in result.output
        assert ui.load_world().sources
        assert query.ask("a fixture question", mode=QueryMode.FAST).abstained
        assert ctx.prune_service.find(source.id) == []
        assert ctx.pipeline.rechunk_source(source.id, dry_run=True).results == []


def test_cli_reports_owner_without_traceback(context, monkeypatch):
    from docket.interfaces.cli import main
    ctx = context
    folder = ctx.settings.data_dir / "documents"
    folder.mkdir()
    source = ctx.source_manager.register_source(folder)
    ctx.pipeline._writer_lock = WriterLock(ctx.settings.data_dir, timeout=0)
    monkeypatch.setattr(main, "build_context", lambda: ctx)
    with holder(ctx.settings.data_dir) as process:
        result = CliRunner().invoke(main.app, ["ingest", source.id])
        assert result.exit_code == 1
        assert str(process.pid) in result.output
        assert "indexing fixture documents" in result.output
        assert "Traceback" not in result.output
