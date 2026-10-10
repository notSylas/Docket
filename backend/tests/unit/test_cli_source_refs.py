"""CLI/classic-prompt source references (names, numbers, prefixes) and the
`sources list` table, on a scratch data dir."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from docket.core.db.models import (
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    VersionStatus,
)
from docket.interfaces.cli import main as cli_main
from docket.interfaces.cli.context import AppContext

runner = CliRunner()


@pytest.fixture
def context(tmp_path, monkeypatch):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data")
    monkeypatch.setattr(cli_main, "build_context", lambda: ctx)
    yield ctx
    ctx.engine.dispose()


def _add_ready_version(ctx, source, chunks=1, name="a.pdf"):
    with ctx.session_factory() as db:
        version = EvidenceVersion(
            source_id=source.id, file_path=name, content_hash=f"h-{source.id}-{name}",
            byte_size=1, parser_name="t", parser_version="1", status=VersionStatus.READY,
        )
        db.add(version)
        db.flush()
        unit = EvidenceUnit(evidence_version_id=version.id, unit_index=0, content_hash=f"u-{version.id}")
        recipe = db.get(ChunkRecipe, "r") or ChunkRecipe(
            id="r", chunk_size=10, overlap=0, splitter="t", parser_name="t", parser_version="1"
        )
        db.add_all([unit, recipe])
        db.flush()
        for n in range(chunks):
            db.add(Chunk(id=f"c-{version.id}-{n}", source_id=source.id, evidence_version_id=version.id,
                         evidence_unit_id=unit.id, chunk_recipe_id=recipe.id, ordinal=n,
                         text="Evidence", content_hash=f"{version.id}{n}"))
        db.commit()


def test_readiness_files_by_source(context, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    sa = context.source_manager.register_source(a)
    sb = context.source_manager.register_source(b)
    _add_ready_version(context, sa, chunks=3, name="1.pdf")
    _add_ready_version(context, sa, chunks=1, name="2.pdf")
    assert context.readiness.files_by_source() == {sa.id: 2}
    context.source_manager.deactivate_source(sa.id, reason="user_disconnected")
    assert context.readiness.files_by_source() == {}
    assert sb.id not in context.readiness.files_by_source()


def test_sources_list_shows_number_name_status_documents_path_and_id(context, tmp_path):
    a = tmp_path / "ForgeOPs" / "docs"
    b = tmp_path / "Finance"
    a.mkdir(parents=True)
    b.mkdir()
    sa = context.source_manager.register_source(a)
    sb = context.source_manager.register_source(b, display_name="Money")
    _add_ready_version(context, sa)

    result = runner.invoke(cli_main.app, ["sources", "list"])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0].split()[:5] == ["#", "NAME", "STATUS", "DOCUMENTS", "PATH"]
    first = next(ln for ln in lines if str(a) in ln)
    second = next(ln for ln in lines if str(b) in ln)
    assert first.split()[:4] == ["1", "docs", "active", "1"]
    assert second.split()[:4] == ["2", "Money", "active", "-"]
    assert sa.id in first and sb.id in second


def test_sources_prune_accepts_name_number_prefix_and_id(context, tmp_path):
    f = tmp_path / "Reports"
    f.mkdir()
    src = context.source_manager.register_source(f)
    for ref in ("reports", "1", src.id[:8], src.id):
        result = runner.invoke(cli_main.app, ["sources", "prune", ref])
        assert result.exit_code == 0, (ref, result.output)
        assert "Nothing to prune" in result.output


def test_unknown_and_ambiguous_refs_exit_1_and_list_candidates(context, tmp_path):
    for name in ("x", "y"):
        (tmp_path / name / "docs").mkdir(parents=True)
        context.source_manager.register_source(tmp_path / name / "docs")
    unknown = runner.invoke(cli_main.app, ["sources", "prune", "nope"])
    assert unknown.exit_code == 1
    assert "source not found: nope" in unknown.output
    ambiguous = runner.invoke(cli_main.app, ["sources", "prune", "docs"])
    assert ambiguous.exit_code == 1
    assert "matches 2 sources" in ambiguous.output
    assert "docs (x)" in ambiguous.output and "docs (y)" in ambiguous.output


def test_sources_add_name_and_covered_message(context, tmp_path):
    parent = tmp_path / "work"
    (parent / "inner").mkdir(parents=True)
    ok = runner.invoke(cli_main.app, ["sources", "add", str(parent), "--name", "Work"])
    assert ok.exit_code == 0, ok.output
    covered = runner.invoke(cli_main.app, ["sources", "add", str(parent / "inner")])
    assert covered.exit_code == 1
    assert "Already covered by Work" in covered.output
    assert [s.display_name for s in context.source_manager.list_sources()] == ["Work"]


def test_ingest_resolves_ref_before_running(context, tmp_path, monkeypatch):
    f = tmp_path / "Reports"
    f.mkdir()
    src = context.source_manager.register_source(f)
    seen = []

    class FakePipeline:
        def run_ingestion_for_source(self, source_id, *a, **k):
            seen.append(source_id)
            return SimpleNamespace(
                source_id=source_id, job_id="job", status="succeeded", files_processed=0,
                files_failed=0, file_results=[], files_skipped=0, invalid_skipped=0,
            )

        def find_stale_versions(self, source_id):
            return []

    monkeypatch.setattr(AppContext, "pipeline", FakePipeline(), raising=False)
    result = runner.invoke(cli_main.app, ["ingest", "reports"])
    assert result.exit_code == 0, result.output
    assert seen == [src.id]
