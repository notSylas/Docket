"""Mutable session state shared by the session, completers and (later) toolbar.

Completers run in a background thread, so they must only read the immutable
`sources` snapshot here and never touch the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceInfo:
    id: str
    path: str
    status: str


@dataclass
class SessionState:
    sources: tuple[SourceInfo, ...] = ()
    mode: str = "auto"
    model: str = ""
    turns: int = 0
    indexed: bool = False
    searchable_files: int = 0
    searchable_chunks: int = 0
    readiness_error: str | None = None
    citations: tuple[tuple[int, str], ...] = ()  # (number, source name) of last answer
    extra: dict[str, Any] = field(default_factory=dict)

    def refresh_sources(self, source_manager: Any) -> None:
        """Rebuild the snapshot. Call on the main thread only."""
        try:
            listed = source_manager.list_sources()
            self.sources = tuple(
                SourceInfo(
                    id=str(s.id),
                    path=str(s.path),
                    status=getattr(s.status, "value", str(s.status)),
                )
                for s in listed
            )
        except Exception:
            # Keep the previous snapshot; completion must never break the session.
            pass

    def toolbar_text(self, width: int | None = None) -> str:
        """Pure, cheap status line (no I/O). Drops low-priority segments to fit `width`."""
        n = len(self.sources)
        if n == 0:
            count_part = ["no sources \u2014 /add <folder>"]
        else:
            count_part = [
                f"{n} source{'' if n == 1 else 's'}",
                "indexed" if self.indexed else "not indexed yet",
            ]
        mode = f"mode: {self.mode}"
        turns = f"{self.turns} turn{'' if self.turns == 1 else 's'}"
        hint = "/ for commands \u00b7 /exit to quit"

        def build(with_mode: bool, with_turns: bool, with_model: bool) -> str:
            segs = list(count_part)
            if with_mode:
                segs.append(mode)
            if with_turns:
                segs.append(turns)
            if with_model and self.model:
                segs.append(self.model)
            return " \u00b7 ".join(segs) + "  |  " + hint

        # Drop order: model, then turns, then mode.
        text = ""
        for flags in ((True, True, True), (True, True, False), (True, False, False), (False, False, False)):
            text = build(*flags)
            if width is None or len(text) < width:
                return text
        return text
