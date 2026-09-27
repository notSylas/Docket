"""Line-reading abstraction so the session is testable and swappable."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Protocol

from .commands import CommandRegistry
from .state import SessionState


class LineReader(Protocol):
    def read(self, prompt: str) -> str:
        """Read one line; raise EOFError / KeyboardInterrupt like input()."""
        ...


class CallableReader:
    def __init__(self, input_fn: Callable[[str], str]) -> None:
        self._input_fn = input_fn

    def read(self, prompt: str) -> str:
        return self._input_fn(prompt)


def prompt_for_mode(mode: str) -> str:
    return "docket> " if mode in ("", "auto") else f"docket ({mode})> "


def _make_file_history_class() -> type:
    from prompt_toolkit.history import FileHistory

    class SafeFileHistory(FileHistory):
        def store_string(self, string: str) -> None:
            try:
                super().store_string(string)
            except OSError:
                pass  # a failed history write must never crash the session

    return SafeFileHistory


def build_history(data_dir: Path | str) -> Any:
    """FileHistory at <data_dir>/history (0600), or InMemoryHistory on any problem."""
    from prompt_toolkit.history import InMemoryHistory

    if os.environ.get("DOCKET_NO_HISTORY"):
        return InMemoryHistory()
    try:
        path = Path(data_dir) / "history"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if not os.access(path, os.W_OK):
            return InMemoryHistory()
        return _make_file_history_class()(str(path))
    except Exception:
        return InMemoryHistory()


_STYLE = {
    "completion-menu": "bg:#262626 #d0d0d0",
    "completion-menu.completion": "bg:#262626 #d0d0d0",
    "completion-menu.completion.current": "bg:#3a5f7f #ffffff",
    "completion-menu.meta.completion": "bg:#262626 #808080",
    "completion-menu.meta.completion.current": "bg:#3a5f7f #c0c0c0",
    "scrollbar.background": "bg:#262626",
    "scrollbar.button": "bg:#606060",
    "bottom-toolbar": "noreverse #808080",
    "bottom-toolbar.text": "noreverse #808080",
    "prompt": "bold",
    "auto-suggestion": "#666666",
}


class PtkReader:
    """prompt_toolkit-backed reader. prompt_toolkit is imported lazily."""

    def __init__(
        self,
        completer: Any = None,
        history: Any = None,
        *,
        input: Any = None,
        output: Any = None,
        prompt_fn: Callable[[], str] | None = None,
        toolbar_fn: Callable[[int | None], str] | None = None,
    ) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import ThreadedCompleter
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style

        kb = KeyBindings()

        @kb.add("enter")
        def _submit(event: Any) -> None:
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        def _newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        self.history = history
        self._input = input
        self._output = output
        self._prompt_fn = prompt_fn
        self._toolbar_fn = toolbar_fn
        self._session = PromptSession(
            completer=ThreadedCompleter(completer) if completer is not None else None,
            history=history,
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            reserve_space_for_menu=10,
            auto_suggest=AutoSuggestFromHistory(),
            enable_history_search=False,
            multiline=True,
            key_bindings=kb,
            style=Style.from_dict(_STYLE),
            input=input,
            output=output,
            bottom_toolbar=self._toolbar if toolbar_fn is not None else None,
        )

    def current_prompt(self, default: str = "docket> ") -> str:
        if self._prompt_fn is not None:
            try:
                return self._prompt_fn()
            except Exception:
                pass
        return default

    def _toolbar(self) -> Any:
        from prompt_toolkit.formatted_text import FormattedText

        width: int | None = None
        try:
            from prompt_toolkit.application import get_app

            width = get_app().output.get_size().columns
        except Exception:
            pass
        try:
            text = self._toolbar_fn(width) if self._toolbar_fn else ""
        except Exception:
            text = ""
        return FormattedText([("class:bottom-toolbar", " " + text)])

    def confirm(self, prompt: str) -> str:
        """One plain line (no completion/toolbar, not stored in history)."""
        from prompt_toolkit import PromptSession

        return PromptSession(input=self._input, output=self._output).prompt(prompt)

    def read(self, prompt: str) -> str:
        # Callable message: re-evaluated on every render, so mode changes show up.
        return self._session.prompt(lambda: self.current_prompt(prompt))


def make_reader(
    input_fn: Callable[[str], str],
    context: Any,
    state: SessionState,
    registry: CommandRegistry,
    console: Any = None,
) -> LineReader:
    if (
        input_fn is input
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
    ):
        try:
            from .completer import DocketCompleter

            history = build_history(context.settings.data_dir)
            return PtkReader(
                DocketCompleter(registry, state),
                history,
                prompt_fn=lambda: prompt_for_mode(state.mode),
                toolbar_fn=state.toolbar_text,
            )
        except Exception as exc:
            msg = f"(rich line editing unavailable: {type(exc).__name__}; using plain input)"
            if console is not None:
                console.print(msg, style="dim", markup=False)
            else:
                print(msg)
    if input_fn is input:
        _setup_readline()
    return CallableReader(input_fn)


def _setup_readline() -> None:
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
