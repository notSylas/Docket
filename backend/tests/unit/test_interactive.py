import io
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from docket.cli.context import AppContext
from docket.cli.interactive import run_session
from docket.cli.main import app
from docket.inference.gateway import InferenceUnavailableError
from docket.ingestion.pipeline import FileIngestResult, IngestionJobResult, SourceNotFoundError
from docket.query.classifier import QueryMode
from docket.query.service import Citation, QueryResult


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

    def run_ingestion_for_source(self, source_id):
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


def drive(ctx, lines, service=None, table=True, state=None):
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
    run_session(ctx, input_fn=input_fn, console=console, query_service_factory=lambda c, t: service, state=state)
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
    assert "Citations:" in out and "[doc.md#1]" in out
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
    assert out.count("chunks_written=3") == 3
    assert "files_processed=1" in out
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

    from docket.cli.interactive import session as session_mod

    ticks = itertools.count(0, 8)  # each perf_counter call advances 8s -> ask takes 8s
    monkeypatch.setattr(session_mod.time, "perf_counter", lambda: next(ticks))
    out, _ = drive(ctx, ["q"])
    assert "quick search \u00b7 8.0s" in out
    out, _ = drive(ctx, ["/mode agent", "q"])
    assert "investigated with agent \u00b7 8.0s" in out


def test_footer_no_elapsed_or_tiny():
    from docket.cli.interactive.render import footer_text

    assert footer_text("fast") == "quick search"
    assert footer_text("agent", 0.01) == "investigated with agent"
    assert footer_text("agent", 14.14) == "investigated with agent \u00b7 14.1s"
