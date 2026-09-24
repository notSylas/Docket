"""Line-reading abstraction so the session is testable and swappable."""

from __future__ import annotations

from typing import Callable, Protocol


class LineReader(Protocol):
    def read(self, prompt: str) -> str:
        """Read one line; raise EOFError / KeyboardInterrupt like input()."""
        ...


class CallableReader:
    def __init__(self, input_fn: Callable[[str], str]) -> None:
        self._input_fn = input_fn

    def read(self, prompt: str) -> str:
        return self._input_fn(prompt)
