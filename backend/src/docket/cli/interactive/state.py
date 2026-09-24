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
    # Extend with more toolbar fields (indexed flag, turns, model, ...) as needed.
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
