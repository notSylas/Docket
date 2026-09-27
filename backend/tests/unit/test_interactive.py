import io
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from docket.cli.context import AppContext
from docket.cli.interactive import run_session
from docket.cli.interactive.state import SessionState
from docket.cli.main import app
from docket.infra.inference.gateway import InferenceUnavailableError
from docket.ingestion.pipeline import FileIngestResult, IngestionJobResult, SourceNotFoundError
from docket.query.classifier import QueryMode
from docket.query.service import Citation, QueryResult
from docket.infra.retrieval.resolver import ChunkNotFoundError, ResolvedEvidence


class FakeService:
    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self.error = error

    def ask(self, question, mode=None, history=None):
        self.calls.append({"question": question, "mode": mode, "history": list(history or [])})
        if self.error:
            raise self.error
        return QueryResult(
            question=question,
            answer=f"answer to {question}",
            citations=[Citation(citation_label="[doc.md#1]", chunk_id="c1", source_display_name="doc.md")],
            abstained=False,
            validation_warnings=["some warning"],
            mode=(mode or QueryMode.FAST).value,
        )


class FakeWriter:
    def __init__(self, table="TABLE"):
        self.table = table


class FakePipeline:
    def __init__(self, writer):
        self.writer = writer

    def run_ingestion_for_source(self, source_id, progress=None):
        if source_id == "bogus":
            raise SourceNotFoundError(source_id)
        self.writer.table = "TABLE"
        return IngestionJobResult(
            source_id=source_id,
            job_id="job1",
            status="succeeded",
            files_processed=1,
            files_failed=0,
            file_results=[FileIngestResult(path="a.md", status="ingested", chunks_written=3)],
        )


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "data"))
    context = AppContext()
    writer = FakeWriter()
    context.__dict__["vector_writer"] = writer
    context.__dict__["pipeline"] = FakePipeline(writer)
    return context


def drive(ctx, lines, service=None, table=True, state=None, **kw):
    service = service or FakeService()
    if not table:
        ctx.vector_writer.table = None
    it = iter(lines)

    def input_fn(prompt):
        try:
            item = next(it)
        except StopIteration:
            raise EOFError
        if isinstance(item, BaseException):
            raise item
        return item

    out = io.StringIO()
    console = Console(file=out, width=120, force_terminal=False)
    run_session(ctx, input_fn=input_fn, console=console, query_service_factory=lambda c, t: service, state=state, **kw)
    return out.getvalue(), service


def test_banner_and_onboarding(ctx):
    out, _ = drive(ctx, [])
    assert "docket" in out and "qwen3" in out
    assert "/help" in out and "/exit" in out
    assert "/add <folder>" in out


def test_no_onboarding_with_sources(ctx, tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    ctx.source_manager.register_source(folder)
    out, _ = drive(ctx, [])
    assert "1 source(s)" in out
    assert "No sources yet" not in out


def test_question_history_and_output(ctx):
    out, svc = drive(ctx, ["first?", "second?"])
    assert svc.calls[0]["question"] == "first?"
    assert svc.calls[0]["history"] == []
    assert svc.calls[1]["history"][0].question == "first?"
    assert svc.calls[1]["history"][0].answer == "answer to first?"
    assert "answer to first?" in out
    assert "Sources:" in out and "[1] doc.md" in out  # was: "Citations:" + raw label
    assert "some warning" in out
    assert "quick search" in out
    assert "[fast]" not in out


def test_clear_empties_history(ctx):
    _, svc = drive(ctx, ["a", "/clear", "b"])
    assert svc.calls[1]["history"] == []


def test_mode_switching(ctx):
    out, svc = drive(ctx, ["/mode", "/mode fast", "q1", "/mode agent", "q2", "/mode auto", "q3", "/mode bogus"])
    assert "Mode: auto" in out
    assert [c["mode"] for c in svc.calls] == [QueryMode.FAST, QueryMode.AGENT, None]
    assert "Usage: /mode" in out


def test_sources_listing(ctx, tmp_path):
    out, _ = drive(ctx, ["/sources"])
    assert "No sources registered" in out
    folder = tmp_path / "f"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)
    out, _ = drive(ctx, ["/sources"])
    assert src.id in out and "active" in out


def test_add_good_and_bad(ctx, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    out, _ = drive(ctx, [f"/add {folder}", "/add /definitely/not/here", "/add", "/sources"])
    assert "Registered source src" in out
    assert "/ingest src" in out
    assert "Error:" in out
    assert "Usage: /add" in out
    assert len(ctx.source_manager.list_sources()) == 1


def test_ingest_paths(ctx, tmp_path):
    out, _ = drive(ctx, ["/ingest"])
    assert "No active sources" in out
    folder = tmp_path / "docs"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)
    out, _ = drive(ctx, ["/ingest", "/ingest all", f"/ingest {src.id}", "/ingest bogus"])
    assert out.count("0 unchanged, 0 failed \u2014 3 chunks written") == 3  # was: chunks_written=3
    assert f"{src.id}: 1 ingested" in out  # was: files_processed=1
    assert "Error ingesting bogus" in out


def test_question_without_table(ctx):
    out, svc = drive(ctx, ["hello?"], table=False)
    assert "Nothing indexed yet" in out
    assert svc.calls == []


def test_ingest_then_question_same_session(ctx, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)
    out, svc = drive(ctx, ["q0", f"/ingest {src.id}", "q1"], table=False)
    assert "Nothing indexed yet" in out
    assert [c["question"] for c in svc.calls] == ["q1"]


def test_inference_error_not_appended(ctx):
    svc = FakeService(error=InferenceUnavailableError("Can't reach Ollama"))
    out, _ = drive(ctx, ["q1"], service=svc)
    assert "Can't reach Ollama" in out
    svc.error = None
    drive_out, _ = drive(ctx, ["q2"], service=svc)
    assert svc.calls[-1]["history"] == []


def test_error_then_continue_history(ctx):
    svc = FakeService(error=InferenceUnavailableError("down"))
    lines = iter(["q1", "q2", "q3"])

    def input_fn(prompt):
        try:
            line = next(lines)
        except StopIteration:
            raise EOFError
        if line == "q2":
            svc.error = None
        return line

    out = io.StringIO()
    run_session(ctx, input_fn=input_fn, console=Console(file=out, width=120),
                query_service_factory=lambda c, t: svc)
    assert [c["question"] for c in svc.calls] == ["q1", "q2", "q3"]
    assert [t.question for t in svc.calls[2]["history"]] == ["q2"]


def test_generic_error_survives(ctx):
    out, svc = drive(ctx, ["q1", "/help"], service=FakeService(error=RuntimeError("boom")))
    assert "boom" in out and "/exit" in out


def test_unknown_command_and_blank(ctx):
    out, svc = drive(ctx, ["", "   ", "/frobnicate"])
    assert "Unknown command: /frobnicate" in out and "/help" in out
    assert svc.calls == []


def test_help_mentions_watch(ctx):
    out, _ = drive(ctx, ["/help"])
    assert "docket watch" in out


@pytest.mark.parametrize("cmd", ["/exit", "/quit"])
def test_exit_commands_end_loop(ctx, cmd):
    _, svc = drive(ctx, [cmd, "never asked"])
    assert svc.calls == []


def test_eof_ends(ctx):
    drive(ctx, [])


def test_keyboard_interrupt_at_prompt_continues(ctx):
    out, svc = drive(ctx, [KeyboardInterrupt(), "q"])
    assert "/exit" in out
    assert len(svc.calls) == 1


def test_keyboard_interrupt_during_query(ctx):
    svc = FakeService(error=KeyboardInterrupt())
    out, _ = drive(ctx, ["q1"], service=svc)
    assert "interrupted" in out


# -- CLI-level -----------------------------------------------------------------

runner = CliRunner()


def test_cli_version_and_help():
    assert runner.invoke(app, ["--version"]).exit_code == 0
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0 and "chat" in result.stdout


def test_bare_docket_non_tty_prints_help(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "d"))
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_chat_command_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "d"))
    result = runner.invoke(app, ["chat"], input="/exit\n")
    assert result.exit_code == 0
    assert "docket" in result.stdout


# -- command registry dispatch (CP0) ----------------------------------------


@pytest.mark.parametrize("cmd", ["/h", "/?", "help", "?", "HELP"])
def test_help_variants(ctx, cmd):
    out, _ = drive(ctx, [cmd])
    assert "Commands:" in out and "docket watch" in out


@pytest.mark.parametrize("cmd", ["/q", "exit", "quit", "/e"])
def test_exit_variants(ctx, cmd):
    _, svc = drive(ctx, [cmd, "never asked"])
    assert svc.calls == []


def test_prefix_and_alias_dispatch(ctx):
    out, _ = drive(ctx, ["/ls", "/sou", "/ing"])
    assert "No sources registered" in out
    assert "No active sources to ingest" in out


def test_bare_help_phrase_is_question(ctx):
    _, svc = drive(ctx, ["help me with x"])
    assert [c["question"] for c in svc.calls] == ["help me with x"]


def test_unknown_command_suggests(ctx):
    out, _ = drive(ctx, ["/hlep"])
    assert "Unknown command: /hlep." in out
    assert "Did you mean /help?" in out and "/help for the list" in out


def test_add_dot_is_absolute_and_duplicate_detected(ctx, tmp_path, monkeypatch):
    folder = tmp_path / "docs"
    folder.mkdir()
    monkeypatch.chdir(folder)
    out, _ = drive(ctx, ["/add ."])
    assert "Registered source" in out
    sources = ctx.source_manager.list_sources()
    assert len(sources) == 1 and sources[0].path == str(folder.resolve())
    out, _ = drive(ctx, [f"/add {folder}"])
    assert f"Already registered: {sources[0].id}" in out
    assert len(ctx.source_manager.list_sources()) == 1


def test_state_turns_and_clear(ctx):
    from docket.cli.interactive.state import SessionState

    st = SessionState()
    drive(ctx, ["a", "b"], state=st)
    assert st.turns == 2 and st.indexed is True
    st = SessionState()
    drive(ctx, ["a", "b", "/clear"], state=st)
    assert st.turns == 0


def test_state_errored_turn_not_counted(ctx):
    from docket.cli.interactive.state import SessionState

    st = SessionState()
    drive(ctx, ["q1"], service=FakeService(error=InferenceUnavailableError("down")), state=st)
    assert st.turns == 0


def test_state_mode_follows_command(ctx):
    from docket.cli.interactive.state import SessionState

    st = SessionState()
    drive(ctx, ["/mode agent"], state=st)
    assert st.mode == "agent"
    drive(ctx, ["/mode agent", "/mode auto"], state=st)
    assert st.mode == "auto"


def test_state_indexed_flips_after_ingest(ctx, tmp_path):
    from docket.cli.interactive.state import SessionState

    folder = tmp_path / "docs"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)
    st = SessionState()
    drive(ctx, [], table=False, state=st)
    assert st.indexed is False and len(st.sources) == 1
    drive(ctx, [f"/ingest {src.id}"], table=False, state=st)
    assert st.indexed is True


def test_footer_timing_and_agent(ctx, monkeypatch):
    import itertools

    from docket.cli.interactive import query_flow as query_flow_mod

    ticks = itertools.count(0, 8)  # each perf_counter call advances 8s -> ask takes 8s
    monkeypatch.setattr(query_flow_mod.time, "perf_counter", lambda: next(ticks))
    out, _ = drive(ctx, ["q"])
    assert "quick search \u00b7 8.0s" in out
    out, _ = drive(ctx, ["/mode agent", "q"])
    assert "investigated with agent \u00b7 8.0s" in out


def test_footer_no_elapsed_or_tiny():
    from docket.cli.interactive.render import footer_text

    assert footer_text("fast") == "quick search"
    assert footer_text("agent", 0.01) == "investigated with agent"
    assert footer_text("agent", 14.14) == "investigated with agent \u00b7 14.1s"


# ---- CP3: numbered citations, /show, /retry, /status, /remove ----------------

TAG_A = "[a.docx #chk_1111aaaa]"
TAG_B = "[b.docx #chk_2222bbbb]"


class TaggedService(FakeService):
    def ask(self, question, mode=None, history=None):
        self.calls.append({"question": question, "mode": mode, "history": list(history or [])})
        return QueryResult(
            question=question,
            answer=f"Second fact {TAG_B}. First fact {TAG_A}. Again {TAG_B}.",
            citations=[
                Citation(citation_label=TAG_A, chunk_id="c_a", source_display_name="a.docx"),
                Citation(citation_label=TAG_B, chunk_id="c_b", source_display_name="b.docx"),
            ],
            abstained=False,
            validation_warnings=[],
            mode="fast",
        )


class FakeResolver:
    def resolve(self, chunk_id):
        if chunk_id == "c_b":
            raise ChunkNotFoundError(chunk_id)
        return ResolvedEvidence(
            chunk_id=chunk_id,
            text="The evidence body text.",
            source_display_name="a.docx",
            evidence_version_id="v1",
            heading="Overview",
            citation_label=TAG_A,
        )


@pytest.fixture
def tctx(ctx):
    ctx.__dict__["resolver"] = FakeResolver()
    return ctx


def test_answer_is_numbered_and_history_keeps_original(tctx):
    out, svc = drive(tctx, ["q1", "q2"], service=TaggedService())
    assert "#chk_" not in out
    assert "Second fact [1]. First fact [2]. Again [1]." in out
    assert "Sources:" in out and "[1] b.docx" in out and "[2] a.docx" in out
    assert "/show <n> to read the evidence" in out
    assert svc.calls[1]["history"][0].answer == (
        f"Second fact {TAG_B}. First fact {TAG_A}. Again {TAG_B}."
    )


def test_show_prints_evidence_and_errors(tctx):
    out, _ = drive(tctx, ["/show 1"], service=TaggedService())
    assert "No answer yet" in out
    out, _ = drive(tctx, ["q", "/show 2", "/show 9", "/show abc", "/show 1"], service=TaggedService())
    assert "[2] a.docx \u2014 Overview" in out
    assert "The evidence body text." in out
    assert "chunk c_a" in out
    assert "No citation 9 in the last answer (it has 2)." in out
    assert "Usage: /show <n>" in out
    assert "no longer available" in out  # citation 1 is c_b


def test_clear_resets_citations(tctx):
    state = SessionState()
    out, _ = drive(tctx, ["q", "/clear", "/show 1"], service=TaggedService(), state=state)
    assert "No answer yet" in out
    assert state.citations == ()


def test_state_citations_snapshot(tctx):
    state = SessionState()
    drive(tctx, ["q"], service=TaggedService(), state=state)
    assert state.citations == ((1, "b.docx"), (2, "a.docx"))


def test_retry(tctx):
    out, svc = drive(tctx, ["/retry", "q1", "/mode agent", "/retry"], service=TaggedService())
    assert "Nothing to retry yet." in out
    assert [c["question"] for c in svc.calls] == ["q1", "q1"]
    assert svc.calls[1]["history"] == []  # stale turn popped
    assert svc.calls[1]["mode"] == QueryMode.AGENT


def test_status(tctx):
    out, _ = drive(tctx, ["/mode fast", "/status"])
    assert str(tctx.settings.data_dir) in out
    assert tctx.settings.gen_model in out and tctx.settings.embed_model in out
    assert "fast" in out and "0 active / 0 total" in out


def _src(tctx, tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    return tctx.source_manager.register_source(folder)


@pytest.mark.parametrize("reply,removed", [("y", True), ("YES", True), ("n", False), ("", False), (EOFError(), False)])
def test_remove_confirmation(tctx, tmp_path, reply, removed):
    src = _src(tctx, tmp_path)
    out, _ = drive(tctx, [f"/remove {src.id}", reply])
    status = tctx.source_manager.list_sources()[0].status.value
    assert (status == "revoked") == removed
    assert ("Removed" in out) == removed


def test_remove_unknown_and_usage(tctx):
    out, _ = drive(tctx, ["/remove nope", "/remove"])
    assert "source not found: nope" in out
    assert "Usage: /remove" in out


def test_sources_table_after_remove(tctx, tmp_path):
    src = _src(tctx, tmp_path)
    out, _ = drive(tctx, [f"/remove {src.id}", "y", "/sources"])
    assert "ID" in out and "Status" in out and "Path" in out
    assert "revoked" in out


# -- CP4: progress, health, first run ---------------------------------------

from pathlib import Path  # noqa: E402

from docket.infra.inference.gateway import ModelNotFoundError  # noqa: E402
from docket.infra.inference.health import HealthReport  # noqa: E402


def test_banner_shows_health_warnings(ctx):
    bad = HealthReport(reachable=False, error="refused", host="http://h:1")
    out, _ = drive(ctx, [], health_check=lambda: bad)
    assert "Can't reach Ollama at http://h:1" in out and "ollama serve" in out
    missing = HealthReport(reachable=True, missing_models=["qwen3:14b"], host="h")
    out, _ = drive(ctx, [], health_check=lambda: missing)
    assert "Model not found: qwen3:14b" in out and "ollama pull qwen3:14b" in out
    for check in (None, lambda: HealthReport(reachable=True, host="h")):
        out, _ = drive(ctx, [], health_check=check)
        assert "Can't reach" not in out and "Model not found" not in out


def test_health_check_exception_does_not_break_banner(ctx):
    def boom():
        raise RuntimeError("x")

    out, _ = drive(ctx, [], health_check=boom)
    assert "docket" in out


def test_ingest_shows_failures_and_summary(ctx, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)

    class P:
        def run_ingestion_for_source(self, source_id, progress=None):
            from docket.ingestion.pipeline import ProgressEvent

            ok = FileIngestResult(path=Path("a.pdf"), status="ingested", chunks_written=5)
            bad = FileIngestResult(path=Path("b.pdf"), status="failed", error="corrupt")
            same = FileIngestResult(path=Path("c.pdf"), status="unchanged")
            files = [ok, bad, same]
            for i, r in enumerate(files, 1):
                progress(ProgressEvent("start", i, 3, r.path))
                progress(ProgressEvent("done", i, 3, r.path, r))
            return IngestionJobResult(source_id, "j", "partial", 3, 1, files)

    ctx.__dict__["pipeline"] = P()
    out, _ = drive(ctx, [f"/ingest {src.id}"])
    assert f"{src.id}: 1 ingested, 1 unchanged, 1 failed — 5 chunks written" in out
    assert "FAILED: b.pdf -- corrupt" in out


def test_ingest_empty_source_friendly(ctx, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)

    class P:
        def run_ingestion_for_source(self, source_id, progress=None):
            return IngestionJobResult(source_id, "j", "failed", 0, 0, [])

    ctx.__dict__["pipeline"] = P()
    out, _ = drive(ctx, [f"/ingest {src.id}"])
    assert "No supported files found in" in out
    assert str(folder) in out and ".docx" in out and ".pdf" in out


def test_friendly_inference_errors(ctx):
    out, _ = drive(ctx, ["q"], service=FakeService(error=InferenceUnavailableError("conn refused")))
    assert "Can't reach Ollama" in out and "ollama serve" in out
    err = ModelNotFoundError("Model 'foo:1b' is not available on Ollama")
    out, _ = drive(ctx, ["q"], service=FakeService(error=err))
    assert "Model not found: foo:1b" in out and "ollama pull foo:1b" in out


def _first_run(ctx, monkeypatch, tmp_path, answers, cwd=None, **kw):
    work = cwd or (tmp_path / "work")
    work.mkdir(exist_ok=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return drive(ctx, answers, offer_first_run=True, **kw)


def test_first_run_yes_yes_registers_and_ingests(ctx, monkeypatch, tmp_path):
    out, _ = _first_run(ctx, monkeypatch, tmp_path, ["y", "y"])
    sources = ctx.source_manager.list_sources()
    assert len(sources) == 1
    assert "1 ingested" in out


def test_first_run_yes_then_no_ingest(ctx, monkeypatch, tmp_path):
    out, _ = _first_run(ctx, monkeypatch, tmp_path, ["y", "n"])
    assert len(ctx.source_manager.list_sources()) == 1
    assert "ingested," not in out


def test_first_run_no_and_eof(ctx, monkeypatch, tmp_path):
    _first_run(ctx, monkeypatch, tmp_path, ["n"])
    assert ctx.source_manager.list_sources() == []
    _first_run(ctx, monkeypatch, tmp_path, [])
    assert ctx.source_manager.list_sources() == []
    _first_run(ctx, monkeypatch, tmp_path, [KeyboardInterrupt()])
    assert ctx.source_manager.list_sources() == []


def test_first_run_skipped_when_sources_exist(ctx, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    ctx.source_manager.register_source(other)
    _first_run(ctx, monkeypatch, tmp_path, ["y"])
    assert len(ctx.source_manager.list_sources()) == 1


def test_first_run_skipped_in_home_and_root(ctx, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _first_run(ctx, monkeypatch, tmp_path, ["y"], cwd=home)
    assert ctx.source_manager.list_sources() == []
    monkeypatch.chdir("/")
    drive(ctx, ["y"], offer_first_run=True)
    assert ctx.source_manager.list_sources() == []


def test_first_run_off_by_default(ctx, monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    drive(ctx, ["y"])
    assert ctx.source_manager.list_sources() == []
