"""Human-friendly source references.

One implementation shared by the CLI, the classic prompt and the full-screen
UI:

- `display_names(sources)`: the stored `display_name`, else the folder name,
  disambiguated by the shortest unique parent-path suffix when two sources
  collide ("docs (ForgeOPs)", then "docs (work/ForgeOPs)" if needed).
- `resolve_source(ref, sources)`: accepts a display name (case-insensitive,
  must be unique), the 1-based number shown by `docket sources list`, a
  unique ID prefix, or the full ID.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from docket.core.db.models import SourceStatus


class SourceRefError(ValueError):
    """A source reference could not be resolved to exactly one source."""

    def __init__(self, message: str, candidates: Sequence[Any] = ()) -> None:
        self.candidates = list(candidates)
        super().__init__(message)


class UnknownSourceRefError(SourceRefError):
    """No source matches the reference."""


class AmbiguousSourceRefError(SourceRefError):
    """More than one source matches the reference."""


def ordered_sources(sources: Iterable[Any]) -> list[Any]:
    """Stable list order behind the 1-based numbers shown in `sources list`:
    oldest first, ties broken by ID."""
    return sorted(sources, key=lambda s: (getattr(s, "created_at", None) is None,
                                          str(getattr(s, "created_at", "") or ""),
                                          str(s.id)))


def _base_name(source: Any) -> str:
    stored = (getattr(source, "display_name", None) or "").strip()
    if stored:
        return stored
    return Path(source.path).name or str(source.path)


def _parent_parts(source: Any) -> list[str]:
    return [p for p in Path(source.path).parent.parts if p not in ("/", "\\") and not p.endswith(":\\")]


def display_names(sources: Iterable[Any]) -> dict[str, str]:
    """Map source id -> display name (see module docstring)."""
    items = list(sources)
    base = {s.id: _base_name(s) for s in items}
    groups: dict[str, list[Any]] = {}
    for s in items:
        groups.setdefault(base[s.id], []).append(s)

    out: dict[str, str] = {}
    for name, members in groups.items():
        if len(members) == 1:
            out[members[0].id] = name
            continue
        parents = {m.id: _parent_parts(m) for m in members}
        deepest = max((len(p) for p in parents.values()), default=0)
        chosen: dict[str, str] | None = None
        for depth in range(1, max(deepest, 1) + 1):
            labels = {
                m.id: (
                    f"{name} ({'/'.join(parents[m.id][-depth:])})" if parents[m.id] else name
                )
                for m in members
            }
            chosen = labels
            if len(set(labels.values())) == len(labels):
                break
        out.update(chosen or {m.id: name for m in members})
    return out


def _describe(source: Any, names: dict[str, str], number: int | None) -> str:
    prefix = f"{number}. " if number is not None else ""
    return f"{prefix}{names.get(source.id, source.id)} ({source.path}) [{source.id}]"


def _candidate_lines(matches: Sequence[Any], ordered: Sequence[Any], names: dict[str, str]) -> str:
    index = {s.id: i for i, s in enumerate(ordered, start=1)}
    return "\n".join("  " + _describe(s, names, index.get(s.id)) for s in matches)


def resolve_source(ref: str, sources: Iterable[Any]) -> Any:
    """Resolve `ref` (name, list number, ID prefix or full ID) to one source.

    Raises `UnknownSourceRefError` / `AmbiguousSourceRefError` (both
    `ValueError`s) whose message lists the candidates."""
    items = list(sources)
    ordered = ordered_sources(items)
    names = display_names(ordered)
    text = (ref or "").strip()
    if not text:
        raise UnknownSourceRefError("no source given")
    if not ordered:
        raise UnknownSourceRefError(f"source not found: {text} (no sources registered)")

    # 1. List number, as shown by `docket sources list`.
    if text.isdigit():
        number = int(text)
        if 1 <= number <= len(ordered):
            return ordered[number - 1]
        raise UnknownSourceRefError(
            f"no source number {number}: pick 1-{len(ordered)} from `docket sources list`"
        )

    # 2. Full ID.
    for s in ordered:
        if s.id == text:
            return s

    # 3. Display name (case-insensitive): the disambiguated label first, then
    # the bare name. Removed sources only count when nothing live matches.
    lowered = text.lower()

    def _live(matches: list[Any]) -> list[Any]:
        live = [m for m in matches if getattr(m, "status", None) != SourceStatus.DELETED]
        return live or matches

    exact = _live([s for s in ordered if names[s.id].lower() == lowered])
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousSourceRefError(
            f"{text!r} matches {len(exact)} sources; use the number or ID:\n"
            + _candidate_lines(exact, ordered, names),
            exact,
        )
    bare = _live([s for s in ordered if _base_name(s).lower() == lowered])
    if len(bare) == 1:
        return bare[0]
    if len(bare) > 1:
        raise AmbiguousSourceRefError(
            f"{text!r} matches {len(bare)} sources; use the number, the full name or the ID:\n"
            + _candidate_lines(bare, ordered, names),
            bare,
        )

    # 4. Unique ID prefix.
    prefixed = [s for s in ordered if str(s.id).startswith(text)]
    if len(prefixed) == 1:
        return prefixed[0]
    if len(prefixed) > 1:
        raise AmbiguousSourceRefError(
            f"{text!r} matches {len(prefixed)} source IDs; type more of the ID or use the number:\n"
            + _candidate_lines(prefixed, ordered, names),
            prefixed,
        )

    raise UnknownSourceRefError(
        f"source not found: {text}. Known sources:\n"
        + _candidate_lines(ordered, ordered, names),
        ordered,
    )
