"""CP5 in the full-screen UI: the library health line, Update now on the
indexing slot, plain failure reasons, Retry / Ignore this file, and Narrow.

A stub backend with real worker-thread semantics for the controller, plus
RealBackend checks over a scratch database. No Ollama, no parser, no user data.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from test_tui_real import (
    StubBackend,
    add_file,
    backend_for,
    ctx,  # noqa: F401 -- fixtures
    folder,  # noqa: F401
    make_context,
    scenario,
)

from docket.core.db.models import VersionStatus
from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui import overlays as ov
from docket.interfaces.cli.tui.backend import (
    BackendError,
    IndexOutcome,
    IndexProgress,
    NarrowPlan,
)
from docket.services.ingestion.pipeline import FileIngestResult, IngestionJobResult
from docket.services.sources.maintenance import MaintenancePlan, MaintenanceStep
from docket.services.sources.scan import ScanResult
from docket.services.sources.session import KIND_NEW, SessionPlan


def _plan(*kinds: str) -> MaintenancePlan:
    table = {
        "prune": MaintenanceStep("prune", 2, "Finance: 2 indexed files no longer read will be removed.", 1.0),
        "refresh": MaintenanceStep("refresh", 4, "Handbook: 4 documents not read yet.", 40.0),
        "rechunk": MaintenanceStep("rechunk", 3, "Finance: 3 documents will be re-prepared.", 90.0),
        "reindex": MaintenanceStep("reindex", 50, "The search index will be rebuilt (a newer search format is available).", 30.0),
    }
    return MaintenancePlan(tuple(table[k] for k in kinds))


class MaintBackend(StubBackend):
    def __init__(self, plan: MaintenancePlan | None = None) -> None:
        super().__init__(sources=True)
        self.plan = plan if plan is not None else MaintenancePlan()
        self.plan_calls = 0
        self.runs: list[dict] = []
        self.release = threading.Event()
        self.release.set()
        self.ignored: list[tuple[str, str]] = []
        self.retried: list[tuple[str, str]] = []
        self.narrowed: list[tuple[str, tuple, tuple]] = []
        self.narrow = NarrowPlan("src-demo-finance", _narrow_session(), ("c",))

    def maintenance_plan(self):
        self.plan_calls += 1
        return self.plan

    def run_maintenance(self, plan, on_progress, should_stop, *, source_ids=None, kinds=None):
        self.runs.append({"source_ids": source_ids, "kinds": kinds})
        total = 4
        for i in range(total):
            on_progress(IndexProgress(i, total, f"f{i}.pdf", indexed=i, stage="Re-preparing documents"))
            deadline = time.monotonic() + 5
            while not self.release.wait(0.01):
                if should_stop() or time.monotonic() > deadline:
                    return IndexOutcome("stopped", IndexProgress(i, total, "", indexed=i))
        self.plan = MaintenancePlan()
        return IndexOutcome(
            "done",
            IndexProgress(total, total, "", indexed=total),
            notices=("2 files removed from the index; 3 documents re-prepared.",),
        )

    def ignore_file(self, source_id, rel_path):
        self.ignored.append((source_id, rel_path))
        return super().ignore_file(source_id, rel_path)

    def retry_file(self, source_id, rel_path, on_progress, should_stop):
        self.retried.append((source_id, rel_path))
        src = self.world.find(source_id)
        for f in src.files:
            if f.path == rel_path:
                f.status, f.error = fd.FILE_READY, ""
        on_progress(IndexProgress(0, 1, rel_path))
        return IndexOutcome(
            "done", IndexProgress(1, 1, "", indexed=1), notices=(f"{rel_path} was read and is searchable now.",)
        )

    def narrow_plan(self, source_id):
        return self.narrow

    def narrow_source(self, source_id, keep, universe):
        self.narrowed.append((source_id, tuple(keep), tuple(universe)))


def _narrow_session() -> SessionPlan:
    scan = ScanResult(
        root=Path("/demo/work/finance"),
        documents=6,
        by_type={".pdf": 6},
        by_subfolder={"a": 3, "b": 2, "c": 1},
        root_documents=0,
        broad=True,
        broad_reason="many subfolders",
    )
    return SessionPlan(KIND_NEW, folder=Path("/demo/work/finance"), name="Finance", scan=scan, needs_preview=True)


def _health_ready(h, b) -> bool:
    return h.ui.health is not None


# ---------------------------------------------------------------------------
# health line
# ---------------------------------------------------------------------------


def test_health_line_up_to_date_and_updates_available():
    async def up_to_date(h, b):
        await h.wait_for(lambda: h.ui.health is not None)
        assert h.ui.health_line() == "Everything is up to date"
        await h.press("f2")
        assert "Library health: Everything is up to date" in h.text()

    scenario(up_to_date, MaintBackend())

    async def available(h, b):
        await h.wait_for(lambda: h.ui.health is not None)
        line = h.ui.health_line()
        assert line.startswith("3 updates available (") and "minute" in line
        notices = [m.text for m in h.ui.messages if m.role == "system"]
        assert any("3 updates available" in n and "Update now" in n for n in notices)
        assert not any("docket " in n.lower() or "`" in n for n in notices)
        await h.press("f2")
        assert "Library health: 3 updates available" in h.text()
        assert "Update now" in h.text()

    scenario(available, MaintBackend(_plan("prune", "rechunk", "reindex")))


def test_update_now_is_disabled_when_up_to_date():
    async def go(h, b):
        await h.wait_for(lambda: h.ui.health is not None)
        await h.press("f2")
        sources = h.ui.overlays[-1]
        btn = next(x for x in sources.buttons() if x.key == "update")
        assert not btn.enabled and "up to date" in btn.why

    scenario(go, MaintBackend())


# ---------------------------------------------------------------------------
# Update now
# ---------------------------------------------------------------------------


def test_update_now_quick_steps_run_without_asking_on_the_indexing_slot():
    async def go(h, b):
        await h.wait_for(lambda: h.ui.health is not None)
        h.ui.update_now()
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert h.ui.job.kind == "update" and h.ui.job.state == "done"
        assert b.runs and b.runs[0]["kinds"] is None
        assert not any(isinstance(o, ov.ConfirmOverlay) for o in h.ui.overlays)
        texts = [m.text for m in h.ui.messages if m.role == "system"]
        assert any("re-prepared" in t for t in texts)
        # health is looked at again once the job ends
        await h.wait_for(lambda: h.ui.health is not None and h.ui.health.empty)
        assert h.ui.health_line() == "Everything is up to date"

    scenario(go, MaintBackend(_plan("prune", "refresh")))


def test_update_now_with_slow_steps_asks_once_with_the_estimate():
    async def go(h, b):
        await h.wait_for(lambda: h.ui.health is not None)
        h.ui.update_now()
        await h.settle()
        confirm = h.ui.overlays[-1]
        assert isinstance(confirm, ov.ConfirmOverlay)
        assert "2 updates available" in confirm.message and "about" in confirm.message
        assert "Docket" not in confirm.message or "docket " not in confirm.message.lower()
        assert b.runs == []  # nothing runs until it is confirmed
        confirm.press("0")  # Update now
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert len(b.runs) == 1

    scenario(go, MaintBackend(_plan("rechunk", "reindex")))


def test_update_pauses_for_a_question_between_files():
    b = MaintBackend(_plan("prune", "refresh"))

    async def go(h, b):
        await h.wait_for(lambda: h.ui.health is not None)
        b.release.clear()
        h.ui.update_now()
        await h.wait_for(lambda: h.ui.job is not None and h.ui.job.active)
        assert h.ui.indexing_active
        # a question is refused for the slot count, not because of the update
        b.ask_gate.clear()
        await h.type("what is revenue?")
        await h.press("enter")
        await h.wait_for(b.ask_started.is_set)
        assert h.ui.question_active and h.ui.indexing_active  # two slots, both taken
        b.ask_gate.set()
        b.release.set()
        await h.wait_for(lambda: len(h.ui.answers()) == 1)
        await h.wait_for(lambda: not h.ui.job.active)
        assert h.ui.job.state == "done"
        # a question never claims "N of M documents indexed" for an update
        assert b.ask_calls[0].index_coverage is None

    scenario(go, b)


def test_update_is_refused_while_a_question_runs_and_says_why():
    b = MaintBackend(_plan("prune"))

    async def go(h, b):
        await h.wait_for(lambda: h.ui.health is not None)
        b.ask_gate.clear()
        await h.type("hello?")
        await h.press("enter")
        await h.wait_for(b.ask_started.is_set)
        h.ui.update_now()
        await h.settle()
        assert b.runs == []
        assert any("question is being answered" in m.text for m in h.ui.messages if m.role == "system")
        b.ask_gate.set()

    scenario(go, b)


# ---------------------------------------------------------------------------
# failed files
# ---------------------------------------------------------------------------


def test_failed_files_overlay_ignore_clears_need_attention():
    async def go(h, b):
        src = h.ui.world.find("src-demo-finance")
        assert src.count(fd.FILE_FAILED) == 2
        before = h.ui.world.attention()
        await h.press("f2")
        sources = h.ui.overlays[-1]
        sources.press("failed")
        await h.settle()
        failed = h.ui.overlays[-1]
        assert isinstance(failed, ov.FailedFilesOverlay)
        assert "Failed files" not in failed.title and "need attention" in failed.title
        first = failed.entries()[0].data.path
        failed.press("ignore")
        await h.settle()
        assert b.ignored == [("src-demo-finance", first)]
        assert h.ui.world.attention() == before - 1
        failed2 = h.ui.overlays[-1]
        failed2.press("ignore")
        await h.settle()
        assert h.ui.world.attention() == before - 2
        # the empty overlay closes itself and the panel remains
        assert isinstance(h.ui.overlays[-1], ov.SourcesOverlay)
        assert any("left out from now on" in m.text for m in h.ui.messages if m.role == "system")

    scenario(go, MaintBackend())


def test_retry_one_file_runs_a_job_and_clears_it():
    async def go(h, b):
        await h.press("f2")
        h.ui.overlays[-1].press("failed")
        await h.settle()
        failed = h.ui.overlays[-1]
        path = failed.entries()[0].data.path
        failed.press("retry")
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert b.retried == [("src-demo-finance", path)]
        assert h.ui.job.kind == "retry" and h.ui.job.state == "done"
        src = h.ui.world.find("src-demo-finance")
        assert src.count(fd.FILE_FAILED) == 1
        assert any("searchable now" in m.text for m in h.ui.messages if m.role == "system")

    scenario(go, MaintBackend())


def test_failed_files_button_is_disabled_when_nothing_failed():
    async def go(h, b):
        await h.press("f2")
        sources = h.ui.overlays[-1]
        for i, e in enumerate(sources.entries()):
            if e.data.id == "src-demo-handbook":
                sources.sel = i
        btn = next(x for x in sources.buttons() if x.key == "failed")
        assert not btn.enabled and "No file" in btn.why

    scenario(go, MaintBackend())


# ---------------------------------------------------------------------------
# narrow
# ---------------------------------------------------------------------------


def test_narrow_shows_the_preview_with_left_out_folders_unticked_and_changes_nothing_until_confirmed():
    async def go(h, b):
        await h.press("f2")
        sources = h.ui.overlays[-1]
        sources.press("narrow")
        await h.wait_for(lambda: isinstance(h.ui.overlays[-1], ov.FolderPreviewOverlay))
        preview = h.ui.overlays[-1]
        assert preview.narrow is not None and preview.ticked == {"a": True, "b": True, "c": False}
        assert "Nothing changes until you confirm" in h.text()
        assert b.narrowed == [] and b.runs == []
        # cancelling changes nothing
        preview.press("cancel")
        await h.settle()
        assert b.narrowed == [] and b.runs == []
        assert not any(isinstance(o, ov.FolderPreviewOverlay) for o in h.ui.overlays)

    scenario(go, MaintBackend())


def test_narrow_confirm_saves_rules_then_prunes_on_the_indexing_slot():
    async def go(h, b):
        await h.press("f2")
        h.ui.overlays[-1].press("narrow")
        await h.wait_for(lambda: isinstance(h.ui.overlays[-1], ov.FolderPreviewOverlay))
        preview = h.ui.overlays[-1]
        preview._toggle("b")  # leave "b" out as well
        assert preview.selected() == 3
        preview.press("go")
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert b.narrowed == [("src-demo-finance", ("a",), ("a", "b", "c"))]
        assert b.runs == [{"source_ids": ["src-demo-finance"], "kinds": frozenset({"prune"})}]
        assert h.ui.job.kind == "update"

    scenario(go, MaintBackend())


def test_nothing_to_narrow_is_said_in_plain_words():
    b = MaintBackend()
    b.narrow = NarrowPlan("src-demo-finance", _narrow_session(), (), reason="Finance is small and has little to leave out, so there is nothing to narrow.")

    async def go(h, b):
        await h.press("f2")
        h.ui.overlays[-1].press("narrow")
        await h.wait_for(lambda: any("nothing to narrow" in m.text for m in h.ui.messages if m.role == "system"))
        assert not any(isinstance(o, ov.FolderPreviewOverlay) for o in h.ui.overlays)

    scenario(go, b)


# ---------------------------------------------------------------------------
# RealBackend over a scratch database
# ---------------------------------------------------------------------------


class _Pipe:
    def __init__(self, ctx_) -> None:
        self.ctx = ctx_
        self.paths: list[list[Path]] = []

    def find_stale_versions(self, source_id):
        return []

    def run_ingestion_for_paths(self, source_id, paths, progress=None):
        self.paths.append(list(paths))
        return IngestionJobResult(
            source_id, "j", "succeeded", 1, 0, [FileIngestResult(paths[0], "ingested", chunks_written=1)]
        )


def _real(ctx_, folder_):
    src = ctx_.source_manager.register_source(folder_)
    add_file(ctx_, src, "ok.docx")
    add_file(ctx_, src, "locked.pdf", status=VersionStatus.FAILED, chunk=False)
    with ctx_.session_factory() as session:
        from sqlalchemy import select

        from docket.core.db.models import EvidenceVersion

        v = session.execute(select(EvidenceVersion).where(EvidenceVersion.file_path.like("%locked.pdf"))).scalar_one()
        v.failure_reason = "File is password protected"
        session.commit()
    add_file(ctx_, src, "old.doc", status=VersionStatus.FAILED, chunk=False)
    return src


def test_real_world_shows_the_stored_reason_in_plain_words(ctx, folder):
    src = _real(ctx, folder)
    world = backend_for(ctx).load_world()
    files = {f.path: f for f in world.find(src.id).files}
    assert "Password-protected" in files["locked.pdf"].error
    assert "Save a copy as .docx" in files["old.doc"].error
    assert "reason not stored" not in files["locked.pdf"].error


def test_real_jobs_details_list_failed_files_with_reasons(ctx, folder):
    from datetime import datetime, timezone

    from docket.core.db.models import IngestionJob, IngestionJobStatus

    src = _real(ctx, folder)
    with ctx.session_factory() as session:
        session.add(
            IngestionJob(
                source_id=src.id,
                status=IngestionJobStatus.PARTIAL,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                stats_json='{"files_processed": 3, "files_failed": 2, "chunks_written": 1}',
            )
        )
        session.commit()
    records = backend_for(ctx).job_history(running=False)
    assert records and records[0].state == "Partial"
    joined = " | ".join(records[0].details)
    assert "locked.pdf: Password-protected" in joined and "old.doc:" in joined


def test_real_ignore_file_and_retry_via_the_backend(ctx, folder):
    src = _real(ctx, folder)
    pipe = _Pipe(ctx)
    ctx.__dict__["pipeline"] = pipe
    ctx.__dict__["prune_service"] = _PruneOnRows(ctx)
    be = backend_for(ctx)
    assert be.ignore_file(src.id, "locked.pdf") == 1
    world = be.load_world()
    assert {f.path for f in world.find(src.id).files if f.status == fd.FILE_FAILED} == {"old.doc"}
    assert world.attention() == 1

    seen: list[IndexProgress] = []
    outcome = be.retry_file(src.id, "old.doc", seen.append, lambda: False)
    assert outcome.state == "done" and "searchable now" in outcome.notices[0]
    assert pipe.paths == [[folder / "old.doc"]]


class _PruneOnRows:
    """The real SourcePruneService, built over the scratch context (no index)."""

    def __new__(cls, ctx_):
        from docket.services.sources.prune import SourcePruneService

        return SourcePruneService(ctx_.session_factory, ctx_.evidence_manager, None)


def test_real_narrow_plan_pre_unticks_left_out_folders(ctx, folder):
    for rel in ("a/x.pdf", "b/y.pdf", "c/z.pdf", "root.pdf"):
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4\n")
    src = ctx.source_manager.register_source(folder)
    ctx.source_manager.narrow_source(src.id, ["a", "b"])  # leaves "c" out
    be = backend_for(ctx)
    plan = be.narrow_plan(src.id)
    assert plan.unticked == ("c",)
    assert dict(plan.plan.scan.by_subfolder) == {"a": 1, "b": 1, "c": 1}  # the whole tree is shown
    be.narrow_source(src.id, ("a",), ("a", "b", "c"))
    import json

    rules = json.loads(ctx.source_manager.get_source(src.id).rules_json)
    assert sorted(rules["exclude"]) == ["/b/", "/c/"]


def test_real_narrow_plan_for_a_small_folder_has_nothing_to_narrow(ctx, folder):
    (folder / "one.pdf").write_bytes(b"%PDF-1.4\n")
    src = ctx.source_manager.register_source(folder)
    plan = backend_for(ctx).narrow_plan(src.id)
    assert "nothing to narrow" in plan.reason


def test_real_backend_missing_folder_is_a_plain_error(ctx, folder, tmp_path):
    import shutil

    src = ctx.source_manager.register_source(folder)
    shutil.rmtree(folder)
    try:
        backend_for(ctx).narrow_plan(src.id)
    except BackendError as exc:
        assert "cannot be reached" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a BackendError")


def test_real_maintenance_plan_runs_over_the_scratch_context(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    (folder / "a.pdf").write_bytes(b"%PDF-1.4\n")
    plan = backend_for(ctx).maintenance_plan()
    assert [s.kind for s in plan.steps] == ["refresh"]
    assert plan.steps[0].source_id == src.id
