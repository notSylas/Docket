"""Wiring tests for ``docket ui``: the real backend over fake gateways/pipelines
and temp dirs, plus headless TUI scenarios on a controllable stub backend.

No Ollama, no parser, no model, no user data: every context is rooted in a
pytest temp dir and every pipeline/service is a test double. Every UI
scenario runs under a hard timeout.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from docket.core.db.identity import compute_chunk_id, compute_recipe_id
from docket.core.db.models import (
    Chunk,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    IngestionJob,
    IngestionJobStatus,
    SourceStatus,
    VersionStatus,
)
from docket.infra.inference.gateway import InferenceUnavailableError
from docket.infra.inference.health import HealthReport
from docket.interfaces.cli.context import AppContext
from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui.backend import (
    AnswerView,
    AskOutcome,
    AskRequest,
    BackendError,
    CitationView,
    FakeBackend,
    FolderCheck,
    IndexOutcome,
    IndexProgress,
    JobRecord,
    ReadinessItem,
    ReadinessReport,
    Turn,
)
from docket.interfaces.cli.tui.headless import Harness, run
from docket.interfaces.cli.tui.real_backend import RealBackend
from docket.services.ingestion.pipeline import FileIngestResult, IngestionJobResult, ProgressEvent
from docket.services.query.classifier import QueryMode
from docket.services.query.service import Citation, QueryResult


# ---------------------------------------------------------------------------
# real-backend fixtures
# ---------------------------------------------------------------------------


class FakeWriter:
    def __init__(self, table: object = "TABLE") -> None:
        self.table = table


class FakePipeline:
    """Mirrors the real pipeline's progress contract, including swallowing
    ``Exception`` from the callback while letting ``BaseException`` through."""

    def __init__(self, plan: list[tuple[Path, str, str | None]], status: str = "succeeded") -> None:
        self.plan = plan
        self.status = status
        self.runs: list[str] = []
        self.stopped_after: int | None = None

    def find_stale_versions(self, source_id: str) -> list:
        return []

    def run_ingestion_for_source(self, source_id: str, progress=None) -> IngestionJobResult:
        self.runs.append(source_id)
        results: list[FileIngestResult] = []
        total = len(self.plan)

        def emit(event: ProgressEvent) -> None:
            if progress is None:
                return
            try:
                progress(event)
            except Exception:
                pass

        for i, (path, status, error) in enumerate(self.plan, start=1):
            emit(ProgressEvent("start", i, total, path))
            r = FileIngestResult(path=path, status=status, error=error, chunks_written=3 if status == "ingested" else 0)
            results.append(r)
            emit(ProgressEvent("done", i, total, path, r))
        failed = sum(1 for r in results if r.status == "failed")
        return IngestionJobResult(
            source_id=source_id,
            job_id="job_test",
            status=self.status,
            files_processed=len(results),
            files_failed=failed,
            file_results=results,
        )


class FakeService:
    def __init__(self, result_for=None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error
        self.result_for = result_for

    def ask(self, question, mode=None, history=None):
        self.calls.append({"question": question, "mode": mode, "history": list(history or [])})
        if self.error:
            raise self.error
        return self.result_for(question, mode)


def make_context(tmp_path: Path, plan=None, writer_table: object = "TABLE") -> AppContext:
    context = AppContext.for_testing(data_dir=tmp_path / "data")
    context.__dict__["vector_writer"] = FakeWriter(writer_table)
    context.__dict__["pipeline"] = FakePipeline(plan or [])
    context.__dict__["prune_service"] = SimpleNamespace(count=lambda source_id: 0)
    return context


def add_file(
    context: AppContext,
    source,
    rel: str,
    text: str = "Revenue was 4.2 in the north.",
    *,
    status: VersionStatus = VersionStatus.READY,
    chunk: bool = True,
    locator: str | None = None,
    observed: datetime | None = None,
    content_hash: str | None = None,
) -> str | None:
    """Insert one evidence version (+ unit + chunk). Returns the chunk id."""
    with context.session_factory() as session:
        version = EvidenceVersion(
            source_id=source.id,
            file_path=str(Path(source.path) / rel),
            content_hash=content_hash or (rel.encode().hex() * 8)[:64].ljust(64, "a"),
            byte_size=10,
            observed_at=observed or datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc),
            parser_name="docling",
            parser_version="1",
            status=status,
        )
        session.add(version)
        session.flush()
        chunk_id = None
        if chunk:
            unit = EvidenceUnit(evidence_version_id=version.id, unit_index=0, heading="Summary", content_hash="b" * 64, locator_json=locator)
            session.add(unit)
            recipe_id = compute_recipe_id(chunk_size=200, overlap=40, splitter="words", parser_name="docling", parser_version="1")
            if session.get(ChunkRecipe, recipe_id) is None:
                session.add(ChunkRecipe(id=recipe_id, chunk_size=200, overlap=40, splitter="words", parser_name="docling", parser_version="1"))
            session.flush()
            chunk_id = compute_chunk_id(version.id, recipe_id, 0, "c" * 64)
            session.add(
                Chunk(
                    id=chunk_id,
                    source_id=source.id,
                    evidence_version_id=version.id,
                    evidence_unit_id=unit.id,
                    chunk_recipe_id=recipe_id,
                    ordinal=0,
                    heading="Summary",
                    text=text,
                    content_hash="c" * 64,
                )
            )
        session.commit()
        return chunk_id


def label_for(name: str, chunk_id: str) -> str:
    return f"[{name} #{chunk_id[:12]}]"


def backend_for(context, service=None, health=None) -> RealBackend:
    return RealBackend(
        context,
        query_service_factory=lambda ctx, table: service,
        health_check=lambda: health or HealthReport(reachable=True),
    )


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    f = tmp_path / "docs"
    f.mkdir()
    return f


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    return make_context(tmp_path)


# ---------------------------------------------------------------------------
# readiness and sources
# ---------------------------------------------------------------------------


def test_world_reports_source_states_files_and_counts(ctx, folder, tmp_path):
    src = ctx.source_manager.register_source(folder)
    add_file(ctx, src, "a.xlsx")
    add_file(ctx, src, "b.pdf", status=VersionStatus.FAILED, chunk=False)
    add_file(ctx, src, "c.docx", chunk=False)  # READY but no chunks
    add_file(ctx, src, "old.pdf", status=VersionStatus.SUPERSEDED)
    gone = tmp_path / "gone"
    gone.mkdir()
    missing = ctx.source_manager.register_source(gone)
    ctx.source_manager.mark_unreachable(missing.id)
    away = tmp_path / "away"
    away.mkdir()
    revoked = ctx.source_manager.register_source(away)
    ctx.source_manager.deactivate_source(revoked.id, reason="user_disconnected")

    world = backend_for(ctx).load_world()
    by_id = {s.id: s for s in world.sources}
    s = by_id[src.id]
    assert (s.status, s.name) == (fd.READY, "docs")
    assert s.count(fd.FILE_READY) == 1 and s.count(fd.FILE_FAILED) == 1 and s.count(fd.FILE_EMPTY) == 1
    assert len(s.files) == 3  # the superseded version is not a file row
    assert s.searchable == 1
    assert by_id[missing.id].status == fd.FAILED and "cannot be reached" in by_id[missing.id].note
    assert by_id[revoked.id].status == fd.DISCONNECTED and "Disconnected by you" in by_id[revoked.id].note
    assert world.ready_files() == 1
    assert world.attention() == 1 + 1  # one failed file, one failed source


def test_world_names_distinguish_same_named_folders(ctx, tmp_path):
    a, b = tmp_path / "x" / "reports", tmp_path / "y" / "reports"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    ctx.source_manager.register_source(a)
    ctx.source_manager.register_source(b)
    names = sorted(s.name for s in backend_for(ctx).load_world().sources)
    assert names == ["reports (x)", "reports (y)"]


def test_readiness_numbers_and_blocked_variants(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    add_file(ctx, src, "a.xlsx")
    ok = backend_for(ctx, health=HealthReport(reachable=True)).readiness()
    values = {i.label: i.value for i in ok.items}
    assert not ok.blocked
    assert values["Ollama"] == "Available"
    assert values["Searchable documents"] == "1 files · 1 chunks"
    assert "installed" in values["Answer model"]

    down = backend_for(ctx, health=HealthReport(reachable=False, error="refused", host="http://h:1")).readiness()
    assert down.blocked and "ollama serve" in down.help
    assert any(i.bad and "http://h:1" in i.value for i in down.items)

    gen = ctx.settings.gen_model
    miss = backend_for(ctx, health=HealthReport(reachable=True, missing_models=[gen])).readiness()
    assert miss.blocked and f"ollama pull {gen}" in miss.help
    assert any(i.label == "Answer model" and i.bad for i in miss.items)


def test_check_folder_validates_paths(ctx, folder, tmp_path):
    be = backend_for(ctx)
    assert not be.check_folder("").ok
    assert "does not exist" in be.check_folder(str(tmp_path / "nope")).message
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert "is a file" in be.check_folder(str(f)).message
    ok = be.check_folder(str(folder))
    assert ok.ok and ok.path == str(folder.resolve())
    ctx.source_manager.register_source(folder)
    assert "already registered" in be.check_folder(str(folder)).message
    src = ctx.source_manager.list_sources()[0]
    ctx.source_manager.deactivate_source(src.id, reason="user_disconnected")
    assert "Reconnect" in be.check_folder(str(folder)).message
    assert be.is_broad_location("/") and be.is_broad_location("~") and not be.is_broad_location(str(folder))


def test_source_actions_add_disconnect_reconnect(ctx, folder, tmp_path):
    be = backend_for(ctx)
    view = be.add_source(str(folder))
    assert view.status == fd.READY and [s.id for s in be.load_world().sources] == [view.id]
    with pytest.raises(BackendError):
        be.add_source(str(tmp_path / "missing"))
    be.disconnect_source(view.id)
    assert be.load_world().find(view.id).status == fd.DISCONNECTED
    be.reconnect_source(view.id)
    assert be.load_world().find(view.id).status == fd.READY
    with pytest.raises(BackendError):
        be.reconnect_source(view.id)  # not disconnected any more
    with pytest.raises(BackendError):
        be.disconnect_source("src_unknown")


def test_job_history_from_job_rows(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    with ctx.session_factory() as session:
        for status, error, stats in (
            (IngestionJobStatus.SUCCEEDED, None, '{"files_processed": 3, "files_failed": 0, "chunks_written": 9}'),
            (IngestionJobStatus.PARTIAL, None, '{"files_processed": 3, "files_failed": 1, "chunks_written": 6}'),
            (IngestionJobStatus.FAILED, "interrupted", None),
            (IngestionJobStatus.RUNNING, None, None),
        ):
            session.add(IngestionJob(source_id=src.id, status=status, error=error, stats_json=stats, finished_at=datetime.now(timezone.utc)))
        session.commit()
    be = backend_for(ctx)
    states = sorted(r.state for r in be.job_history(running=False))
    assert states == ["Completed", "Interrupted", "Interrupted", "Partial"]
    assert sorted(r.state for r in be.job_history(running=True)) == ["Completed", "Interrupted", "Partial"]
    done = next(r for r in be.job_history(running=False) if r.state == "Completed")
    assert done.name == "docs" and "3 processed" in done.summary and "9 chunks" in done.summary


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------


def test_run_index_reports_progress_counts_and_concise_failures(tmp_path, folder):
    plan = [
        (folder / "a.pdf", "ingested", None),
        (folder / "sub" / "b.xlsx", "unchanged", None),
        (folder / "c.docx", "failed", "failed to parse /x/c.docx for source s:\nTraceback...\nBadZip"),
    ]
    ctx = make_context(tmp_path, plan)
    src = ctx.source_manager.register_source(folder)
    seen: list[IndexProgress] = []
    out = backend_for(ctx).run_index(src.id, seen.append, lambda: False)
    assert out.state == "done"
    p = out.progress
    assert (p.index, p.total, p.indexed, p.unchanged, p.failed) == (3, 3, 1, 1, 1)
    assert p.failures == (("c.docx", "Traceback..."),)
    assert any(s.current == "a.pdf" and s.index == 0 and s.total == 3 for s in seen)
    assert any(s.current == str(Path("sub") / "b.xlsx") and s.index == 2 for s in seen)
    assert ctx.pipeline.runs == [src.id]


def test_run_index_stop_is_cooperative_and_between_files(tmp_path, folder):
    plan = [(folder / f"f{i}.pdf", "ingested", None) for i in range(4)]
    ctx = make_context(tmp_path, plan)
    src = ctx.source_manager.register_source(folder)
    flag = {"stop": False}

    def progress(p: IndexProgress) -> None:
        if p.index == 1:  # after the first file has finished
            flag["stop"] = True

    out = backend_for(ctx).run_index(src.id, progress, lambda: flag["stop"])
    assert out.state == "stopped"
    # the file in flight finished; the next one never started
    assert out.progress.index == 1 and out.progress.indexed == 1 and out.progress.total == 4


def test_run_index_empty_and_unreachable_folder_reasons(tmp_path, folder):
    ctx = make_context(tmp_path, [], )
    ctx.pipeline.status = "failed"
    src = ctx.source_manager.register_source(folder)
    out = backend_for(ctx).run_index(src.id, lambda p: None, lambda: False)
    assert out.state == "failed" and "No supported files" in out.reason
    ctx.source_manager.mark_unreachable(src.id)
    out = backend_for(ctx).run_index(src.id, lambda p: None, lambda: False)
    assert out.state == "failed" and "unavailable" in out.reason


# ---------------------------------------------------------------------------
# asking
# ---------------------------------------------------------------------------


def _result(question, answer, citations, **kw):
    base = dict(question=question, answer=answer, citations=citations, abstained=False, validation_warnings=[], mode="fast")
    base.update(kw)
    return QueryResult(**base)


def test_ask_success_numbers_citations_and_resolves_evidence(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    cid = add_file(ctx, src, "q3.xlsx", "North 4.2 | South 3.1", locator='{"sheet": "Summary", "range": "B8:F8"}')
    lbl = label_for("q3.xlsx", cid)
    gone = "chk_" + "f" * 64
    gone_lbl = label_for("old.pdf", gone)

    def result_for(q, mode):
        return _result(
            q,
            f"North led {lbl}. A figure {gone_lbl} too.",
            [
                Citation(citation_label=lbl, chunk_id=cid, source_display_name="q3.xlsx"),
                Citation(citation_label=gone_lbl, chunk_id=gone, source_display_name="old.pdf"),
            ],
            validation_warnings=["one claim lacks a citation"],
            standalone_query="Q3 revenue by region",
            ambiguity={"reason": "period", "options": [{"file": "q3.xlsx", "fiscal_year": "FY2025-26"}]},
            compute={"status": "used", "derivation": [{"expression": "4.2+3.1", "result_text": "7.3", "units": "m"}]},
            scoped_to=["q3.xlsx"],
        )

    svc = FakeService(result_for)
    be = backend_for(ctx, svc)
    stages: list[str] = []
    out = be.ask(AskRequest("what was q3?", "auto"), stages.append, "All ready sources", "Auto")
    assert out.error == ""
    a = out.answer
    assert a.text == "North led [1]. A figure [2] too."
    assert out.history_answer.startswith("North led [q3.xlsx #")  # tagged text kept for follow-ups
    assert stages == [be.stage_search, be.stage_citations]
    c1, c2 = a.citations
    assert c1.available and c1.source_name == "q3.xlsx" and c1.passage == "North 4.2 | South 3.1"
    assert c1.location == "Location: q3.xlsx > Summary, range B8:F8"
    assert c1.version_label == "version 1 of 1 (current)" and c1.source == "docs"
    assert c1.indexed.startswith("2026-03-")  # local time of the stored version
    assert not c2.available and "no longer be shown" in c2.reason
    assert a.warnings == ("one claim lacks a citation",)
    assert a.rewrite == 'Follow-up rewritten as: "Q3 revenue by region"'
    assert "FY2025-26 (q3.xlsx)" in a.ambiguity
    assert "4.2+3.1 = 7.3 m" in a.compute
    assert "q3.xlsx" in a.scope_label and a.mode_label == "Quick search"
    assert svc.calls[0]["mode"] is None and svc.calls[0]["history"] == []


def test_ask_passes_history_and_mode_and_reports_abstention(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    add_file(ctx, src, "a.md")
    svc = FakeService(lambda q, m: _result(q, "I cannot answer that from the documents.", [], abstained=True, mode="agent"))
    be = backend_for(ctx, svc)
    out = be.ask(AskRequest("and next year?", "agent", (Turn("first?", "tagged answer [x]"),)), lambda s: None, "All ready sources", "x")
    assert svc.calls[0]["mode"] == QueryMode.AGENT
    assert svc.calls[0]["history"][0].question == "first?" and svc.calls[0]["history"][0].answer == "tagged answer [x]"
    assert out.answer.abstained and out.answer.abstain_reason
    assert out.answer.mode_label == "Investigated with agent"
    assert out.answer.compute == "Deterministic computation was not used."


def test_ask_errors_are_user_presentable(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    add_file(ctx, src, "a.md")
    be = backend_for(ctx, FakeService(error=InferenceUnavailableError("down")))
    out = be.ask(AskRequest("q", "auto"), lambda s: None, "All", "Auto")
    assert out.answer is None and "Ollama" in out.error
    # nothing indexed: no table, and no eligible chunks
    empty = make_context(ctx.settings.data_dir.parent / "other", writer_table=None)
    out = backend_for(empty, FakeService()).ask(AskRequest("q", "auto"), lambda s: None, "All", "Auto")
    assert "Nothing is indexed" in out.error


def test_citation_names_are_disambiguated_only_when_needed(ctx, tmp_path):
    a, b = tmp_path / "x" / "reports", tmp_path / "y" / "reports"
    for d in (a, b):
        d.mkdir(parents=True)
    sa, sb = ctx.source_manager.register_source(a), ctx.source_manager.register_source(b)
    c1 = add_file(ctx, sa, "sum.xlsx")
    c2 = add_file(ctx, sb, "sum.xlsx")
    c3 = add_file(ctx, sa, "other.pdf")
    cits = [Citation(citation_label=label_for(n, c), chunk_id=c, source_display_name=n) for n, c in (("sum.xlsx", c1), ("sum.xlsx", c2), ("other.pdf", c3))]
    svc = FakeService(lambda q, m: _result(q, " ".join(c.citation_label for c in cits), cits))
    out = backend_for(ctx, svc).ask(AskRequest("q", "auto"), lambda s: None, "All", "Auto")
    views = out.answer.citations
    assert [v.source_name for v in views] == ["sum.xlsx", "sum.xlsx", "other.pdf"]  # file name only
    # the same relative path under two sources is told apart; a unique one is left alone
    assert views[0].rel_path != views[1].rel_path and all(v.rel_path.endswith("sum.xlsx") for v in views[:2])
    assert views[2].rel_path == "other.pdf"
    assert {views[0].source, views[1].source} == {"reports (x)", "reports (y)"}


# ---------------------------------------------------------------------------
# headless UI on a stub backend
# ---------------------------------------------------------------------------


class StubBackend(FakeBackend):
    """Fake data, but a non-demo backend with real worker-thread semantics."""

    demo = False
    scope_enforced = False
    can_open_original = False
    intro_notice = "Stub backend ready."

    def __init__(self, *, sources: bool = True, blocked: bool = False) -> None:
        super().__init__(empty=not sources)
        self._blocked = blocked
        self.ask_gate = threading.Event()
        self.index_gate = threading.Event()
        self.ask_started = threading.Event()
        self.ask_calls: list[AskRequest] = []
        self.fail_ask: str = ""
        self.index_total = 3
        self.readiness_calls = 0
        self.index_calls = 0
        self.stop_release = threading.Event()  # lets a stop request finish "the current file"
        self.ask_gate.set()
        self.index_gate.set()
        self.stop_release.set()

    def readiness(self) -> ReadinessReport:
        self.readiness_calls += 1
        if self._blocked:
            return ReadinessReport((ReadinessItem("Ollama", "Not reachable at http://stub", True),), True, "Start Ollama, then check again.", "Checked: still blocked.")
        return ReadinessReport((ReadinessItem("Ollama", "Available"),), False, "", "Checked: all good.")

    def add_source(self, path: str):
        src = self.world.new_source_from_path(path)
        src.status = fd.READY
        src.files = []
        return src

    def run_index(self, source_id, on_progress, should_stop) -> IndexOutcome:
        self.index_calls += 1
        done = 0
        for i in range(1, self.index_total + 1):
            on_progress(IndexProgress(done, self.index_total, f"file-{i}.pdf", indexed=done))
            # hold the file until released; a stop request is honoured between files
            while not self.index_gate.wait(0.01):
                if should_stop():
                    break
            if should_stop():
                self.stop_release.wait(5)
                return IndexOutcome("stopped", IndexProgress(done, self.index_total, "", indexed=done))
            done += 1
            on_progress(IndexProgress(done, self.index_total, f"file-{i}.pdf", indexed=done))
        return IndexOutcome("done", IndexProgress(done, self.index_total, "", indexed=done), notices=("2 files were skipped by ignore rules.",))

    def ask(self, request, on_stage, scope_label, mode_label) -> AskOutcome:
        self.ask_calls.append(request)
        self.ask_started.set()
        on_stage("Searching and writing answer")
        self.ask_gate.wait(5)
        if self.fail_ask:
            return AskOutcome(error=self.fail_ask)
        n = len(self.ask_calls)
        cit = CitationView("budget.xlsx", "finance/budget.xlsx", "Sheet Summary", "Revenue 4.2", version=2, versions=2, indexed="2026-03-04 12:00", chunk_id="chk1", source="Finance")
        gone = CitationView("old.pdf", "old.pdf", None, "", available=False, reason="This passage can no longer be shown.", chunk_id="chk2")
        a = AnswerView(
            f"Answer number {n} [1] and [2].",
            (cit, gone),
            "Quick search",
            1.5,
            scope_label,
            rewrite="",
            ambiguity="",
            warnings=("one warning",),
        )
        return AskOutcome(answer=a, history_answer=f"tagged answer {n}")


def scenario(fn, backend=None, state="chat", cols=120, rows=40, **kw):
    backend = backend or StubBackend()

    async def go():
        async with Harness(state, cols, rows, backend=backend, **kw) as h:
            return await fn(h, backend)

    return run(go, timeout=30)


def test_real_mode_has_no_demo_tag_and_demo_still_has_it():
    async def go(h, b):
        assert "DEMO DATA" not in h.screen_lines()[0]
        assert "Stub backend ready." in h.text()

    scenario(go)

    async def demo(h):
        assert "DEMO DATA" in h.screen_lines()[0]

    async def go2():
        async with Harness("chat", 120, 40) as h:
            await demo(h)

    run(go2, timeout=30)


def test_welcome_shows_async_readiness_and_check_again_reruns_it():
    async def go(h, b):
        await h.wait_for(lambda: not h.ui.readiness.checking)
        assert "Welcome to Docket" in h.text()
        assert "Ollama" in h.text() and "Available" in h.text()
        n = b.readiness_calls
        await h.press("tab")  # Check again
        await h.press("enter")
        await h.wait_for(lambda: b.readiness_calls > n and not h.ui.readiness.checking)
        assert "Checked: all good." in h.text()

    scenario(go, StubBackend(sources=False))


def test_blocked_readiness_is_announced_once_and_shown():
    async def go(h, b):
        await h.wait_for(lambda: h.ui.readiness.blocked)
        assert "Not ready" in h.text()
        assert sum("Not ready" in m.text for m in h.ui.messages) == 1

    scenario(go, StubBackend(blocked=True))


def test_ask_runs_off_thread_gates_sending_and_keeps_the_draft():
    async def go(h, b):
        b.ask_gate.clear()
        await h.type("first question")
        await h.press("enter")
        await h.wait_for(lambda: b.ask_started.is_set())
        assert h.ui.query_op is not None and h.ui.query_op.real
        assert "Searching and writing answer" in h.text()
        # a second send while busy is refused, and the draft stays
        await h.type("second one")
        await h.press("enter")
        assert h.ui.composer.text == "second one"
        assert "Sending is unavailable while answering" in h.text()
        assert len(b.ask_calls) == 1
        b.ask_gate.set()
        await h.wait_for(lambda: h.ui.query_op is None)
        assert "Answer number 1" in h.text()
        assert h.ui.history == [Turn("first question", "tagged answer 1")]
        # follow-up carries the tagged history
        await h.type("and then?")
        await h.press("enter")
        await h.wait_for(lambda: len(h.ui.history) == 2)
        assert b.ask_calls[1].history == (Turn("first question", "tagged answer 1"),)

    scenario(go)


def test_answer_sources_evidence_and_details_come_from_the_result():
    async def go(h, b):
        await h.type("how much")
        await h.press("enter")
        await h.wait_for(lambda: len(h.ui.answers()) == 1)
        text = h.text()
        assert "[1] budget.xlsx" in text and "(unavailable)" in text
        assert "Warning: one warning" in text
        await h.press("f5")
        assert "Evidence [1] of 2" in h.text()
        assert "Revenue 4.2" in h.text() and "version 2 of 2 (current)" in h.text()
        await h.press("o")  # open original is not available in real mode
        assert "not available" in h.text()
        await h.press("esc")
        await h.press("f6")
        assert "Answer details" in h.text() and "Quick search" in h.text()

    scenario(go)


def test_failed_question_reports_error_and_retry_asks_again():
    async def go(h, b):
        b.fail_ask = "Can't reach Ollama. Is it running? (ollama serve)"
        await h.type("q1")
        await h.press("enter")
        await h.wait_for(lambda: h.ui.query_op is None)
        assert "Can't reach Ollama" in h.text()
        assert not h.ui.answers() and h.ui.history == []
        b.fail_ask = ""
        await h.type("/retry")
        await h.press("enter")
        await h.wait_for(lambda: len(h.ui.answers()) == 1)
        assert len(b.ask_calls) == 2 and b.ask_calls[1].question == "q1"

    scenario(go)


def test_cancelling_an_answer_discards_it_when_the_call_returns():
    async def go(h, b):
        b.ask_gate.clear()
        await h.type("slow one")
        await h.press("enter")
        await h.wait_for(lambda: b.ask_started.is_set())
        await h.press("ctrl-c")
        assert "cannot be interrupted" in h.text()
        assert h.ui.busy  # still one expensive operation until the worker returns
        b.ask_gate.set()
        await h.wait_for(lambda: h.ui.query_op is None)
        assert not h.ui.answers() and h.ui.history == []
        assert "Question cancelled" in h.text()

    scenario(go)


def test_clear_resets_conversation_memory_but_not_while_answering():
    async def go(h, b):
        await h.type("one")
        await h.press("enter")
        await h.wait_for(lambda: len(h.ui.history) == 1)
        await h.type("/clear")
        await h.press("enter")
        assert h.ui.history == [] and not h.ui.answers()
        b.ask_gate.clear()
        await h.type("two")
        await h.press("enter")
        await h.wait_for(lambda: b.ask_started.is_set())
        await h.type("/clear")
        await h.press("enter")
        assert "being written" in h.text()
        b.ask_gate.set()
        await h.wait_for(lambda: h.ui.query_op is None)

    scenario(go)


def test_scope_choice_is_refused_honestly_while_scope_is_not_enforced():
    async def go(h, b):
        await h.press("f3")
        await h.press("down")
        await h.press("enter")
        assert h.ui.scope.kind == "all"
        assert "not connected to search yet" in h.text()

    scenario(go)


def test_shell_commands_are_not_sent_as_questions():
    async def go(h, b):
        await h.type("ls -la")
        await h.press("enter")
        assert b.ask_calls == []

    scenario(go)


def test_add_folder_indexes_with_progress_hide_and_completion():
    async def go(h, b):
        b.index_gate.clear()
        await h.press("enter")  # Welcome: Add a folder
        await h.type("/tmp/stub-folder")
        await h.press("enter")
        await h.wait_for(lambda: h.ui.job is not None and h.ui.job.total == 3)
        text = h.text()
        assert "1 of 3" in text and "file-1.pdf" in text
        assert "0 indexed" in text
        assert "Stage:" not in text  # no invented stages for a real run
        await h.press("esc")  # Hide returns to chat; work continues
        assert h.focused == "composer" and h.ui.job.active
        await h.type("x")
        await h.press("enter")  # sending is paused while indexing
        assert "Sending is unavailable while indexing" in h.text()
        b.index_gate.set()
        await h.wait_for(lambda: not h.ui.job.active)
        assert h.ui.job.state == "done" and h.ui.job.indexed == 3
        assert "finished: 3 indexed" in h.text()
        assert "skipped by ignore rules" in h.text()

    scenario(go, StubBackend(sources=False))


def test_stop_indexing_is_cooperative_and_reported():
    async def go(h, b):
        b.index_gate.clear()
        b.stop_release.clear()
        await h.press("enter")  # Welcome: Add a folder
        await h.type("/tmp/stub-folder")
        await h.press("enter")
        await h.wait_for(lambda: h.ui.job is not None and h.ui.job.total == 3)
        await h.press("tab")  # Hide progress -> Stop indexing
        await h.press("enter")
        assert h.ui.job.state == "stopping"
        assert "after the current file finishes" in h.text()
        b.stop_release.set()
        await h.wait_for(lambda: not h.ui.job.active)
        assert h.ui.job.state == "stopped"
        assert "Stopped before finishing" in h.text()
        assert any("stopped after 0 of 3 files" in m.text for m in h.ui.messages)

    scenario(go, StubBackend(sources=False))


def test_worker_exceptions_become_failed_jobs_and_failed_questions_not_crashes():
    class Boom(StubBackend):
        def run_index(self, *a, **k):
            raise RuntimeError("disk exploded")

        def ask(self, *a, **k):
            raise ValueError("bad thing")

    async def go(h, b):
        await h.press("f2")
        await h.press("tab")  # Add folder
        await h.press("right")  # Refresh
        await h.press("enter")
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert h.ui.job.state == "failed" and "disk exploded" in h.ui.job.reason
        await h.press("esc")
        await h.press("esc")
        await h.type("hello there")
        await h.press("enter")
        await h.wait_for(lambda: h.ui.query_op is None)
        assert "bad thing" in h.text()

    scenario(go, Boom())


def test_exit_while_indexing_asks_first_and_requests_a_stop():
    async def go(h, b):
        b.index_gate.clear()
        await h.press("f2")
        await h.press("tab")
        await h.press("right")
        await h.press("enter")
        await h.wait_for(lambda: h.ui.job is not None and h.ui.job.total == 3)
        await h.press("esc")
        await h.press("esc")
        await h.press("ctrl-d")
        assert "Exit while work is running?" in h.text()
        await h.press("esc")  # keep running
        assert h.ui.job.active

    scenario(go)


def test_ui_command_refuses_without_a_tty():
    from typer.testing import CliRunner

    from docket.interfaces.cli.main import app

    result = CliRunner().invoke(app, ["ui"])
    assert result.exit_code == 1
    assert "interactive terminal" in result.output


# ---------------------------------------------------------------------------
# the full path: TuiApp -> RealBackend -> (fake pipeline, fake service, real DB)
# ---------------------------------------------------------------------------


def test_full_wiring_add_folder_index_ask_and_open_evidence(tmp_path, folder):
    plan = [(folder / "a.md", "ingested", None), (folder / "b.md", "failed", "parse error: bad header")]
    ctx = make_context(tmp_path, plan)
    holder: dict = {}

    def pipeline_run(source_id, progress=None):
        # the real pipeline would write rows; the double writes one ready file at the end
        source = ctx.source_manager.get_source(source_id)
        holder["chunk"] = add_file(ctx, source, "a.md", "The policy says hiring is paused.")
        return FakePipeline.run_ingestion_for_source(ctx.pipeline, source_id, progress)

    ctx.pipeline.run_ingestion_for_source = pipeline_run

    def result_for(q, mode):
        cid = holder["chunk"]
        lbl = label_for("a.md", cid)
        return _result(q, f"Hiring is paused {lbl}.", [Citation(citation_label=lbl, chunk_id=cid, source_display_name="a.md")])

    svc = FakeService(result_for)
    be = backend_for(ctx, svc)

    async def go(h):
        assert "DEMO DATA" not in h.screen_lines()[0]
        await h.wait_for(lambda: not h.ui.readiness.checking)
        assert "Welcome to Docket" in h.text()
        await h.press("a")  # Add a folder
        await h.type(str(folder))
        await h.press("enter")
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert h.ui.job.state == "done" and (h.ui.job.indexed, h.ui.job.failed) == (1, 1)
        assert "FAILED" not in h.text() and "parse error: bad header" in h.text()
        await h.press("esc")
        await h.wait_for(lambda: h.ui.world.ready_files() == 1)
        await h.type("is hiring paused?")
        await h.press("enter")
        await h.wait_for(lambda: len(h.ui.answers()) == 1)
        assert "Hiring is paused [1]." in h.text()
        await h.press("f5")
        assert "The policy says hiring is paused." in h.text()
        assert "a.md" in h.text()

    async def wrapper():
        async with Harness("chat", 120, 40, backend=be) as h:
            await go(h)

    run(wrapper, timeout=60)
