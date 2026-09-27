import os
import threading
import time
import stat

import pytest
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from docket.interfaces.cli.interactive import reader as reader_mod
from docket.interfaces.cli.interactive.commands import default_registry
from docket.interfaces.cli.interactive.completer import DocketCompleter
from docket.interfaces.cli.interactive.reader import CallableReader, PtkReader, build_history, make_reader
from docket.interfaces.cli.interactive.state import SessionState


def run(text, history=None, completer=None, chunks=None):
    """Feed `chunks` (or `text`) from a helper thread with small pauses, so that
    background completion has time to finish between keystrokes, like a human."""
    chunks = chunks or [text]
    with create_pipe_input() as pipe:
        r = PtkReader(completer, history or InMemoryHistory(), input=pipe, output=DummyOutput())

        def feed():
            for chunk in chunks:
                time.sleep(0.15)
                pipe.send_text(chunk)

        t = threading.Thread(target=feed)
        t.start()
        try:
            return r.read("docket> ")
        finally:
            t.join()


def test_plain_line():
    assert run("hello\r") == "hello"


def test_tab_completes_command():
    completer = DocketCompleter(default_registry(), SessionState())
    assert run("", completer=completer, chunks=["/he", "\t", "\r"]) == "/help"


def test_tab_completes_command_with_arg_adds_space():
    completer = DocketCompleter(default_registry(), SessionState())
    assert run("", completer=completer, chunks=["/mo", "\t", "\r"]) == "/mode "


def test_escape_enter_inserts_newline():
    assert run("a\x1b\rb\r") == "a\nb"


def test_history_persists(tmp_path):
    h1 = build_history(tmp_path)
    assert run("first question\r", history=h1) == "first question"
    h2 = build_history(tmp_path)
    assert "first question" in list(h2.load_history_strings())


def test_history_env_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKET_NO_HISTORY", "1")
    assert isinstance(build_history(tmp_path), InMemoryHistory)
    assert not (tmp_path / "history").exists()


def test_history_unusable_dir(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert isinstance(build_history(blocker / "sub"), InMemoryHistory)


def test_history_mode_0600(tmp_path):
    build_history(tmp_path / "new")
    mode = stat.S_IMODE(os.stat(tmp_path / "new" / "history").st_mode)
    assert mode == 0o600


def test_history_store_failure_is_swallowed(tmp_path):
    h = build_history(tmp_path)
    os.chmod(tmp_path / "history", 0o400)
    if os.access(tmp_path / "history", os.W_OK):
        pytest.skip("running as a user that ignores file modes")
    h.store_string("x")  # must not raise


class _Ctx:
    class settings:
        data_dir = "unused"


def _gate(input_fn, monkeypatch, tty=True, term="xterm-256color"):
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(reader_mod.sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(reader_mod.sys.stdout, "isatty", lambda: tty, raising=False)
    return make_reader(input_fn, _Ctx, SessionState(), default_registry())


def test_gate_custom_input_fn(monkeypatch):
    assert isinstance(_gate(lambda p: "", monkeypatch), CallableReader)


def test_gate_non_tty(monkeypatch):
    assert isinstance(_gate(input, monkeypatch, tty=False), CallableReader)


def test_gate_term_dumb(monkeypatch):
    assert isinstance(_gate(input, monkeypatch, term="dumb"), CallableReader)


def test_prompt_and_toolbar_fns_with_mode_change():
    from docket.interfaces.cli.interactive.reader import prompt_for_mode

    state = SessionState(mode="auto")
    seen = []

    def prompt_fn():
        p = prompt_for_mode(state.mode)
        seen.append(p)
        return p

    with create_pipe_input() as pipe:
        r = PtkReader(
            None, InMemoryHistory(), input=pipe, output=DummyOutput(),
            prompt_fn=prompt_fn, toolbar_fn=state.toolbar_text,
        )
        pipe.send_text("one\r")
        assert r.read("ignored> ") == "one"
        state.mode = "agent"
        pipe.send_text("two\r")
        assert r.read("ignored> ") == "two"
    assert "docket> " in seen and "docket (agent)> " in seen
    assert prompt_for_mode("fast") == "docket (fast)> "
    assert r.current_prompt() == "docket (agent)> "
    frags = r._toolbar()
    assert "mode: agent" in "".join(t for _, t in frags)


def test_confirm_reads_plain_line_and_skips_history():
    hist = InMemoryHistory()
    with create_pipe_input() as pipe:
        r = PtkReader(None, hist, input=pipe, output=DummyOutput())
        pipe.send_text("y\r")
        assert r.confirm("Remove? [y/N] ") == "y"
    assert list(hist.load_history_strings()) == []
