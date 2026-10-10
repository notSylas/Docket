"""`docket sources prune`: supersede + de-index versions whose path is now
ignored, on a scratch DB with generated files (never real data)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from conftest import _write_xlsx
from docket.core.db.models import EvidenceVersion, VersionStatus
from docket.interfaces.cli import main as cli_main
from docket.interfaces.cli.quiet import ignore_notices
from docket.services.sources.manager import SourceNotFoundError
from docket.services.sources.prune import SourcePruneService


def _statuses(env) -> dict[str, VersionStatus]:
    with env.session_factory() as session:
        rows = session.execute(select(EvidenceVersion)).scalars().all()
        return {Path(v.file_path).name: v.status for v in rows}


@pytest.fixture()
def indexed(env, monkeypatch):
    """Two xlsx files indexed with ignoring OFF (as before the rules existed):
    one normal, one under .venv/lib/site-packages."""
    _write_xlsx(env.folder / "keep.xlsx", "S", ["n", "q"], [["apples", 1]])
    junk = env.folder / ".venv" / "lib" / "site-packages" / "junk.xlsx"
    junk.parent.mkdir(parents=True)
    _write_xlsx(junk, "S", ["n", "q"], [["pears", 2]])
    monkeypatch.setattr(env.pipeline._settings, "ingest_ignore_enabled", False)
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.files_processed == 2
    monkeypatch.setattr(env.pipeline._settings, "ingest_ignore_enabled", True)
    env.prune = SourcePruneService(
        env.session_factory, env.pipeline._evidence_manager, env.pipeline._index_manager
    )
    env.junk = junk
    return env


def test_find_is_a_dry_run(indexed):
    before = indexed.fts.chunk_ids_for_source(indexed.source.id)
    found = indexed.prune.find(indexed.source.id)
    assert [c.relative_path.as_posix() for c in found] == [".venv/lib/site-packages/junk.xlsx"]
    assert found[0].status == "ready"
    assert indexed.fts.chunk_ids_for_source(indexed.source.id) == before
    assert _statuses(indexed) == {"keep.xlsx": VersionStatus.READY, "junk.xlsx": VersionStatus.READY}


def test_apply_supersedes_and_removes_from_indexes_but_not_files(indexed):
    sid = indexed.source.id
    pruned = indexed.prune.apply(sid)
    assert len(pruned) == 1
    assert _statuses(indexed) == {
        "keep.xlsx": VersionStatus.READY,
        "junk.xlsx": VersionStatus.SUPERSEDED,
    }
    fts_ids = indexed.fts.chunk_ids_for_source(sid)
    assert fts_ids and indexed.vector.chunk_ids_for_source(sid) == fts_ids
    assert indexed.junk.exists()  # user file untouched
    assert indexed.prune.find(sid) == []  # idempotent


def test_ingest_after_prune_does_not_resurrect_ignored_files(indexed):
    indexed.prune.apply(indexed.source.id)
    result = indexed.pipeline.run_ingestion_for_source(indexed.source.id)
    assert [r.path.name for r in result.file_results] == ["keep.xlsx"]
    assert result.files_skipped == 1
    assert _statuses(indexed)["junk.xlsx"] == VersionStatus.SUPERSEDED


def test_disabled_rules_find_nothing(indexed):
    off = SourcePruneService(
        indexed.session_factory,
        indexed.pipeline._evidence_manager,
        indexed.pipeline._index_manager,
        enabled=False,
    )
    assert off.find(indexed.source.id) == []


def test_unknown_source(indexed):
    with pytest.raises(SourceNotFoundError):
        indexed.prune.find("src_nope")


def test_root_inside_hidden_path_does_not_prune_everything(env, tmp_path):
    hidden_root = tmp_path / ".hidden" / "proj"
    hidden_root.mkdir(parents=True)
    source = env.source_manager.register_source(hidden_root)
    _write_xlsx(hidden_root / "ok.xlsx", "S", ["n", "q"], [["a", 1]])
    env.pipeline.run_ingestion_for_source(source.id)
    prune = SourcePruneService(
        env.session_factory, env.pipeline._evidence_manager, env.pipeline._index_manager
    )
    assert prune.find(source.id) == []


def test_hint_when_nothing_skipped_but_index_has_ignored_files(indexed):
    result = SimpleNamespace(files_skipped=0)
    notes = ignore_notices(result, indexed.prune, indexed.source.id)
    assert notes == [
        "1 indexed file is now in ignored folders; "
        f"run `docket sources prune {indexed.source.id}` to remove them."
    ]
    # Nothing to hint about when discovery already skipped something.
    assert ignore_notices(SimpleNamespace(files_skipped=3), indexed.prune, "x") == [
        "Skipped 3 files in ignored folders (virtualenvs, site-packages, hidden)"
    ]
    # And after pruning there is no hint.
    indexed.prune.apply(indexed.source.id)
    assert ignore_notices(result, indexed.prune, indexed.source.id) == []


def test_cli_prune_dry_run_and_apply(monkeypatch, tmp_path):
    from docket.services.sources.prune import PruneCandidate

    calls = []
    cand = PruneCandidate("ev_1", tmp_path / ".venv" / "a.pdf", Path(".venv/a.pdf"), "ready")

    class FakeService:
        def find(self, sid):
            calls.append(("find", sid))
            return [cand]

        def apply(self, sid):
            calls.append(("apply", sid))
            return [cand]

    monkeypatch.setattr(
        cli_main, "build_context", lambda: SimpleNamespace(prune_service=FakeService())
    )
    runner = CliRunner()
    dry = runner.invoke(cli_main.app, ["sources", "prune", "src_1"])
    assert dry.exit_code == 0
    assert "Would prune 1 file(s)" in dry.output and ".venv/a.pdf (ready)" in dry.output
    assert "Dry run" in dry.output
    assert calls == [("find", "src_1")]

    applied = runner.invoke(cli_main.app, ["sources", "prune", "src_1", "--apply"])
    assert applied.exit_code == 0
    assert "Pruned 1 file(s)" in applied.output
    assert calls[-1] == ("apply", "src_1")
