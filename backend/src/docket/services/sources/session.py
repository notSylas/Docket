"""Folder-first launch: what should Docket do for the folder it was started in?

`resolve_session(cwd, source_manager)` answers with one `SessionPlan`:

* ``library``  the folder is home, ``/`` or a broad well-known root (Desktop,
  Downloads, Documents), or no folder was given (icon launch, ``--all``):
  show the library of all folders.
* ``covered``  the folder is an ACTIVE source or lies inside one: reuse that
  source, scope questions to the folder, index nothing.
* ``new``      not covered and it holds documents: carries the bounded scan and
  whether a preview is needed (broad or large).
* ``empty``    no supported documents here: explain, offer the library.

Overlap rules are CP1's: `SourceManager.register_source` refuses a folder
inside an ACTIVE source and excludes registered children from a parent.
Nothing in this module registers or indexes; the interface does that after
the user agrees.

The one-time consent ("Docket will read 4 documents in ...") is remembered in
`<data_dir>/preferences.json` under a versioned key.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from docket.core.db.models import Source, SourceStatus
from docket.services.ingestion.ignore import parse_rules_json
from docket.services.sources.manager import (
    SourceManager,
    _glob_escape,
    child_excludes,
    is_within,
)
from docket.services.sources.resolve import display_names
from docket.services.sources.scan import (
    ScanResult,
    recent_seconds_per_file,
    scan_folder,
)

KIND_LIBRARY = "library"
KIND_COVERED = "covered"
KIND_NEW = "new"
KIND_EMPTY = "empty"

# Folders that are a place, not a project: opening Docket here shows the
# library instead of offering to read everything.
WELL_KNOWN_ROOTS = ("Desktop", "Downloads", "Documents")

PREFERENCES_NAME = "preferences.json"
CONSENT_KEY = "folder_consent_v1"


@dataclass(frozen=True)
class SessionPlan:
    kind: str
    folder: Path | None = None
    name: str = ""  # short label for the folder (e.g. "ForgeOPs/docs")
    reason: str = ""  # why this kind (library / empty): plain words
    source: Source | None = None  # covering source (covered), disconnected one (new)
    source_name: str = ""
    scope: Any = None  # a QueryScope for the covered folder
    relative_path: str = ""  # covered: the folder relative to the source root ("" = root)
    scan: ScanResult | None = None
    needs_preview: bool = False
    excludes: tuple[str, ...] = ()  # child-source excludes a registration would store
    disconnected: Source | None = None  # an earlier, user-disconnected source for this exact folder
    suggestions: tuple[tuple[str, int], ...] = ()  # library: (folder path, documents), most first

    @property
    def documents(self) -> int:
        return self.scan.documents if self.scan is not None else 0


# -- location rules ------------------------------------------------------------


def broad_location_roots(home: Path | None = None) -> list[Path]:
    home = Path(home) if home is not None else Path.home()
    return [home, *(home / name for name in WELL_KNOWN_ROOTS), Path("/")]


def broad_location_reason(path: Path, *, home: Path | None = None) -> str:
    """Plain reason when `path` is itself a broad place; "" otherwise.

    Pure path logic (no I/O beyond resolving links): the launcher uses it to
    decide the window title before any scan runs."""
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        return ""
    if real == Path("/"):
        return "the top of the file system"
    for candidate in broad_location_roots(home):
        try:
            if Path(os.path.realpath(candidate)) == real:
                if candidate == (Path(home) if home is not None else Path.home()):
                    return "your home folder"
                return f"your {candidate.name} folder"
        except OSError:
            continue
    return ""


def folder_label(path: Path) -> str:
    """'ForgeOPs/docs': the folder and its parent, for one-line messages."""
    parts = Path(path).parts
    tail = [p for p in parts[-2:] if p not in ("/", "")]
    return "/".join(tail) or str(path)


# -- consent ---------------------------------------------------------------------


def _preferences_path(data_dir: Path) -> Path:
    return Path(data_dir) / PREFERENCES_NAME


def read_preferences(data_dir: Path) -> dict:
    try:
        data = json.loads(_preferences_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def has_consent(data_dir: Path) -> bool:
    entry = read_preferences(data_dir).get(CONSENT_KEY)
    return bool(entry) and (entry is True or isinstance(entry, dict))


def write_preferences(data_dir: Path, prefs: dict) -> None:
    """Atomically replace `<data_dir>/preferences.json` (temp file + rename)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    target = _preferences_path(data_dir)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".preferences.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(prefs, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_consent(data_dir: Path) -> None:
    """Remember the one-time folder consent (atomic write, other keys kept)."""
    prefs = read_preferences(Path(data_dir))
    prefs[CONSENT_KEY] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    write_preferences(data_dir, prefs)


# -- resolution -----------------------------------------------------------------


def _library(reason: str, folder: Path | None = None, suggestions=()) -> SessionPlan:
    return SessionPlan(
        KIND_LIBRARY,
        folder=folder,
        name=folder_label(folder) if folder else "",
        reason=reason,
        suggestions=tuple(suggestions),
    )


def resolve_session(
    cwd: Path | str | None,
    source_manager: SourceManager,
    *,
    session_factory: Callable[[], Any] | None = None,
    home: Path | None = None,
    scan: Callable[..., ScanResult] = scan_folder,
    suggest_for_library: bool = True,
) -> SessionPlan:
    """Decide what opening Docket in `cwd` means. See the module docstring."""
    if cwd is None or str(cwd) == "":
        return _library("no folder")
    try:
        folder = Path(os.path.realpath(Path(cwd).expanduser()))
    except OSError:
        return _library("folder cannot be read")
    if not folder.is_dir():
        return _library("not a folder", folder)

    broad = broad_location_reason(folder, home=home)
    if broad:
        sources = source_manager.list_sources()
        suggestions: list[tuple[str, int]] = []
        active = [s for s in sources if s.status == SourceStatus.ACTIVE]
        if suggest_for_library and not active and folder != Path("/"):
            suggestions = _library_suggestions(
                folder, scan, _rate(session_factory), home
            )
        return _library(broad, folder, suggestions)

    sources = source_manager.list_sources()
    local = [s for s in sources if s.source_type == "local_folder"]
    names = display_names(sources)

    covering = _covering_source(folder, local)
    if covering is not None:
        from docket.services.query.scope import QueryScope

        root = Path(os.path.realpath(covering.path))
        rel = "" if folder == root else folder.relative_to(root).as_posix()
        label = names.get(covering.id, root.name) + (f" / {rel}" if rel else "")
        scope = (
            QueryScope.for_source(covering.id, label=label)
            if not rel
            else QueryScope.for_folder(str(folder), label=label)
        )
        return SessionPlan(
            KIND_COVERED,
            folder=folder,
            name=folder_label(folder),
            source=covering,
            source_name=names.get(covering.id, root.name),
            scope=scope,
            relative_path=rel,
        )

    excludes = tuple(child_excludes(folder, local))
    result = scan(
        folder,
        extra_rules={"exclude": list(excludes)} if excludes else None,
        seconds_per_file=_rate(session_factory),
        well_known_broad=broad_location_roots(home),
    )
    disconnected = next(
        (
            s
            for s in local
            if s.status == SourceStatus.REVOKED
            and s.status_reason in (None, "user_disconnected")
            and _same_path(s.path, folder)
        ),
        None,
    )
    if result.documents == 0:
        why = "no supported documents"
        if result.skipped.get("invalid_files"):
            why = "no readable documents (some files are not real documents)"
        elif excludes:
            why = "everything here is already indexed as separate folders"
        return SessionPlan(
            KIND_EMPTY,
            folder=folder,
            name=folder_label(folder),
            reason=why,
            scan=result,
            excludes=excludes,
        )
    return SessionPlan(
        KIND_NEW,
        folder=folder,
        name=folder_label(folder),
        scan=result,
        needs_preview=bool(result.broad),
        excludes=excludes,
        disconnected=disconnected,
    )


def subfolder_exclude_pattern(name: str) -> str:
    """Rule text that excludes the top-level subfolder `name` (and only that
    one: the leading slash anchors it to the source root)."""
    return "/" + _glob_escape(name) + "/"


def start_folder(
    source_manager: SourceManager,
    plan: SessionPlan,
    *,
    excluded_subfolders: tuple[str, ...] = (),
) -> Source:
    """Register (or reconnect) the folder of a `new` plan, named after the
    folder, and save unticked preview subfolders as `exclude` rules.

    Raises the manager's errors unchanged (`SourceCoveredError` when another
    registration raced in, `ValueError` for an unusable folder)."""
    if plan.folder is None:
        raise ValueError("no folder to register")
    if plan.disconnected is not None:
        source = source_manager.reconnect_source(plan.disconnected.id)
    else:
        source = source_manager.register_source(
            plan.folder, display_name=Path(plan.folder).name or None
        )
    if excluded_subfolders:
        rules = parse_rules_json(source.rules_json)
        for name in excluded_subfolders:
            pattern = subfolder_exclude_pattern(name)
            if pattern not in rules["exclude"]:
                rules["exclude"].append(pattern)
        source = source_manager.set_rules(source.id, rules)
    return source


def _rate(session_factory) -> float | None:
    if session_factory is None:
        return None
    return recent_seconds_per_file(session_factory)


def _same_path(a: str | Path, b: Path) -> bool:
    try:
        return Path(os.path.realpath(a)) == b
    except OSError:
        return False


def _covering_source(folder: Path, local: list[Source]) -> Source | None:
    """The ACTIVE source whose root is `folder` or contains it (deepest wins)."""
    best: Source | None = None
    best_depth = -1
    for s in local:
        if s.status != SourceStatus.ACTIVE:
            continue
        root = Path(os.path.realpath(s.path))
        if folder == root or is_within(folder, root):
            depth = len(root.parts)
            if depth > best_depth:
                best, best_depth = s, depth
    return best


def _library_suggestions(
    folder: Path, scan, rate: float | None, home: Path | None
) -> list[tuple[str, int]]:
    """Subfolders of a broad `folder` that hold documents, most documents first."""
    try:
        result = scan(
            folder,
            seconds_per_file=rate,
            well_known_broad=broad_location_roots(home),
        )
    except Exception:  # noqa: BLE001 -- suggestions are a nicety
        return []
    out = [(str(folder / name), n) for name, n in result.suggested_subfolders if n > 0]
    return out[:6]


__all__ = [
    "CONSENT_KEY",
    "KIND_COVERED",
    "KIND_EMPTY",
    "KIND_LIBRARY",
    "KIND_NEW",
    "SessionPlan",
    "broad_location_reason",
    "folder_label",
    "has_consent",
    "record_consent",
    "resolve_session",
]
