"""CP4: ask questions while indexing is running.

Headless TUI scenarios on a stub backend whose indexing worker mimics the real
progress contract (a callback at each file's start and end), plus RealBackend
checks over fakes and a real temporary LanceDB table. Every wait is bounded and
the stub worker has its own deadline, so a regression fails instead of hanging.
No Ollama, no parser, no user data.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from test_tui_real import (
    FakeService,
    StubBackend,
    _result,
    add_file,
    backend_for,
    ctx,  # noqa: F401 -- fixtures
    folder,  # noqa: F401
    make_context,
    scenario,
)

from docket.interfaces.cli.quiet import quiet_ingest, quiet_streams
from docket.interfaces.cli.tui.backend import (
    AnswerView,
    AskOutcome,
    AskRequest,
    IndexOutcome,
    IndexProgress,
)
from docket.interfaces.cli.tui.model import INDEXING_NOTE, coverage_text
from docket.interfaces.cli.tui.yield_gate import YieldGate

WORKER_DEADLINE = 20.0


class OverlapBackend(StubBackend):
    """A stub whose indexing worker reports at each file's start and end (the
    two between-files checkpoints), does its "work" only when the test releases
    a permit, and records what happened in order."""

    def __init__(self, files: int = 3) -> None:
        super().__init__(sources=True)
        self.index_total = files
        self.events: list[str] = []
        self._lock = threading.Lock()
        self.permits = threading.Semaphore(0)
        self.at_work = threading.Event()  # the worker is inside a file, waiting for a permit
        self.abstain = False
        self.ask_delay = 0.0

    def note(self, event: str) -> None:
        with self._lock:
            self.events.append(event)

    def log(self) -> list[str]:
        with self._lock:
            return list(self.events)

    def run_index(self, source_id, on_progress, should_stop) -> IndexOutcome:
        self.index_calls += 1
        deadline = time.monotonic() + WORKER_DEADLINE
        total, done = self.index_total, 0

        def stopped() -> IndexOutcome:
            self.note("stopped")
            return IndexOutcome("stopped", IndexProgress(done, total, "", indexed=done))

        for i in range(1, total + 1):
            on_progress(IndexProgress(done, total, f"f{i}.md", indexed=done))  # checkpoint (start)
            self.note(f"start {i}")
            if should_stop():
                return stopped()
            self.at_work.set()
            while not self.permits.acquire(timeout=0.02):
                if should_stop():
                    return stopped()
                if time.monotonic() > deadline:
                    return IndexOutcome("failed", IndexProgress(done, total), reason="test deadline")
            self.at_work.clear()
            done += 1
            on_progress(IndexProgress(done, total, f"f{i}.md", indexed=done))  # checkpoint (end)
            self.note(f"end {i}")
            if should_stop() and done < total:
                return stopped()
        return IndexOutcome("done", IndexProgress(done, total, "", indexed=done))

    def ask(self, request, on_stage, scope_label, mode_label) -> AskOutcome:
        self.note("ask-start")
        self.ask_calls.append(request)
        self.ask_started.set()
        on_stage("Searching and writing answer")
        self.ask_gate.wait(10)
        if self.ask_delay:
            time.sleep(self.ask_delay)
        self.note("ask-end")
        cov = request.index_coverage
        if self.abstain:
            text = f"I cannot answer that from the documents.\n\n{INDEXING_NOTE} (use /retry)."
            a = AnswerView(text, (), "Quick search", 0.4, scope_label, abstained=True, abstain_reason="no evidence", index_coverage=cov)
        else:
            a = AnswerView(f"Answer to {request.question}", (), "Quick search", 0.4, scope_label, index_coverage=cov)
        return AskOutcome(answer=a, history_answer=a.text)


def _start_index(h) -> None:
    h.ui.start_job(h.ui.world.sources[0], "Refreshing", "", show_overlay=False)


async def _wait_event(h, b, name: str) -> None:
    await h.wait_for(lambda: name in b.log())


# ---------------------------------------------------------------------------
# the controller
# ---------------------------------------------------------------------------


def test_question_during_indexing_pauses_between_files_then_resumes():
    b = OverlapBackend(3)

    async def go(h, b):
        _start_index(h)
        await _wait_event(h, b, "start 1")
        b.permits.release()  # file 1 done
        await _wait_event(h, b, "start 2")
        await h.wait_for(lambda: h.ui.job.indexed == 1)
        b.ask_gate.clear()
        await h.type("what is revenue?")
        await h.press("enter")
        await h.wait_for(b.ask_started.is_set)
        assert h.ui.question_active and h.ui.indexing_active  # two slots, both taken
        b.permits.release()  # file 2's work ends: the worker reaches the checkpoint
        await h.wait_for(lambda: h.ui.job.paused)
        # paused between files: file 2 has not been reported finished, file 3 not started
        assert "end 2" not in b.log() and "start 3" not in b.log()
        text = h.text()
        assert "Refreshing" in text and "paused while answering" in text
        b.ask_gate.set()
        await h.wait_for(lambda: h.ui.query_op is None)
        await _wait_event(h, b, "start 3")
        assert not h.ui.job.paused
        b.permits.release()
        await h.wait_for(lambda: not h.ui.job.active)
        assert h.ui.job.state == "done"
        assert b.log() == ["start 1", "end 1", "start 2", "ask-start", "ask-end", "end 2", "start 3", "end 3"]
        # coverage rode along on the question and shows on the answer
        assert b.ask_calls[0].index_coverage == {"indexed": 1, "total": 3, "indexing": True}
        assert "1 of 3 documents indexed when answered" in h.text()
        await h.press("f6")
        assert "Index coverage" in h.text()

    scenario(go, b)


def test_second_question_is_refused_while_one_is_active_and_draft_kept():
    b = OverlapBackend(2)

    async def go(h, b):
        _start_index(h)
        await _wait_event(h, b, "start 1")
        b.ask_gate.clear()
        await h.type("first")
        await h.press("enter")
        await h.wait_for(b.ask_started.is_set)
        await h.type("second")
        await h.press("enter")
        assert len(b.ask_calls) == 1
        assert h.ui.composer.text == "second"
        assert "already being answered" in h.text()
        assert h.ui.indexing_active  # indexing is untouched by the refusal
        b.ask_gate.set()
        await h.wait_for(lambda: h.ui.query_op is None)
        b.permits.release()
        b.permits.release()
        await h.wait_for(lambda: not h.ui.job.active)

    scenario(go, b)


def test_stop_still_works_while_indexing_is_paused_for_an_answer():
    b = OverlapBackend(3)

    async def go(h, b):
        _start_index(h)
        await _wait_event(h, b, "start 1")
        b.ask_gate.clear()
        await h.type("slow question")
        await h.press("enter")
        await h.wait_for(b.ask_started.is_set)
        b.permits.release()
        await h.wait_for(lambda: h.ui.job.paused)
        await h.press("ctrl-c")  # first: the question is in front
        assert h.ui.query_op is not None and h.ui.query_op.state == "stopping"
        assert h.ui.job.paused
        await h.press("ctrl-c")  # second: stops indexing, although the worker is waiting
        await h.wait_for(lambda: not h.ui.job.active)
        assert h.ui.job.state == "stopped"
        assert b.log()[-1] == "stopped" and "start 2" not in b.log()  # no further file began
        assert h.ui.query_op is not None  # the abandoned answer is still being waited for
        b.ask_gate.set()
        await h.wait_for(lambda: h.ui.query_op is None)

    scenario(go, b)


def test_job_start_is_refused_while_a_question_runs_and_nothing_is_registered():
    b = OverlapBackend(2)

    async def go(h, b):
        before = len(h.ui.world.sources)
        b.ask_gate.clear()
        await h.type("q")
        await h.press("enter")
        await h.wait_for(b.ask_started.is_set)
        _start_index(h)
        assert h.ui.job is None and b.index_calls == 0
        assert any("A question is being answered" in m.text for m in h.ui.messages)
        h.ui.add_folder("/tmp/never-registered")
        assert len(h.ui.world.sources) == before
        b.ask_gate.set()
        await h.wait_for(lambda: h.ui.query_op is None)
        _start_index(h)  # allowed again once the answer is in
        await h.wait_for(lambda: h.ui.job is not None and h.ui.job.active)
        b.permits.release()
        b.permits.release()
        await h.wait_for(lambda: not h.ui.job.active)

    scenario(go, b)


def test_abstention_during_indexing_explains_and_retry_hint_appears_when_done():
    b = OverlapBackend(2)
    b.abstain = True

    async def go(h, b):
        _start_index(h)
        await _wait_event(h, b, "start 1")
        await h.type("where is the policy?")
        await h.press("enter")
        await h.wait_for(lambda: len(h.ui.answers()) == 1)
        assert "still being indexed" in h.text() and "No answer" in h.text()
        b.permits.release()
        b.permits.release()
        await h.wait_for(lambda: not h.ui.job.active)
        assert any("use /retry" in m.text and "Indexing finished" in m.text for m in h.ui.messages)

    scenario(go, b)


def test_interleaved_questions_and_files_never_hang_or_reorder():
    """3 files, 3 questions, each landing while a file is mid-work."""
    b = OverlapBackend(3)
    b.ask_delay = 0.05

    async def go(h, b):
        _start_index(h)
        for round_no in range(1, 4):
            await h.wait_for(b.at_work.is_set)
            await h.type(f"question {round_no}")
            await h.press("enter")
            await h.wait_for(b.ask_started.is_set)
            b.ask_started.clear()
            b.permits.release()  # the file finishes while the answer is being written
            await h.wait_for(lambda n=round_no: len(h.ui.answers()) == n)
            await h.wait_for(lambda n=round_no: f"end {n}" in b.log())
        await h.wait_for(lambda: not h.ui.job.active)
        assert h.ui.job.state == "done" and h.ui.job.indexed == 3
        expected = []
        for i in (1, 2, 3):
            expected += [f"start {i}", "ask-start", "ask-end", f"end {i}"]
        assert b.log() == expected  # a file never ends while its question is open
        assert len(b.ask_calls) == 3 and not h.ui._gate.held

    scenario(go, b)


# ---------------------------------------------------------------------------
# small pieces
# ---------------------------------------------------------------------------


def test_yield_gate_waits_until_released_and_stop_wins():
    gate = YieldGate()
    stop = threading.Event()
    assert gate.wait_clear(stop.is_set) is False  # nothing to wait for

    gate.hold()
    waited: list[bool] = []
    t = threading.Thread(target=lambda: waited.append(gate.wait_clear(stop.is_set)), daemon=True)
    t.start()
    t.join(0.2)
    assert t.is_alive()  # parked
    gate.release()
    t.join(2)
    assert waited == [True] and not t.is_alive()

    gate.hold()
    t = threading.Thread(target=lambda: waited.append(gate.wait_clear(stop.is_set)), daemon=True)
    t.start()
    stop.set()
    gate.poke()
    t.join(2)
    assert not t.is_alive() and gate.held  # woken by stop while the question is still open
    gate.release_all()
    assert not gate.held


def test_coverage_text_variants():
    assert coverage_text({"indexed": 31, "total": 47, "indexing": True}) == "31 of 47 documents indexed when answered"
    assert coverage_text({"indexed": 1, "total": 1, "indexing": True}) == "1 of 1 document indexed when answered"
    assert "still being indexed" in coverage_text({"indexed": 0, "total": 0, "indexing": True})
    assert coverage_text(None) == "" and coverage_text({"indexed": 3, "total": 3, "indexing": False}) == ""


def test_quiet_ingest_overlapping_threads_restore_once_and_only_at_the_end(monkeypatch):
    monkeypatch.delenv("DOCKET_DEBUG", raising=False)
    noisy = logging.getLogger("transformers")
    noisy.setLevel(logging.INFO)
    entered, leave = [threading.Event(), threading.Event()], [threading.Event(), threading.Event()]

    def worker(n: int) -> None:
        with quiet_ingest():
            entered[n].set()
            leave[n].wait(5)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in (0, 1)]
    threads[0].start()
    assert entered[0].wait(2)
    threads[1].start()
    assert entered[1].wait(2)
    assert noisy.level == logging.WARNING
    leave[0].set()
    threads[0].join(2)
    assert noisy.level == logging.WARNING  # the second scope is still inside
    leave[1].set()
    threads[1].join(2)
    assert noisy.level == logging.INFO


def test_quiet_streams_swallow_library_output_and_restore(capsys, monkeypatch):
    monkeypatch.delenv("DOCKET_DEBUG", raising=False)
    handler = logging.StreamHandler(sys.stderr)
    log = logging.getLogger("cp4.noise")
    log.addHandler(handler)
    log.propagate = False
    try:
        before = (sys.stdout, sys.stderr)
        with quiet_streams():
            print("tqdm 10%|#   |")
            sys.stderr.write("Preset 'granite_vision_v4' already registered\n")
            log.warning("HF Hub unauthenticated warning")
            assert handler.stream is not before[1]
        assert (sys.stdout, sys.stderr) == before and handler.stream is before[1]
        out = capsys.readouterr()
        assert "tqdm" not in out.out and "granite" not in out.err and "unauthenticated" not in out.err
        print("visible again")
        assert "visible again" in capsys.readouterr().out
    finally:
        log.removeHandler(handler)


def test_quiet_streams_with_debug_write_to_the_log_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOCKET_DEBUG", "1")
    path = tmp_path / "ui-debug.log"
    with quiet_streams(path):
        sys.stderr.write("debug line\n")
    assert "debug line" in path.read_text()
    assert "debug line" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# RealBackend: coverage, abstention note, table reopen, session isolation
# ---------------------------------------------------------------------------


COVERAGE = {"indexed": 31, "total": 47, "indexing": True}


def test_real_backend_records_coverage_and_marks_abstention(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    add_file(ctx, src, "a.md")
    seen: list = []

    def abstain(q, mode):
        r = _result(q, "I cannot answer that from the documents.", [], abstained=True)
        seen.append(r)
        return r

    out = backend_for(ctx, FakeService(abstain)).ask(
        AskRequest("q", "auto", index_coverage=COVERAGE), lambda s: None, "All ready sources", "Auto"
    )
    assert out.answer.abstained and out.answer.index_coverage == COVERAGE
    assert INDEXING_NOTE in out.answer.text and "/retry" in out.answer.text
    assert INDEXING_NOTE in out.answer.abstain_reason
    assert seen[0].index_coverage == COVERAGE  # on the QueryResult too (eval records it)
    assert out.history_answer == "I cannot answer that from the documents."  # memory stays raw

    ok = backend_for(ctx, FakeService(lambda q, m: _result(q, "Fine.", []))).ask(
        AskRequest("q", "auto", index_coverage=COVERAGE), lambda s: None, "All", "Auto"
    )
    assert ok.answer.index_coverage == COVERAGE and INDEXING_NOTE not in ok.answer.text

    plain = backend_for(ctx, FakeService(lambda q, m: _result(q, "Fine.", []))).ask(
        AskRequest("q", "auto"), lambda s: None, "All", "Auto"
    )
    assert plain.answer.index_coverage is None


def test_real_backend_nothing_searchable_yet_while_the_first_file_indexes(tmp_path):
    empty = make_context(tmp_path / "other", writer_table=None)
    out = backend_for(empty, FakeService()).ask(
        AskRequest("q", "auto", index_coverage={"indexed": 0, "total": 5, "indexing": True}), lambda s: None, "All", "Auto"
    )
    assert out.answer is None and "still being indexed" in out.error


def test_run_record_carries_index_coverage_optionally():
    from docket.eval.schema import RunRecord

    assert RunRecord(question_id="q", repeat=0).index_coverage is None
    rec = RunRecord(question_id="q", repeat=0, index_coverage=COVERAGE)
    assert RunRecord.model_validate_json(rec.model_dump_json()).index_coverage == COVERAGE
    from docket.services.query.service import QueryResult

    r = _result("q", "a", [])
    assert isinstance(r, QueryResult) and r.index_coverage is None


def test_each_question_reopens_the_vector_table_and_sees_concurrent_writes(tmp_path):
    from docket.infra.index.base import ChunkRecord
    from docket.infra.index.vector_index import LanceIndexWriter

    def rec(n: int) -> ChunkRecord:
        return ChunkRecord(
            chunk_id=f"chk_{n}", source_id="src_1", evidence_version_id=f"ev_{n}", evidence_unit_id="eu_1",
            chunk_recipe_id="rcp_1", ordinal=0, heading=None, text=f"text {n}", content_hash=f"h{n}",
        )

    writer = LanceIndexWriter(tmp_path / "lancedb", table_name="chunks")
    writer.upsert([rec(0)], embeddings=[[0.1, 0.2, 0.3]])
    ctx = make_context(tmp_path / "ctx")
    ctx.__dict__["vector_writer"] = writer
    src = ctx.source_manager.register_source(tmp_path / "ctx")
    add_file(ctx, src, "a.md")  # SQLite eligibility so the backend proceeds

    counts: list[int] = []

    class Spy:
        def __init__(self, table) -> None:
            self.table = table

        def ask(self, question, mode=None, history=None):
            counts.append(self.table.count_rows())
            return _result(question, "ok", [])

    be = backend_for(ctx)
    be._factory = lambda c, table: Spy(table)

    def ask() -> None:
        out = be.ask(AskRequest("q", "auto", index_coverage=COVERAGE), lambda s: None, "All", "Auto")
        assert out.error == "" and out.answer is not None

    ask()
    errors: list[BaseException] = []

    def write_more() -> None:
        try:
            for n in range(1, 6):
                writer.upsert([rec(n)], embeddings=[[0.1 * n, 0.2, 0.3]])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=write_more, daemon=True)
    t.start()
    while t.is_alive():  # questions interleave with the writes
        ask()
    t.join(5)
    assert not errors
    ask()
    assert counts[0] == 1
    assert counts == sorted(counts), "a later question must never see fewer chunks"
    assert counts[-1] == 6, "the question after the writes sees every committed chunk"


def test_worker_threads_use_their_own_database_sessions(ctx, folder):
    src = ctx.source_manager.register_source(folder)
    add_file(ctx, src, "a.md")
    # Retain the actual objects, then compare identity. Object ids may be
    # reused after collection, so numeric ids cannot prove session sharing.
    opened: list[tuple[int, object]] = []
    lock = threading.Lock()
    real = ctx.session_factory

    def spy():
        s = real()
        with lock:
            opened.append((threading.get_ident(), s))
        return s

    ctx.session_factory = spy
    be = backend_for(ctx, FakeService(lambda q, m: _result(q, "ok", [])))
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for n in range(5):
                add_file(ctx, src, f"w{n}.md", content_hash=f"{n:064d}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    for _ in range(5):
        be.load_world()
        assert be.ask(AskRequest("q", "auto"), lambda s: None, "All", "Auto").answer is not None
    t.join(10)
    assert not errors and not t.is_alive()
    by_thread: dict[int, list[object]] = {}
    for tid, session in opened:
        by_thread.setdefault(tid, []).append(session)
    assert len(by_thread) == 2  # the writer thread and this one
    a, b = by_thread.values()
    assert all(left is not right for left in a for right in b), "no session object is shared across threads"
