#!/usr/bin/env python3
"""Reusable helper for the Phase 7 package regroup (and any later, similar
layout change): rewrites `from docket.<old>...` / `import docket.<old>...`
statement lines to a new dotted prefix, across `backend/src` and
`backend/tests`.

Deliberately narrow in scope: it only touches lines that are actual
`from`/`import` statements. Prose that happens to mention a dotted path
(docstrings, comments, `pyproject.toml`, non-Python config) is untouched by
design -- those get fixed by hand in a separate cleanup step, since a regex
can't reliably tell "this comment is now stale" from "this comment is still
accurate".

Usage
-----
    python scripts/migrate_layout.py OLD:NEW [OLD:NEW ...] [--dry-run]

Example (Phase 7, move 1):

    python scripts/migrate_layout.py \\
        docket.config:docket.core.config \\
        docket.db:docket.core.db \\
        --dry-run

Each OLD:NEW pair is a dotted-prefix mapping, e.g. `docket.db:docket.core.db`.
Matching is prefix-aware and word-boundary-safe: `docket.db` matches
`docket.db`, `docket.db.models`, and `docket.db as db_pkg`, but will NOT
match an unrelated package that merely starts with the same characters, like
a hypothetical `docket.database_thing`.

Re-running the script after it has already applied a mapping is a no-op for
that mapping (the rewritten lines no longer start with the old prefix), so
it's safe to invoke again in a later phase with a different mapping without
re-touching already-migrated imports.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Directories we never walk into, regardless of which roots are requested.
_ALWAYS_SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules"}


@dataclass(frozen=True)
class Mapping:
    old: str
    new: str


@dataclass(frozen=True)
class Change:
    path: Path
    lineno: int
    old_line: str
    new_line: str


def parse_mapping(spec: str) -> Mapping:
    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            f"mapping {spec!r} must be of the form OLD_PREFIX:NEW_PREFIX"
        )
    old, new = spec.split(":", 1)
    old, new = old.strip(), new.strip()
    if not old or not new:
        raise argparse.ArgumentTypeError(
            f"mapping {spec!r} must have non-empty OLD_PREFIX and NEW_PREFIX"
        )
    return Mapping(old=old, new=new)


def _boundary_pattern(prefix: str) -> str:
    """A regex fragment matching `prefix` only when NOT followed by another
    identifier character -- so `docket.db` matches `docket.db.models` and
    `docket.db` but not `docket.database_thing`."""
    return re.escape(prefix) + r"(?![A-Za-z0-9_])"


def rewrite_line(line: str, mappings: list[Mapping]) -> str:
    """Rewrite a single line's `from`/`import` module path(s), if any of the
    mappings' old prefixes appear in the statement position.

    Handles:
      - `from <old_prefix>... import X`  (only one module path possible)
      - `import <old_prefix>...`         (possibly several, comma-separated,
                                           each optionally with `as alias`)
    """
    from_match = re.match(r"^(\s*from\s+)(\S+)(\s+import\b.*)$", line)
    if from_match:
        head, module, tail = from_match.groups()
        for m in mappings:
            pattern = re.compile(_boundary_pattern(m.old))
            new_module, n = pattern.subn(m.new, module, count=1)
            if n:
                return head + new_module + tail
        return line

    import_match = re.match(r"^(\s*import\s+)(.*)$", line)
    if import_match:
        head, rest = import_match.groups()
        # A bare `import a.b, c.d as e` statement: split on top-level commas
        # (plain import statements don't contain parens/brackets) and
        # rewrite each dotted-module token independently.
        parts = rest.split(",")
        changed = False
        new_parts = []
        for part in parts:
            leading_ws = part[: len(part) - len(part.lstrip())]
            body = part.strip()
            for m in mappings:
                pattern = re.compile(_boundary_pattern(m.old))
                new_body, n = pattern.subn(m.new, body, count=1)
                if n:
                    body = new_body
                    changed = True
                    break
            new_parts.append(leading_ws + body)
        if changed:
            return head + ",".join(new_parts)
        return line

    return line


def iter_python_files(
    roots: list[Path], *, skip_scripts: bool, self_path: Path
) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == self_path:
                continue
            parts = set(path.parts)
            if parts & _ALWAYS_SKIP_DIRS:
                continue
            if skip_scripts and "scripts" in parts:
                continue
            files.append(path)
    return files


def process_file(path: Path, mappings: list[Mapping]) -> tuple[list[Change], str]:
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    changes: list[Change] = []
    new_lines: list[str] = []
    for i, line in enumerate(lines, start=1):
        # Preserve line endings while matching/rewriting the content.
        stripped_end = ""
        content = line
        for ending in ("\r\n", "\n", "\r"):
            if line.endswith(ending):
                stripped_end = ending
                content = line[: -len(ending)]
                break
        new_content = rewrite_line(content, mappings)
        if new_content != content:
            changes.append(
                Change(path=path, lineno=i, old_line=content, new_line=new_content)
            )
        new_lines.append(new_content + stripped_end)
    return changes, "".join(new_lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite `from docket.<old>` / `import docket.<old>` statement "
            "lines to a new dotted prefix, across backend/src and "
            "backend/tests."
        )
    )
    parser.add_argument(
        "mappings",
        nargs="+",
        type=parse_mapping,
        metavar="OLD_PREFIX:NEW_PREFIX",
        help="e.g. docket.db:docket.core.db (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without writing any files",
    )
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="path to the backend/ directory (default: parent of scripts/)",
    )
    parser.add_argument(
        "--roots",
        nargs="+",
        default=["src", "tests"],
        help="directories, relative to --backend-root, to walk (default: src tests)",
    )
    parser.add_argument(
        "--include-scripts",
        action="store_true",
        help=(
            "also rewrite files under a 'scripts' directory. Off by default: "
            "this script's own invocations pass dotted paths as CLI "
            "arguments/data, not as real imports of the moved packages, so "
            "scripts/ should not be rewritten as a side effect."
        ),
    )
    args = parser.parse_args(argv)

    backend_root: Path = args.backend_root.resolve()
    roots = [backend_root / r for r in args.roots]
    self_path = Path(__file__).resolve()

    files = iter_python_files(
        roots, skip_scripts=not args.include_scripts, self_path=self_path
    )

    total_changes = 0
    touched_files = 0
    for path in files:
        changes, new_text = process_file(path, args.mappings)
        if not changes:
            continue
        touched_files += 1
        total_changes += len(changes)
        rel = path.relative_to(backend_root) if backend_root in path.parents else path
        print(f"{rel}:")
        for c in changes:
            print(f"  L{c.lineno}: - {c.old_line}")
            print(f"  L{c.lineno}: + {c.new_line}")
        if not args.dry_run:
            path.write_text(new_text, encoding="utf-8")

    mode = "DRY RUN -- no files written" if args.dry_run else "APPLIED"
    print(f"\n[{mode}] {total_changes} line(s) changed across {touched_files} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
