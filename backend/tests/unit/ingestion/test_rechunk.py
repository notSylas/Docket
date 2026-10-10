"""Per-parser recipes, recipe-drift detection and `docket ingest --rechunk`
(Upgrade doc 04 section 5). Real Docling/openpyxl parsing, fake gateway."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from conftest import _write_docx, _write_xlsx
from docket.core.config import Settings, settings as singleton_settings
from docket.core.db.models import (
    Chunk,
    EvidenceUnit,
    EvidenceVersion,
    SourceStatus,
    VersionStatus,
)
from docket.infra.parsing import xlsx_context
from docket.infra.parsing.recipes import (
    DEFAULT_SPLITTER,
    ChunkRecipe,
    docling_recipe,
    pptx_recipe,
    recipe_family,
    xlsx_recipe,
)
from docket.interfaces.cli.main import app
from docket.services.ingestion.pipeline import IngestionPipeline


@pytest.fixture(autouse=True)
def _never_touch_real_data(tmp_path, monkeypatch):
    """Any accidental `build_context()` must land in a scratch dir."""
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "scratch-data"))


# -- recipes -----------------------------------------------------------------


def _ids(s: Settings) -> tuple[str, str, str]:
    return (
        docling_recipe(s, "docling", "1").id,
        xlsx_recipe(s, "openpyxl", "3").id,
        pptx_recipe(s, "python-pptx", "1").id,
    )


def test_recipes_distinct_per_family_and_depend_only_on_own_inputs(monkeypatch) -> None:
    base = Settings()
    d, x, p = _ids(base)
    assert len({d, x, p}) == 3

    # xlsx-only inputs
    assert _ids(base.model_copy(update={"xlsx_period_context_enabled": False})) == (
        d,
        _ids(base.model_copy(update={"xlsx_period_context_enabled": False}))[1],
        p,
    )
    assert _ids(base.model_copy(update={"xlsx_period_context_enabled": False}))[1] != x
    with monkeypatch.context() as m:
        m.setattr(xlsx_context, "XLSX_CONTEXT_VERSION", xlsx_context.XLSX_CONTEXT_VERSION + 1)
        bumped = _ids(base)
    assert bumped[0] == d and bumped[2] == p and bumped[1] != x

    # docling-only inputs
    d2, x2, p2 = _ids(base.model_copy(update={"chunk_size_words": base.chunk_size_words + 1}))
    assert (d2 != d, x2, p2) == (True, x, p)
    d3, x3, p3 = _ids(base.model_copy(update={"chunk_overlap_words": base.chunk_overlap_words + 1}))
    assert (d3 != d, x3, p3) == (True, x, p)

    # shared token cap moves all three
    d4, x4, p4 = _ids(base.model_copy(update={"chunk_max_tokens": base.chunk_max_tokens + 1}))
    assert d4 != d and x4 != x and p4 != p

    # parser versions are per family
    assert docling_recipe(base, "docling", "2").id != d
    assert xlsx_recipe(base, "openpyxl", "4").id != x


def test_recipe_family_by_extension() -> None:
    assert recipe_family("/a/b/Q.XLSX") == "xlsx"
    assert recipe_family("/a/b/x.pptx") == "pptx"
    assert recipe_family("/a/b/x.docx") == "docling"
    assert recipe_family(None) == "docling"


# -- helpers -------------------------------------------------------------------


def _pipeline(env, *, chunk_size: int, xlsx_context: bool) -> IngestionPipeline:
    """A pipeline sharing `env`'s stores but with a chosen recipe: stands in for
    'the code/config as it was when the files were first ingested'."""
    old = env.pipeline
    settings = singleton_settings.model_copy(update={"xlsx_period_context_enabled": xlsx_context})
    return IngestionPipeline(
        session_factory=env.session_factory,
        evidence_manager=old._evidence_manager,
        parser=old._parser,
        index_manager=old._index_manager,
        chunk_recipe=ChunkRecipe(
            chunk_size=chunk_size,
            overlap=40,
            splitter=DEFAULT_SPLITTER,
            parser_name=old._parser.parser_name,
            parser_version=old._parser.parser_version,
        ),
        settings=settings,
    )


def _legacy_ingest(env, monkeypatch, *, chunk_size: int = 100) -> None:
    """Ingest the folder as an older build would have: other docling recipe, no xlsx context."""
    with monkeypatch.context() as m:
        m.setattr(singleton_settings, "xlsx_period_context_enabled", False)
        legacy = _pipeline(env, chunk_size=chunk_size, xlsx_context=False)
        result = legacy.run_ingestion_for_source(env.source.id)
    assert result.files_failed == 0, result.file_results


def _versions(env) -> dict[str, EvidenceVersion]:
    with env.session_factory() as session:
        rows = session.execute(select(EvidenceVersion)).scalars().all()
        return {Path(v.file_path).name + ":" + v.status.name: v for v in rows}


def _chunks(env, version_id: str) -> list[Chunk]:
    with env.session_factory() as session:
        return list(
            session.execute(select(Chunk).where(Chunk.evidence_version_id == version_id)).scalars()
        )


def _ready(env, name: str) -> EvidenceVersion:
    return _versions(env)[f"{name}:READY"]


def _populate(env) -> None:
    _write_docx(env.folder / "memo.docx", "Memo", "Quarterly memo body text here.")
    _write_xlsx(
        env.folder / "Budget_FY2025-26.xlsx",
        "Monthly",
        ["Month", "Amount"],
        [["Jul", 10], ["Aug", 20]],
    )


def _index_ids(env, ids: set[str]) -> tuple[set[str], set[str]]:
    return env.fts.existing_chunk_ids(ids), env.vector.existing_chunk_ids(ids)


# -- drift detection -----------------------------------------------------------


def test_current_versions_are_not_stale(env) -> None:
    _populate(env)
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert env.pipeline.find_stale_versions(env.source.id) == []
    candidates, up_to_date = env.pipeline.find_rechunk_candidates(env.source.id)
    assert candidates == [] and up_to_date == 2


def test_stale_detection_and_exclusions(env, monkeypatch) -> None:
    _populate(env)
    _legacy_ingest(env, monkeypatch)
    stale = env.pipeline.find_stale_versions(env.source.id)
    assert sorted(c.path.name for c in stale) == ["Budget_FY2025-26.xlsx", "memo.docx"]
    assert {c.reason for c in stale} == {"stale"}

    # zero chunks -> never stale
    memo = _ready(env, "memo.docx")
    env.pipeline._chunk_writer.delete_units_and_chunks(memo.id)
    assert [c.path.name for c in env.pipeline.find_stale_versions(env.source.id)] == [
        "Budget_FY2025-26.xlsx"
    ]

    # chunks that disagree among themselves -> stale
    xlsx = _ready(env, "Budget_FY2025-26.xlsx")
    current = env.pipeline._recipes["xlsx"]
    env.pipeline._chunk_writer.ensure_recipe_row(current)
    with env.session_factory() as session:
        chunk = session.execute(
            select(Chunk).where(Chunk.evidence_version_id == xlsx.id)
        ).scalars().first()
        chunk.chunk_recipe_id = current.id
        session.commit()
    assert [c.path.name for c in env.pipeline.find_stale_versions(env.source.id)] == [
        "Budget_FY2025-26.xlsx"
    ]

    # SUPERSEDED versions are excluded: change the xlsx file -> new version, old superseded
    _write_xlsx(env.folder / "Budget_FY2025-26.xlsx", "Monthly", ["Month", "Amount"], [["Jul", 99]])
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert env.pipeline.find_stale_versions(env.source.id) == []

    # revoked source -> refused
    env.source_manager.deactivate_source(env.source.id)
    from docket.services.ingestion.pipeline import SourceNotActiveError

    with pytest.raises(SourceNotActiveError):
        env.pipeline.find_stale_versions(env.source.id)


# -- re-chunk ------------------------------------------------------------------


def test_rechunk_docx_and_xlsx_replaces_chunks_everywhere(env, monkeypatch) -> None:
    _populate(env)
    _legacy_ingest(env, monkeypatch)
    memo_v, xlsx_v = _ready(env, "memo.docx"), _ready(env, "Budget_FY2025-26.xlsx")
    old_ids = {c.id for v in (memo_v, xlsx_v) for c in _chunks(env, v.id)}
    old_recipe_ids = {c.chunk_recipe_id for v in (memo_v, xlsx_v) for c in _chunks(env, v.id)}
    assert old_ids and _index_ids(env, old_ids) == (old_ids, old_ids)
    assert all("context" not in json.loads(u.locator_json) for u in _units(env, xlsx_v.id))

    # Live file changes must not matter: rechunk uses the stored bytes.
    (env.folder / "Budget_FY2025-26.xlsx").unlink()
    report = env.pipeline.rechunk_source(env.source.id, dry_run=True)
    assert len(report.candidates) == 2 and report.results == []
    assert {c.id for v in (memo_v, xlsx_v) for c in _chunks(env, v.id)} == old_ids  # dry run: unchanged

    report = env.pipeline.rechunk_source(env.source.id)
    assert [r.status for r in report.results] == ["ingested", "ingested"]

    recipes = env.pipeline._recipes
    for v, fam in ((memo_v, "docling"), (xlsx_v, "xlsx")):
        chunks = _chunks(env, v.id)
        assert chunks and {c.chunk_recipe_id for c in chunks} == {recipes[fam].id}
        assert recipes[fam].id not in old_recipe_ids
        units = _units(env, v.id)
        assert len(units) == len(chunks)  # exactly one unit/chunk set
        with env.session_factory() as session:
            assert session.get(EvidenceVersion, v.id).status == VersionStatus.READY
    new_ids = {c.id for v in (memo_v, xlsx_v) for c in _chunks(env, v.id)}
    assert not (new_ids & old_ids)
    assert _index_ids(env, old_ids) == (set(), set())
    assert _index_ids(env, new_ids) == (new_ids, new_ids)

    # xlsx context present, FY label from the ORIGINAL name, not the temp dir
    contexts = [json.loads(u.locator_json).get("context") for u in _units(env, xlsx_v.id)]
    assert all(contexts)
    joined = " ".join(" ".join(c) for c in contexts)
    assert "FY2025-26" in joined and "docket-rechunk" not in joined
    assert any("July" in c for c in contexts[0])

    # idempotent: nothing stale now, a second run touches nothing
    assert env.pipeline.find_stale_versions(env.source.id) == []
    again = env.pipeline.rechunk_source(env.source.id)
    assert again.candidates == [] and again.up_to_date == 2
    assert {c.id for v in (memo_v, xlsx_v) for c in _chunks(env, v.id)} == new_ids


def _units(env, version_id: str) -> list[EvidenceUnit]:
    with env.session_factory() as session:
        return list(
            session.execute(
                select(EvidenceUnit)
                .where(EvidenceUnit.evidence_version_id == version_id)
                .order_by(EvidenceUnit.unit_index)
            ).scalars()
        )


def test_current_recipe_version_is_untouched(env, monkeypatch) -> None:
    _populate(env)
    _legacy_ingest(env, monkeypatch)
    # docx is current (recipe built the same way as env's), xlsx stale
    env.pipeline.run_ingestion_for_source(env.source.id)  # plain ingest changes nothing
    memo = _ready(env, "memo.docx")
    # re-ingest docx under env's own recipe so it is current
    env.pipeline.rechunk_source(env.source.id)
    before = {c.id for c in _chunks(env, memo.id)}
    report = env.pipeline.rechunk_source(env.source.id)
    assert report.candidates == []
    assert {c.id for c in _chunks(env, memo.id)} == before


def test_failure_leaves_failed_and_next_run_fixes_it(env, monkeypatch) -> None:
    _populate(env)
    _legacy_ingest(env, monkeypatch)
    pipeline = env.pipeline
    real = pipeline._index_manager.upsert_chunks
    calls = {"n": 0}

    def flaky(records):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("embed down")
        return real(records)

    monkeypatch.setattr(pipeline._index_manager, "upsert_chunks", flaky)
    report = pipeline.rechunk_source(env.source.id)
    statuses = sorted(r.status for r in report.results)
    assert statuses == ["failed", "ingested"]
    failed = next(r for r in report.results if r.status == "failed")
    assert "embed down" in failed.error
    failed_versions = [v for k, v in _versions(env).items() if k.endswith(":FAILED")]
    assert len(failed_versions) == 1
    # failed version's index entries are not left servable
    fv_ids = {c.id for c in _chunks(env, failed_versions[0].id)}
    assert _index_ids(env, fv_ids) == (set(), set())

    monkeypatch.setattr(pipeline._index_manager, "upsert_chunks", real)
    again = pipeline.rechunk_source(env.source.id)
    assert [c.reason for c in again.candidates] == ["failed"]
    assert [r.status for r in again.results] == ["ingested"]
    assert not any(k.endswith(":FAILED") for k in _versions(env))
    assert pipeline.find_stale_versions(env.source.id) == []
    ids = {c.id for c in _chunks(env, failed_versions[0].id)}
    assert ids and _index_ids(env, ids) == (ids, ids)


def test_missing_blob_fails_clearly_and_others_continue(env, monkeypatch) -> None:
    _populate(env)
    _legacy_ingest(env, monkeypatch)
    xlsx = _ready(env, "Budget_FY2025-26.xlsx")
    env.pipeline._evidence_manager.store._object_path(xlsx.content_hash).unlink()
    report = env.pipeline.rechunk_source(env.source.id)
    by_name = {r.path.name: r for r in report.results}
    assert by_name["Budget_FY2025-26.xlsx"].status == "failed"
    assert "missing" in by_name["Budget_FY2025-26.xlsx"].error
    assert by_name["memo.docx"].status == "ingested"
    # the untouched version keeps serving its old chunks
    assert _ready(env, "Budget_FY2025-26.xlsx")


# -- CLI -------------------------------------------------------------------------


@pytest.fixture()
def cli(env, monkeypatch):
    ctx = SimpleNamespace(
        pipeline=env.pipeline,
        source_manager=env.source_manager,
        settings=SimpleNamespace(index_manifest_path=env.folder / "no-manifest.json"),
    )
    monkeypatch.setattr("docket.interfaces.cli.main.build_context", lambda: ctx)
    return CliRunner()


def test_cli_plain_ingest_reports_stale_and_changes_nothing(env, cli, monkeypatch) -> None:
    _populate(env)
    _legacy_ingest(env, monkeypatch)
    before = {k: [c.id for c in _chunks(env, v.id)] for k, v in _versions(env).items()}
    result = cli.invoke(app, ["ingest", env.source.id])
    assert result.exit_code == 0, result.output
    assert "2 already-ingested files have chunks from an older chunking recipe" in result.output
    assert "docket ingest --all --rechunk" in result.output
    after = {k: [c.id for c in _chunks(env, v.id)] for k, v in _versions(env).items()}
    assert after == before


def test_cli_plain_ingest_silent_when_nothing_stale(env, cli) -> None:
    _populate(env)
    result = cli.invoke(app, ["ingest", env.source.id])
    assert result.exit_code == 0
    assert "--rechunk" not in result.output


def test_cli_all_recovers_missing_source(env, cli) -> None:
    from docket.core.db.models import SourceStatus
    _populate(env)
    env.source_manager.mark_unreachable(env.source.id)
    result = cli.invoke(app, ["ingest", "--all"])
    assert result.exit_code == 0, result.output
    assert "status=succeeded" in result.output
    assert env.source_manager.get_source(env.source.id).status == SourceStatus.ACTIVE


def test_cli_dry_run_then_rechunk(env, cli, monkeypatch) -> None:
    _populate(env)
    _legacy_ingest(env, monkeypatch)
    before = {k: [c.id for c in _chunks(env, v.id)] for k, v in _versions(env).items()}
    dry = cli.invoke(app, ["ingest", "--all", "--rechunk", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Budget_FY2025-26.xlsx (stale)" in dry.output and "memo.docx (stale)" in dry.output
    assert {k: [c.id for c in _chunks(env, v.id)] for k, v in _versions(env).items()} == before

    real = cli.invoke(app, ["ingest", env.source.id, "--rechunk"])
    assert real.exit_code == 0, real.output
    assert "Re-chunked 2, failed 0, skipped 0 up to date." in real.output
    assert cli.invoke(app, ["ingest", env.source.id]).output.count("--rechunk") == 0

    bad = cli.invoke(app, ["ingest", env.source.id, "--dry-run"])
    assert bad.exit_code == 1
