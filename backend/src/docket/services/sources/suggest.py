"""Folders that look like they hold documents, for an empty library.

A bounded, time-limited look at a few well-known places: the children (one
level deep) of `~/Documents`, `~/Downloads` and `~/Desktop`, plus the
siblings of the current directory. Each candidate is measured with
`scan.scan_folder` (the same ignore rules and signature check as indexing),
so a count here is what indexing would read.

Safety rules: never scan `/` or a place that is itself broad, never list a
hidden folder, never follow a symlink, stop at a total time and entry budget.
Meant for a worker thread.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from docket.services.sources.scan import scan_folder

WELL_KNOWN_PLACES: tuple[str, ...] = ("Documents", "Downloads", "Desktop")
DEFAULT_TIME_BUDGET_S = 3.0
DEFAULT_MAX_ENTRIES = 30000  # directory entries looked at, across all candidates
PER_FOLDER_ENTRIES = 5000
PER_FOLDER_SECONDS = 0.6
MAX_CANDIDATES = 60
DEFAULT_LIMIT = 6

_TYPE_NAMES = {".pdf": "PDF", ".docx": "Word", ".xlsx": "Excel", ".pptx": "PowerPoint",
               ".txt": "Text", ".md": "Markdown", ".csv": "CSV", ".html": "HTML", ".htm": "HTML"}


@dataclass(frozen=True)
class FolderSuggestion:
    path: str
    name: str
    documents: int
    by_type: dict[str, int] = field(default_factory=dict)
    incomplete: bool = False

    @property
    def type_summary(self) -> str:
        """'4 PDF, 2 Word' (most common first)."""
        ranked = sorted(self.by_type.items(), key=lambda kv: (-kv[1], kv[0]))
        return ", ".join(f"{n} {_TYPE_NAMES.get(ext, ext.lstrip('.').upper())}" for ext, n in ranked)

    @property
    def count_text(self) -> str:
        n = self.documents
        return f"{n}+ documents" if self.incomplete else f"{n} document{'s' if n != 1 else ''}"


def _child_dirs(parent: Path, limit: int) -> list[Path]:
    """Visible, non-link subfolders of `parent` (sorted, capped)."""
    out: list[Path] = []
    try:
        with os.scandir(parent) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                out.append(Path(entry.path))
                if len(out) >= limit * 4:
                    break
    except OSError:
        return []
    return sorted(out, key=lambda p: p.name.lower())[:limit]


def candidate_folders(home: Path, cwd: Path | None) -> list[Path]:
    """The folders worth measuring, most promising places first, de-duplicated."""
    home = Path(home)
    seen: set[str] = set()
    out: list[Path] = []

    def add(paths: Iterable[Path]) -> None:
        for p in paths:
            key = os.path.realpath(p)
            if key in seen or key == "/" or key == os.path.realpath(home):
                continue
            seen.add(key)
            out.append(p)

    for name in WELL_KNOWN_PLACES:
        add(_child_dirs(home / name, MAX_CANDIDATES))
    if cwd is not None:
        cwd = Path(cwd)
        parent = cwd.parent
        # never `/`, and never the directory that holds every user's home
        if parent != cwd and len(parent.parts) >= 2 and parent != Path("/home"):
            add(p for p in _child_dirs(parent, MAX_CANDIDATES) if os.path.realpath(p) != os.path.realpath(cwd))
    return out[:MAX_CANDIDATES]


def _covered(path: Path, roots: list[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True
    return False


def suggest_folders(
    home: Path | None = None,
    cwd: Path | None = None,
    *,
    registered: Iterable[str | os.PathLike[str]] = (),
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    limit: int = DEFAULT_LIMIT,
    scan=scan_folder,
) -> list[FolderSuggestion]:
    """Folders that hold documents, most documents first.

    `registered` are source roots to leave out (already added or inside one).
    The whole call stops at `time_budget_s` and `max_entries`; what was
    measured by then is returned."""
    home = Path(home) if home is not None else Path.home()
    if cwd is None:
        try:
            cwd = Path.cwd()
        except OSError:
            cwd = None
    roots = [os.path.realpath(r) for r in registered]
    deadline = time.monotonic() + time_budget_s
    remaining_entries = max_entries
    found: list[FolderSuggestion] = []
    for folder in candidate_folders(home, cwd):
        left = deadline - time.monotonic()
        if left <= 0 or remaining_entries <= 0:
            break
        if _covered(folder, roots):
            continue
        try:
            result = scan(
                folder,
                time_budget_s=min(PER_FOLDER_SECONDS, left),
                max_entries=min(PER_FOLDER_ENTRIES, remaining_entries),
            )
        except Exception:  # noqa: BLE001 -- a suggestion is a nicety
            continue
        remaining_entries -= max(int(result.entries_scanned), 1)
        if result.documents > 0:
            found.append(
                FolderSuggestion(
                    str(folder), folder.name, result.documents, dict(result.by_type), bool(result.incomplete)
                )
            )
    found.sort(key=lambda s: (-s.documents, s.name.lower(), s.path))
    return found[:limit]


__all__ = ["FolderSuggestion", "candidate_folders", "suggest_folders"]
