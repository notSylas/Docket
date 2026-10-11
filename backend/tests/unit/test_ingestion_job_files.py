from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest
from alembic import command
from sqlalchemy import select

from docket.core.db.models import IngestionFileResult, IngestionJob, IngestionJobStatus
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.interfaces.cli.context import AppContext
from docket.interfaces.cli.tui.real_backend import RealBackend
from docket.services.ingestion.job_results import JobFiles


@pytest.fixture
def env(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data", gateway=FakeInferenceGateway(),
                                 parser=SimpleNamespace(parser_name="unused", parser_version="1"))
    folder = tmp_path / "docs"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)
    yield ctx, src, folder
    ctx.engine.dispose()


def test_results_persist_before_completion_callback_and_survive_retry(env):
    ctx, src, folder = env
    (folder / "one.txt").write_text("The committee approved the budget on Monday. " * 20)
    missing = folder / "missing.txt"
    observed = []
    def progress(event):
        if event.kind == "done":
            with ctx.session_factory() as db:
                observed.append(db.execute(select(IngestionFileResult).where(
                    IngestionFileResult.path == str(event.path))).scalars().first().finished_at)
    first = ctx.pipeline.run_ingestion_for_paths(src.id, [folder / "one.txt", missing, missing], progress)
    assert len(observed) == 2 and all(observed)
    with ctx.session_factory() as db:
        rows = db.execute(select(IngestionFileResult).where(IngestionFileResult.job_id == first.job_id)).scalars().all()
        assert len(rows) == 2
        assert all(r.started_at and r.finished_at and r.finished_at >= r.started_at for r in rows)
        failed = next(r for r in rows if r.outcome == "failed")
        assert failed.failure_reason and "Traceback" not in failed.failure_reason
        assert next(r for r in rows if r.outcome == "ingested").chunks_written > 0
    be = RealBackend(ctx, health_check=lambda: None)
    assert "missing.txt" in " ".join(be.failed_file_lines()[src.id])
    assert "missing.txt" in " ".join(be.job_history(running=False)[0].details)
    missing.write_text("A repaired document contains an approved budget. " * 10)
    ctx.pipeline.run_ingestion_for_paths(src.id, [missing])
    assert src.id not in be.failed_file_lines()
    assert any("missing.txt: failed" in line for job in be.job_history(running=False) for line in job.details)


def test_interruption_records_attempted_and_unattempted_files(env):
    ctx, src, folder = env
    a, b = folder / "a.txt", folder / "b.txt"
    with ctx.session_factory() as db:
        job = IngestionJob(source_id=src.id, status=IngestionJobStatus.RUNNING)
        db.add(job); db.commit(); jid = job.id
    files = JobFiles(ctx.session_factory)
    files.prepare(jid, [a, b]); files.update(jid, a); files.finish(jid)
    with ctx.session_factory() as db:
        rows = db.execute(select(IngestionFileResult).order_by(IngestionFileResult.path)).scalars().all()
        assert [r.outcome for r in rows] == ["interrupted", "cancelled"]
        assert rows[0].started_at and rows[1].started_at is None


def test_additive_migration_rehearsed_only_on_copy_of_populated_temp_database(tmp_path):
    from alembic.config import Config
    backend = Path(__file__).resolve().parents[2]
    def config(path):
        c = Config(str(backend / "alembic.ini"))
        c.set_main_option("script_location", str(backend / "src/docket/core/db/migrations"))
        c.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        return c
    original, copied = tmp_path / "original.db", tmp_path / "rehearsal.db"
    command.upgrade(config(original), "b8e1f3a45c92")
    with sqlite3.connect(original) as db:
        db.execute("INSERT INTO workspaces (id,name,created_at) VALUES ('existing','kept','2026-01-01')")
        db.commit()
        with sqlite3.connect(copied) as target:
            db.backup(target)
    before = original.read_bytes()
    command.upgrade(config(copied), "head")
    with sqlite3.connect(copied) as db:
        assert db.execute("SELECT name FROM workspaces").fetchone() == ("kept",)
        assert db.execute("SELECT count(*) FROM ingestion_file_results").fetchone() == (0,)
    command.downgrade(config(copied), "b8e1f3a45c92")
    with sqlite3.connect(copied) as db:
        assert db.execute("SELECT name FROM workspaces").fetchone() == ("kept",)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ingestion_file_results'").fetchone() is None
    assert original.read_bytes() == before


def test_source_job_keeps_pre_version_failure_and_stop_outcomes(env, monkeypatch):
    ctx, src, folder = env
    for name in ("a.txt", "b.txt"):
        (folder / name).write_text("Some plain document text.")
    def fail(*args):
        raise ValueError("Password protected document\nTraceback should not be stored")
    monkeypatch.setattr(ctx.pipeline, "_ingest_one_file", fail)
    class Stop(BaseException):
        pass
    def stop(event):
        if event.kind == "done":
            raise Stop()
    with pytest.raises(Stop):
        ctx.pipeline.run_ingestion_for_source(src.id, stop)
    with ctx.session_factory() as db:
        rows = db.execute(select(IngestionFileResult)).scalars().all()
        assert {r.outcome for r in rows} == {"failed", "cancelled"}
        assert all(r.finished_at for r in rows)
        assert next(r.failure_reason for r in rows if r.outcome == "failed").startswith("Password-protected")
    be = RealBackend(ctx, health_check=lambda: None)
    assert len([f for f in be.load_world().find(src.id).files if f.status == "failed"]) == 1


def test_ignoring_failure_hides_it_without_rewriting_job_history(env):
    ctx, src, folder = env
    missing = folder / "missing.txt"
    ctx.pipeline.run_ingestion_for_paths(src.id, [missing])
    be = RealBackend(ctx, health_check=lambda: None)
    assert src.id in be.failed_file_lines()
    ctx.source_manager.add_ignored_file(src.id, "missing.txt")
    assert src.id not in be.failed_file_lines()
    assert "missing.txt: failed" in " ".join(be.job_history(running=False)[0].details)


def test_failure_view_respects_explicit_ignore_escape_hatch(env):
    ctx, src, folder = env
    ctx.source_manager.add_ignored_file(src.id, "missing.txt")
    ctx.settings.ingest_ignore_enabled = False
    ctx.pipeline.run_ingestion_for_paths(src.id, [folder / "missing.txt"])
    assert src.id in RealBackend(ctx, health_check=lambda: None).failed_file_lines()
