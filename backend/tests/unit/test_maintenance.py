"""CP5: library self-maintenance (`services/sources/maintenance.py`), the
`docket sync` command and plain-language notices.

Fake pipeline / prune / reindex over a real scratch SQLite and temp folders.
No parser, no Ollama, no user data.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from docket.core.db.identity import compute_chunk_id, compute_recipe_id
from docket.core.db.models import (
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    IngestionJob,
    IngestionJobStatus,
    VersionStatus,
)
from docket.infra.index.context import INDEX_TEXT_VERSION
from docket.infra.index.manifest import new_manifest, read_manifest, write_manifest
from docket.interfaces.cli import main as cli_main
from docket.interfaces.cli.context import AppContext
from docket.interfaces.cli.quiet import ignore_notices, index_format_notice, stale_recipe_notice
from docket.services.ingestion.failure import plain_failure_reason
from docket.services.ingestion.pipeline import (
    FileIngestResult,
    IngestionJobResult,
    ProgressEvent,
    RechunkCandidate,
    RechunkReport,
)
from docket.services.sources.maintenance import (
    LibraryMaintenance,
    MaintenancePlan,
    MaintenanceProgress,
    MaintenanceStep,
)

runner = CliRunner()


# -- fakes ---------------------------------------------------------------------


class FakePrune:
    def __init__(self) -> None:
        self.pending: dict[str, list[str]] = {}
        self.applied: list[str] = []

    def find(self, source_id: str):
        return [
            SimpleNamespace(relative_path=Path(rel), status="ready", version_id=f"v-{rel}")
            for rel in self.pending.get(source_id, [])
        ]

    def count(self, source_id: str) -> int:
        return len(self.pending.get(source_id, []))

    def apply_candidates(self, candidates):
        for c in candidates:
            self.applied.append(c.relative_path.as_posix())
            for rels in self.pending.values():
                if c.relative_path.as_posix() in rels:
                    rels.remove(c.relative_path.as_posix())
        return list(candidates)

    def apply(self, source_id: str):
        return self.apply_candidates(self.find(source_id))


class FakePipeline:
    def __init__(self, session_factory, root: Path) -> None:
        self._sf = session_factory
        self.root = root
        self.stale: dict[str, list[Path]] = {}
        self.calls: list[str] = []
        self.ingest_files = ["a.pdf", "b.pdf"]
        self.paths_calls: list[list[Path]] = []

    def find_stale_versions(self, source_id: str):
        return [RechunkCandidate(f"v{i}", p, "stale") for i, p in enumerate(self.stale.get(source_id, []))]

    def run_ingestion_for_source(self, source_id: str, progress=None):
        self.calls.append(f"ingest:{source_id}")
        results = []
        total = len(self.ingest_files)
        for i, name in enumerate(self.ingest_files, start=1):
            path = self.root / name
            if progress:
                progress(ProgressEvent("start", i, total, path))
            r = FileIngestResult(path=path, status="ingested", chunks_written=1)
            results.append(r)
            if progress:
                progress(ProgressEvent("done", i, total, path, r))
        with self._sf() as session:
            session.add(
                IngestionJob(
                    source_id=source_id,
                    status=IngestionJobStatus.SUCCEEDED,
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
        return IngestionJobResult(source_id, "job", "succeeded", total, 0, results)

    def rechunk_source(self, source_id, *, dry_run=False, progress=None, only_stale=False, should_stop=None):
        self.calls.append(f"rechunk:{source_id}")
        cands = self.find_stale_versions(source_id)
        report = RechunkReport(source_id, candidates=cands)
        for i, c in enumerate(cands, start=1):
            if should_stop is not None and should_stop():
                break
            if progress:
                progress(i, len(cands), c)
            report.results.append(FileIngestResult(c.path, "ingested", chunks_written=1))
        if len(report.results) == len(cands):
            self.stale[source_id] = []
        return report

    def run_ingestion_for_paths(self, source_id, paths, progress=None):
        self.paths_calls.append(list(paths))
        r = FileIngestResult(path=paths[0], status="ingested", chunks_written=2)
        return IngestionJobResult(source_id, "job", "succeeded", 1, 0, [r])


def _doc(path: Path, name: str = "a.pdf") -> Path:
    p = path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n")
    return p


def _add_ready_version(ctx, source, rel: str, *, with_chunk: bool = True) -> None:
    with ctx.session_factory() as session:
        v = EvidenceVersion(
            source_id=source.id,
            file_path=str(Path(source.path) / rel),
            content_hash=(rel.encode().hex() * 8)[:64].ljust(64, "a"),
            byte_size=10,
            observed_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
            parser_name="docling",
            parser_version="1",
            status=VersionStatus.READY,
        )
        session.add(v)
        session.flush()
        if with_chunk:
            unit = EvidenceUnit(evidence_version_id=v.id, unit_index=0, heading="H", content_hash="b" * 64)
            session.add(unit)
            rid = compute_recipe_id(chunk_size=200, overlap=40, splitter="words", parser_name="docling", parser_version="1")
            if session.get(ChunkRecipe, rid) is None:
                session.add(ChunkRecipe(id=rid, chunk_size=200, overlap=40, splitter="words", parser_name="docling", parser_version="1"))
            session.flush()
            session.add(
                Chunk(
                    id=compute_chunk_id(v.id, rid, 0, "c" * 64),
                    source_id=source.id,
                    evidence_version_id=v.id,
                    evidence_unit_id=unit.id,
                    chunk_recipe_id=rid,
                    ordinal=0,
                    heading="H",
                    text="text",
                    content_hash="c" * 64,
                )
            )
        session.commit()


@pytest.fixture
def world(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data")
    docs = tmp_path / "docs"
    fresh = tmp_path / "fresh"
    docs.mkdir()
    fresh.mkdir()
    indexed = ctx.source_manager.register_source(docs)
    _add_ready_version(ctx, indexed, "old.pdf")
    # a source that has been read before (a job row) so it is not "never read"
    with ctx.session_factory() as session:
        session.add(
            IngestionJob(
                source_id=indexed.id,
                status=IngestionJobStatus.SUCCEEDED,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    never = ctx.source_manager.register_source(fresh)
    _doc(fresh, "one.pdf")
    _doc(fresh, "two.pdf")

    prune = FakePrune()
    pipeline = FakePipeline(ctx.session_factory, fresh)
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, new_manifest("embed-x", 8, index_text_version=0))
    calls: list[str] = []

    def reindex():
        calls.append("reindex")
        write_manifest(manifest, new_manifest("embed-x", 8))
        return SimpleNamespace(chunks=1, pages=0)

    m = LibraryMaintenance(
        session_factory=ctx.session_factory,
        source_manager=ctx.source_manager,
        prune_service=prune,
        pipeline=lambda: pipeline,
        reindex=reindex,
        manifest_path=manifest,
        embed_model="embed-x",
        seconds_per_file=lambda: 2.0,
    )
    yield SimpleNamespace(
        ctx=ctx,
        m=m,
        prune=prune,
        pipeline=pipeline,
        indexed=indexed,
        never=never,
        manifest=manifest,
        reindex_calls=calls,
        docs=docs,
        fresh=fresh,
    )
    ctx.engine.dispose()


def _load_all(w) -> None:
    w.prune.pending[w.indexed.id] = ["venv/x.pdf", "venv/y.pdf"]
    w.pipeline.stale[w.indexed.id] = [w.docs / "old.pdf"]


# -- plan ------------------------------------------------------------------------


def test_plan_lists_every_kind_in_run_order_with_counts_and_estimates(world):
    _load_all(world)
    plan = world.m.plan()
    assert [s.kind for s in plan.steps] == ["prune", "refresh", "rechunk", "reindex"]
    by = {s.kind: s for s in plan.steps}
    assert by["prune"].count == 2 and "2 indexed files" in by["prune"].label
    assert by["prune"].detail == ("venv/x.pdf", "venv/y.pdf")
    assert by["refresh"].count == 2 and by["refresh"].source_id == world.never.id
    assert by["rechunk"].count == 1 and "older recipe" in by["rechunk"].label
    assert by["reindex"].count == 1 and "newer search format" in by["reindex"].label
    assert plan.estimated_seconds > 0 and all(s.seconds > 0 for s in plan.steps)
    assert [s.kind for s in plan.cheap_steps] == ["prune", "refresh"]
    assert [s.kind for s in plan.costly_steps] == ["rechunk", "reindex"]
    assert re.match(r"4 updates available \((under a minute|about \d+ minutes?)\)$", plan.headline)


def test_plan_empty_says_everything_is_up_to_date(world):
    world.pipeline.stale.clear()
    # make the never-read source read and the manifest current
    with world.ctx.session_factory() as session:
        session.add(
            IngestionJob(
                source_id=world.never.id,
                status=IngestionJobStatus.SUCCEEDED,
                started_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    write_manifest(world.manifest, new_manifest("embed-x", 8))
    plan = world.m.plan()
    assert plan.empty and plan.headline == "Everything is up to date"


def test_embedding_model_mismatch_plans_a_reindex(world):
    write_manifest(world.manifest, new_manifest("another-model", 8))
    plan = world.m.plan()
    reindex = [s for s in plan.steps if s.kind == "reindex"]
    assert reindex and "embedding model changed" in reindex[0].label


def test_source_filter_limits_steps_and_leaves_global_reindex_out(world):
    _load_all(world)
    plan = world.m.plan([world.indexed.id])
    assert {s.kind for s in plan.steps} == {"prune", "rechunk"}
    assert all(s.source_id == world.indexed.id for s in plan.steps)


def test_plan_is_read_only(world):
    _load_all(world)
    world.m.plan()
    assert world.prune.applied == [] and world.pipeline.calls == [] and world.reindex_calls == []
    assert read_manifest(world.manifest).index_text_version == 0


# -- run ---------------------------------------------------------------------------


def test_run_order_progress_stream_and_result(world):
    _load_all(world)
    events: list[MaintenanceProgress] = []
    result = world.m.run(world.m.plan(), progress=events.append)
    assert result.ok, result
    assert world.prune.applied == ["venv/x.pdf", "venv/y.pdf"]
    assert world.pipeline.calls == [f"ingest:{world.never.id}", f"rechunk:{world.indexed.id}"]
    assert world.reindex_calls == ["reindex"]
    assert (result.pruned, result.files_read, result.rechunked, result.reindexed) == (2, 2, 1, True)
    stages = [e.stage for e in events]
    assert stages == sorted(stages, key=["prune", "refresh", "rechunk", "reindex"].index)
    assert set(stages) == {"prune", "refresh", "rechunk", "reindex"}
    assert all(e.done <= e.total for e in events)
    assert events[-1].done == events[-1].total
    done_values = [e.done for e in events]
    assert done_values == sorted(done_values)
    assert "removed from the index" in result.summary() and "rebuilt" in result.summary()


def test_second_plan_is_empty_after_run(world):
    _load_all(world)
    first = world.m.run(world.m.plan())
    assert first.ok
    assert world.m.plan().empty


def test_stop_between_files_skips_the_rest(world):
    _load_all(world)
    seen: list[str] = []

    def progress(p):
        seen.append(p.stage)

    count = {"n": 0}

    def stop():
        count["n"] += 1
        return len(world.prune.applied) >= 1  # after the first pruned file

    result = world.m.run(world.m.plan(), progress=progress, stop=stop)
    assert result.stopped and not result.ok
    assert world.prune.applied == ["venv/x.pdf"]
    assert world.pipeline.calls == [] and world.reindex_calls == []


def test_stop_during_refresh_ends_the_run_cleanly(world):
    world.prune.pending.clear()
    flag = {"stop": False}

    def progress(p):
        if p.stage == "refresh" and p.done >= 1:
            flag["stop"] = True

    result = world.m.run(world.m.plan(), progress=progress, stop=lambda: flag["stop"])
    assert result.stopped
    assert world.reindex_calls == []  # nothing after the stop ran


def test_reindex_only_when_planned(world):
    write_manifest(world.manifest, new_manifest("embed-x", 8))
    _load_all(world)
    plan = world.m.plan()
    assert "reindex" not in plan.kinds()
    world.m.run(plan)
    assert world.reindex_calls == []


def test_reindex_failure_is_reported_and_leaves_the_old_index(world):
    def failing():
        raise ValueError("embedding service failed")

    world.m._reindex = failing
    result = world.m.run(world.m.plan().only({"reindex"}))
    assert not result.ok and not result.reindexed
    assert "embedding service failed" in result.reindex_error
    assert "left as it was" in result.summary()
    assert read_manifest(world.manifest).index_text_version == 0  # untouched: old index still named


def test_failed_rechunk_file_is_reported_not_raised(world):
    world.pipeline.stale[world.indexed.id] = [world.docs / "old.pdf"]
    original = world.pipeline.rechunk_source

    def broken(source_id, **kw):
        report = original(source_id, **kw)
        report.results = [FileIngestResult(world.docs / "old.pdf", "failed", error="bad zip file")]
        return report

    world.pipeline.rechunk_source = broken
    result = world.m.run(world.m.plan().only({"rechunk"}))
    assert result.files_failed == 1 and result.failures[0][0].endswith("old.pdf")


# -- notices and reasons ---------------------------------------------------------------


def test_notices_contain_no_command_names(world):
    _load_all(world)
    lines = [
        *ignore_notices(SimpleNamespace(files_skipped=0), world.prune, world.indexed.id),
        stale_recipe_notice(2),
        stale_recipe_notice(1),
        index_format_notice(),
        *world.m.plan().lines(),
        world.m.plan().headline,
    ]
    for line in lines:
        assert "docket " not in line.lower() and "`" not in line, line
        assert "--" not in line and "run " not in line.lower(), line


@pytest.mark.parametrize(
    "reason, path, expect",
    [
        ("File is password protected", "a.pdf", "Password-protected"),
        ("unsupported file format '.xls' for x.xls: only .xlsx is implemented", "b.xls", "Save a copy as .xlsx"),
        ("unsupported file format '.ppt' for x.ppt: only .pptx", "c.ppt", "Save a copy as .pptx"),
        ("", "d.doc", "Save a copy as .docx"),
        ("File is not a zip file", "e.xlsx", "damaged or unreadable"),
        ("the document is empty", "f.pdf", "empty"),
        (None, "g.pdf", "could not read this file"),
        ("parse error: bad header", "h.md", "parse error: bad header"),
    ],
)
def test_failure_reason_mapping(reason, path, expect):
    assert expect.lower() in plain_failure_reason(reason, path).lower()


# -- CLI -----------------------------------------------------------------------------------


@pytest.fixture
def cli(world, monkeypatch):
    world.ctx.__dict__["maintenance"] = world.m
    monkeypatch.setattr(cli_main, "build_context", lambda: world.ctx)
    return world


def test_sync_dry_run_prints_the_plan_and_changes_nothing(cli):
    _load_all(cli)
    result = runner.invoke(cli_main.app, ["sync", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "4 updates available" in result.output
    assert "Dry run: nothing changed." in result.output
    assert "docket sync" not in result.output
    assert cli.prune.applied == [] and cli.pipeline.calls == [] and cli.reindex_calls == []


def test_sync_runs_quick_steps_and_skips_slow_ones_without_yes(cli):
    _load_all(cli)
    result = runner.invoke(cli_main.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert cli.prune.applied == ["venv/x.pdf", "venv/y.pdf"]
    assert cli.pipeline.calls == [f"ingest:{cli.never.id}"]  # no rechunk
    assert cli.reindex_calls == []
    assert "slower updates" in result.output and "--yes" in result.output


def test_sync_yes_runs_everything_and_a_second_run_is_up_to_date(cli):
    _load_all(cli)
    result = runner.invoke(cli_main.app, ["sync", "--yes"])
    assert result.exit_code == 0, result.output
    assert cli.reindex_calls == ["reindex"]
    again = runner.invoke(cli_main.app, ["sync"])
    assert again.exit_code == 0 and "Everything is up to date." in again.output


def test_sync_asks_once_with_the_estimate_on_a_terminal(cli, monkeypatch):
    _load_all(cli)
    monkeypatch.setattr(cli_main, "_interactive", lambda: True)
    declined = runner.invoke(cli_main.app, ["sync"], input="n\n")
    assert "Run the slower updates now?" in declined.output
    assert re.search(r"takes (under a minute|about \d+ minutes?)", declined.output)
    assert cli.reindex_calls == [] and cli.pipeline.calls == [f"ingest:{cli.never.id}"]
    accepted = runner.invoke(cli_main.app, ["sync"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert cli.reindex_calls == ["reindex"]


def test_sync_for_one_source_by_name(cli):
    _load_all(cli)
    result = runner.invoke(cli_main.app, ["sync", "docs", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "2 updates available" in result.output
    assert "fresh" not in result.output


def test_sync_reindex_failure_exits_nonzero_and_says_so(cli):
    def failing():
        raise ValueError("model offline")

    cli.m._reindex = failing
    result = runner.invoke(cli_main.app, ["sync", "--yes"])
    assert result.exit_code == 1
    assert "left as it was" in result.output


def test_plan_helpers_headline_singular():
    one = MaintenancePlan((MaintenanceStep("prune", 1, "x", seconds=1.0),))
    assert one.headline.startswith("1 update available")
    assert one.only({"reindex"}).empty
    assert INDEX_TEXT_VERSION >= 1


# -- classic prompt ------------------------------------------------------------------------


class _Status:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, text):
        self.last = text


def _classic_session(m, answer: str):
    said: list[str] = []
    asked: list[str] = []

    def read(prompt: str) -> str:
        asked.append(prompt)
        return answer

    return SimpleNamespace(
        context=SimpleNamespace(maintenance=m),
        say=lambda text="", **kw: said.append(text),
        error=lambda text: said.append(text),
        reader=SimpleNamespace(read=read),
        console=SimpleNamespace(status=lambda text: _Status()),
        said=said,
        asked=asked,
    )


def test_classic_prompt_offers_one_yes_no_and_runs_the_plan(world):
    from docket.interfaces.cli.interactive.ingestion_ui import offer_update

    _load_all(world)
    session = _classic_session(world.m, "y")
    offer_update(session, [world.indexed.id])
    assert len(session.asked) == 1
    assert session.asked[0].endswith("Update now? [y/N] ")
    assert "docket " not in session.asked[0].lower()
    assert world.prune.applied == ["venv/x.pdf", "venv/y.pdf"]
    assert world.pipeline.calls == [f"rechunk:{world.indexed.id}"]
    assert world.reindex_calls == []  # global: only with no source filter
    assert any("removed from the index" in line for line in session.said)
    assert not any("docket " in line.lower() for line in session.said)


def test_classic_prompt_declined_changes_nothing(world):
    from docket.interfaces.cli.interactive.ingestion_ui import offer_update

    _load_all(world)
    session = _classic_session(world.m, "n")
    offer_update(session, [world.indexed.id])
    assert world.prune.applied == [] and world.pipeline.calls == []


def test_classic_prompt_is_silent_when_up_to_date(world):
    from docket.interfaces.cli.interactive.ingestion_ui import offer_update

    session = _classic_session(world.m, "y")
    offer_update(session, [world.indexed.id])
    assert session.asked == [] and session.said == []
