"""Headless driver for tests and screenshots-as-text.

Uses prompt_toolkit's pipe input and a dummy output of a chosen size. It never
opens a real terminal, never reads the home directory, and every wait is bounded.
"""

from __future__ import annotations

import asyncio
from typing import Any

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from docket.interfaces.cli.tui.app import DemoUI

KEYS = {
    "enter": "\r",
    "esc": "\x1b",
    "alt-enter": "\x1b\r",
    "ctrl-j": "\n",
    "ctrl-c": "\x03",
    "ctrl-d": "\x04",
    "ctrl-u": "\x15",
    "tab": "\t",
    "shift-tab": "\x1b[Z",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "backspace": "\x7f",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "ctrl-end": "\x1b[1;5F",
    "alt-up": "\x1b\x1b[A",
    "alt-down": "\x1b\x1b[B",
}


class SizedOutput(DummyOutput):
    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        super().__init__()
        self.columns, self.rows = columns, rows

    def get_size(self) -> Size:
        return Size(rows=self.rows, columns=self.columns)


class Harness:
    """Async context manager: ``async with Harness(...) as h: await h.press("f2")``."""

    def __init__(self, state: str = "chat", cols: int = 120, rows: int = 40, timeout: float = 5.0, **kw: Any) -> None:
        self.cols, self.rows, self.timeout = cols, rows, timeout
        self.kw = kw
        self.state = state
        self.renders = 0
        self.ui: DemoUI | None = None

    async def __aenter__(self) -> "Harness":
        self._cm = create_pipe_input()
        self._pipe = self._cm.__enter__()
        self.output = SizedOutput(self.cols, self.rows)
        kw = dict(self.kw)
        kw.setdefault("reduced_motion", True)
        kw.setdefault("ascii_mode", False)
        self.ui = DemoUI(self.state, input=self._pipe, output=self.output, **kw)
        self.ui.application.after_render.add_handler(lambda _a: self._bump())
        self._task = asyncio.ensure_future(self.ui.run_async())
        await self.settle()
        return self

    def _bump(self) -> None:
        self.renders += 1

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self.ui and self.ui.application.is_running:
                self.ui.application.exit()
            await asyncio.wait_for(self._task, self.timeout)
        except Exception:
            self._task.cancel()
        finally:
            self._cm.__exit__(None, None, None)

    async def settle(self) -> None:
        """Wait until rendering has been idle for a short moment (bounded)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        last, stable = -1, 0
        while stable < 3:
            if loop.time() > deadline:
                raise TimeoutError("UI did not settle")
            await asyncio.sleep(0.03)
            if self.renders == last and self.renders > 0:
                stable += 1
            else:
                stable, last = 0, self.renders

    async def send(self, raw: str) -> None:
        self._pipe.send_text(raw)
        await self.settle()

    async def press(self, *names: str) -> None:
        for n in names:
            await self.send(KEYS.get(n, n))

    async def type(self, text: str) -> None:
        await self.send(text)

    async def resize(self, cols: int, rows: int) -> None:
        self.output.columns, self.output.rows = cols, rows
        assert self.ui
        self.ui.application._on_resize()
        await self.settle()

    # -- inspection ----------------------------------------------------
    def screen_lines(self) -> list[str]:
        assert self.ui
        screen = self.ui.application.renderer.last_rendered_screen
        if screen is None:
            return []
        lines = []
        for y in range(self.output.rows):
            row = screen.data_buffer[y] if y in screen.data_buffer else {}
            line = "".join(row[x].char if x in row else " " for x in range(self.output.columns))
            lines.append(line.rstrip())
        return lines

    def text(self) -> str:
        return "\n".join(self.screen_lines())

    @property
    def focused(self) -> str:
        assert self.ui
        w = self.ui.application.layout.current_window
        if w is self.ui.overlay_window:
            return "overlay"
        if w is self.ui.composer_window:
            return "composer"
        return "other"


def run(coro_fn, *args: Any, timeout: float = 20.0) -> Any:
    """Run an async scenario with a hard overall bound."""

    async def bounded() -> Any:
        return await asyncio.wait_for(coro_fn(*args), timeout)

    return asyncio.run(bounded())


def capture(state: str, keys: list[str] | None = None, cols: int = 100, rows: int = 30, typed: str | None = None, **kw: Any) -> str:
    """Render a state (optionally after key presses) to plain text."""

    async def scenario() -> str:
        async with Harness(state, cols, rows, **kw) as h:
            if typed:
                await h.type(typed)
            for k in keys or []:
                await h.press(k)
            return h.text()

    return run(scenario)
