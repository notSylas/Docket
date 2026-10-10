"""Discovery ignore rules: which folders/files under a registered root are
never ingested (virtualenvs, site-packages, hidden folders, Office lock
files, plus an optional `.docketignore` at the root and the source's stored
`rules_json`).

Everything is evaluated on paths RELATIVE to the registered root, so a root
that itself lives under a hidden or virtualenv path still works, and the
root is never ignored.

Deliberately NOT ignored (too risky): directories named tests, fixtures,
docs, build, dist, env.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from docket.infra.parsing.filecheck import InvalidFileError, check_file_signature

DOCKETIGNORE_NAME = ".docketignore"

_IGNORED_DIR_NAMES = frozenset(
    {
        "node_modules",
        "__pycache__",
        "site-packages",
        "dist-packages",
        "venv",
        "__macosx",
        "$recycle.bin",
        ".tox",
    }
)


def is_ignored_dir_name(name: str) -> bool:
    """Built-in rule for one directory name (case-insensitive)."""
    lowered = name.lower()
    if lowered.startswith("."):  # hidden, incl. .venv*, .git, .tox
        return True
    if lowered in _IGNORED_DIR_NAMES:
        return True
    if lowered.endswith(".egg-info"):
        return True
    if lowered.endswith(("-venv", "_venv")):
        return True
    if lowered.startswith("venv") and (len(lowered) == 4 or not lowered[4].isalpha()):
        return True  # venv, venv3, venv-foo, venv_x; not "venvironment"
    return False


def is_ignored_file_name(name: str) -> bool:
    """Built-in rule for one file name: hidden files and Office lock files."""
    return name.startswith(".") or name.startswith("~$")


@dataclass(frozen=True)
class _Pattern:
    text: str
    dir_only: bool
    has_slash: bool


def _compile_pattern(line: str) -> _Pattern | None:
    """One gitignore-style line -> pattern. Blank, comment and `!` lines and
    bare slashes yield None."""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("!"):
        return None
    dir_only = line.endswith("/")
    anchored = line.startswith("/")  # gitignore: a leading slash anchors to the root
    line = line.strip("/")
    if not line:
        return None
    return _Pattern(line, dir_only, "/" in line or anchored)


def parse_rules_json(raw: object) -> dict:
    """Parse a source's stored `rules_json` (str, dict or None) into
    ``{"exclude": [...], "include": [...], "ignored_files": [...]}``.
    Malformed input degrades to "no rules"."""
    empty: dict = {"exclude": [], "include": [], "ignored_files": []}
    data = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return empty
    if not isinstance(data, dict):
        return empty
    out: dict = {}
    for key in ("exclude", "include", "ignored_files"):
        value = data.get(key, [])
        out[key] = [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    return out


def dump_rules_json(rules: dict) -> str | None:
    """Canonical `rules_json` text, or None when there is nothing to store."""
    parsed = parse_rules_json(rules)
    if not any(parsed.values()):
        return None
    return json.dumps({"version": 1, **parsed}, sort_keys=True)


@dataclass
class IgnoreRules:
    """Built-in rules plus the (optional) `.docketignore` patterns and the
    source's stored rules (`exclude` / `include` / `ignored_files`)."""

    patterns: list[_Pattern] = field(default_factory=list)
    # When non-empty, a file is kept only if it, or one of its ancestor
    # directories, matches an include pattern.
    include: list[_Pattern] = field(default_factory=list)
    ignored_files: frozenset[str] = frozenset()

    @classmethod
    def for_root(cls, root: Path, extra_rules: object = None) -> "IgnoreRules":
        """Rules for `root`: `.docketignore` plus `extra_rules` (a source's
        stored `rules_json`, as text or a dict)."""
        patterns: list[_Pattern] = []
        try:
            raw = (Path(root) / DOCKETIGNORE_NAME).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raw = ""
        for line in raw.splitlines():
            pat = _compile_pattern(line)
            if pat is not None:
                patterns.append(pat)
        stored = parse_rules_json(extra_rules)
        for line in stored["exclude"]:
            pat = _compile_pattern(line)
            if pat is not None:
                patterns.append(pat)
        include = [p for p in map(_compile_pattern, stored["include"]) if p is not None]
        ignored = frozenset(
            PurePosixPath(f.replace("\\", "/")).as_posix().lstrip("/")
            for f in stored["ignored_files"]
        )
        return cls(patterns, include, ignored)

    def _custom_match(self, rel_posix: str, name: str, is_dir: bool) -> bool:
        for pat in self.patterns:
            if pat.dir_only and not is_dir:
                continue
            target = rel_posix if pat.has_slash else name
            if fnmatch.fnmatchcase(target, pat.text):
                return True
        return False

    def _include_allows(self, rel_posix: str, is_dir: bool) -> bool:
        """Include rules drop FILES that no include pattern covers, plus
        directories that provably cannot contain an included path (a
        directory is never pruned when unsure)."""
        if not self.include:
            return True
        parts = rel_posix.split("/")
        for pat in self.include:
            # Matches the entry itself or any ancestor directory.
            for i in range(1, len(parts) + 1):
                target = "/".join(parts[:i]) if pat.has_slash else parts[i - 1]
                if fnmatch.fnmatchcase(target, pat.text):
                    return True
            if is_dir:
                if not pat.has_slash:
                    return True  # a name glob could match anywhere below
                if pat.text.startswith(rel_posix + "/"):
                    return True  # an include path lies under this directory
                if any(c in pat.text for c in "*?["):
                    return True  # wildcard path: cannot tell, do not prune
        return False

    def entry_ignored(self, rel_posix: str, name: str, is_dir: bool) -> bool:
        """Is this single entry (given its path relative to the root) ignored?
        Ancestors are NOT consulted -- the walk prunes them first."""
        if is_dir:
            if is_ignored_dir_name(name):
                return True
        elif is_ignored_file_name(name):
            return True
        elif rel_posix in self.ignored_files:
            return True
        if self._custom_match(rel_posix, name, is_dir):
            return True
        return not self._include_allows(rel_posix, is_dir)

    def path_ignored(self, rel: Path) -> bool:
        """Would a FILE at `rel` (relative to the root) be ignored, counting
        every ancestor directory? Used by `docket sources prune`."""
        parts = PurePosixPath(*rel.parts).parts if rel.parts else ()
        for i in range(1, len(parts) + 1):
            is_dir = i < len(parts)
            if self.entry_ignored("/".join(parts[:i]), parts[i - 1], is_dir):
                return True
        return False


@dataclass
class DiscoveryReport:
    files: list[Path]
    dirs_skipped: int = 0
    # Discoverable-extension files that live in a pruned directory or were
    # ignored by a file rule.
    files_skipped: int = 0
    # Files whose bytes contradict their extension (e.g. a text file named
    # x.pdf): not real documents, never handed to a parser.
    invalid_skipped: int = 0
    # Set when a `max_entries` / `time_budget_s` bound stopped the walk early
    # (bounded scans only; ingestion never passes a budget).
    incomplete: bool = False
    entries_scanned: int = 0


def discover(
    root: Path,
    *,
    extensions: frozenset[str] | set[str],
    enabled: bool = True,
    extra_rules: object = None,
    max_entries: int | None = None,
    time_budget_s: float | None = None,
    count_ignored: bool = True,
) -> DiscoveryReport:
    """Walk `root`, pruning ignored directories. With `enabled=False` nothing
    is ignored (escape hatch: DOCKET_INGEST_IGNORE_ENABLED=false).
    `extra_rules` is the source's stored `rules_json`.

    The remaining parameters serve bounded scans (`services/sources/scan.py`)
    and default to the unbounded behaviour ingestion relies on: stop after
    `max_entries` directory entries or `time_budget_s` seconds (reported as
    `incomplete`), and `count_ignored=False` skips counting the documents
    inside ignored folders (which would walk e.g. a whole `node_modules`)."""
    root = Path(root)
    rules = IgnoreRules.for_root(root, extra_rules) if enabled else IgnoreRules()
    report = DiscoveryReport(files=[])
    deadline = None if time_budget_s is None else time.monotonic() + time_budget_s

    def over_budget() -> bool:
        if max_entries is not None and report.entries_scanned > max_entries:
            return True
        return deadline is not None and time.monotonic() > deadline

    for dirpath, dirnames, filenames in os.walk(root):
        report.entries_scanned += len(dirnames) + len(filenames)
        if over_budget():
            report.incomplete = True
            break
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        rel_prefix = "" if str(rel_dir) == "." else rel_dir.as_posix() + "/"

        kept: list[str] = []
        for d in dirnames:
            if enabled and rules.entry_ignored(rel_prefix + d, d, True):
                report.dirs_skipped += 1
                if count_ignored:
                    report.files_skipped += _count_supported(here / d, extensions)
            else:
                kept.append(d)
        dirnames[:] = kept

        for f in filenames:
            if Path(f).suffix.lower() not in extensions:
                continue
            if deadline is not None and time.monotonic() > deadline:
                report.incomplete = True
                break
            if enabled and rules.entry_ignored(rel_prefix + f, f, False):
                report.files_skipped += 1
                continue
            p = here / f
            if p.is_file():
                try:
                    check_file_signature(p)
                except InvalidFileError:
                    report.invalid_skipped += 1
                    continue
                except OSError:
                    pass  # unreadable: the parser reports it per file
                report.files.append(p)

    report.files.sort()
    return report


def _count_supported(directory: Path, extensions) -> int:
    n = 0
    for _dp, _dn, fns in os.walk(directory):
        n += sum(1 for f in fns if Path(f).suffix.lower() in extensions)
    return n


def skipped_summary(files_skipped: int) -> str | None:
    """The one dim line printed after an ingest, or None if nothing skipped."""
    if files_skipped <= 0:
        return None
    noun = "file" if files_skipped == 1 else "files"
    return (
        f"Skipped {files_skipped} {noun} in ignored folders "
        "(virtualenvs, site-packages, hidden)"
    )


def invalid_summary(invalid_skipped: int) -> str | None:
    """Dim line for files skipped because they are not real documents."""
    if invalid_skipped <= 0:
        return None
    if invalid_skipped == 1:
        return "Skipped 1 file that is not a real document (contents do not match its extension)"
    return (
        f"Skipped {invalid_skipped} files that are not real documents "
        "(contents do not match their extensions)"
    )
