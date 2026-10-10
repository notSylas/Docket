"""Discovery ignore rules: which folders/files under a registered root are
never ingested (virtualenvs, site-packages, hidden folders, Office lock
files, plus an optional `.docketignore` at the root).

Everything is evaluated on paths RELATIVE to the registered root, so a root
that itself lives under a hidden or virtualenv path still works, and the
root is never ignored.

Deliberately NOT ignored (too risky): directories named tests, fixtures,
docs, build, dist, env.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

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


@dataclass
class IgnoreRules:
    """Built-in rules plus the (optional) `.docketignore` patterns."""

    patterns: list[_Pattern] = field(default_factory=list)

    @classmethod
    def for_root(cls, root: Path) -> "IgnoreRules":
        patterns: list[_Pattern] = []
        try:
            raw = (Path(root) / DOCKETIGNORE_NAME).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return cls(patterns)
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            dir_only = line.endswith("/")
            line = line.strip("/")
            if not line:
                continue
            patterns.append(_Pattern(line, dir_only, "/" in line))
        return cls(patterns)

    def _custom_match(self, rel_posix: str, name: str, is_dir: bool) -> bool:
        for pat in self.patterns:
            if pat.dir_only and not is_dir:
                continue
            target = rel_posix if pat.has_slash else name
            if fnmatch.fnmatchcase(target, pat.text):
                return True
        return False

    def entry_ignored(self, rel_posix: str, name: str, is_dir: bool) -> bool:
        """Is this single entry (given its path relative to the root) ignored?
        Ancestors are NOT consulted -- the walk prunes them first."""
        if is_dir:
            if is_ignored_dir_name(name):
                return True
        elif is_ignored_file_name(name):
            return True
        return self._custom_match(rel_posix, name, is_dir)

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


def discover(
    root: Path, *, extensions: frozenset[str] | set[str], enabled: bool = True
) -> DiscoveryReport:
    """Walk `root`, pruning ignored directories. With `enabled=False` nothing
    is ignored (escape hatch: DOCKET_INGEST_IGNORE_ENABLED=false)."""
    root = Path(root)
    rules = IgnoreRules.for_root(root) if enabled else IgnoreRules()
    report = DiscoveryReport(files=[])

    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        rel_prefix = "" if str(rel_dir) == "." else rel_dir.as_posix() + "/"

        kept: list[str] = []
        for d in dirnames:
            if enabled and rules.entry_ignored(rel_prefix + d, d, True):
                report.dirs_skipped += 1
                report.files_skipped += _count_supported(here / d, extensions)
            else:
                kept.append(d)
        dirnames[:] = kept

        for f in filenames:
            if Path(f).suffix.lower() not in extensions:
                continue
            if enabled and rules.entry_ignored(rel_prefix + f, f, False):
                report.files_skipped += 1
                continue
            p = here / f
            if p.is_file():
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
