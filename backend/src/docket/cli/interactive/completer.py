"""prompt_toolkit completers for slash commands and their arguments.

Completion runs in a background thread: everything here reads only the
immutable `SessionState.sources` snapshot, never the database.
"""

from __future__ import annotations

from typing import Iterable

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    PathCompleter,
    WordCompleter,
)
from prompt_toolkit.document import Document

from .commands import CommandRegistry
from .state import SessionState


class SourceIdCompleter(Completer):
    """`all` plus ids of ACTIVE sources (from the snapshot), path as meta."""

    def __init__(self, state: SessionState) -> None:
        self._state = state

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        word = document.get_word_before_cursor(WORD=True)
        candidates: list[tuple[str, str]] = [("all", "every active source")]
        candidates += [(s.id, s.path) for s in self._state.sources if s.status == "active"]
        for text, meta in candidates:
            if text.startswith(word):
                yield Completion(text, start_position=-len(word), display_meta=meta)


def build_arg_completers(state: SessionState) -> dict[str, Completer]:
    """Argument completers keyed by command name."""
    return {
        "mode": WordCompleter(
            ["auto", "fast", "agent"],
            meta_dict={
                "auto": "let Docket choose",
                "fast": "quick search",
                "agent": "multi-step investigation",
            },
            ignore_case=True,
        ),
        "add": PathCompleter(only_directories=True, expanduser=True),
        "ingest": SourceIdCompleter(state),
    }


class DocketCompleter(Completer):
    def __init__(
        self,
        registry: CommandRegistry,
        state: SessionState,
        arg_completers: dict[str, Completer] | None = None,
    ) -> None:
        self._registry = registry
        self._arg_completers = (
            arg_completers if arg_completers is not None else build_arg_completers(state)
        )

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        head, sep, rest = text[1:].partition(" ")
        if not sep:
            typed = head.lower()
            for cmd in self._registry.all():
                if any(t.lower().startswith(typed) for t in (cmd.name, *cmd.aliases)):
                    yield Completion(
                        text=f"/{cmd.name}" + (" " if cmd.arg_hint else ""),
                        start_position=-len(text),
                        display=cmd.usage,
                        display_meta=cmd.summary,
                    )
            return
        cmd = self._registry.resolve(head)
        if cmd is None:
            return
        completer = self._arg_completers.get(cmd.name)
        if completer is None:
            return
        yield from completer.get_completions(Document(rest, len(rest)), complete_event)
