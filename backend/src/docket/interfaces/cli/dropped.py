"""Recognise a pasted or dragged path.

A terminal pastes a dragged file or folder as text: its path, often quoted or
with backslash-escaped spaces, sometimes a `file://` URI with percent
escapes. `parse_dropped_path` decides whether one line of input is such a
path to something that exists, in the same spirit as `shell_command_hint`:
anything that could be a question stays a question (a sentence that merely
contains a path, a bare word that happens to be a folder name).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_LENGTH = 4096
_QUOTES = ("'", '"')


@dataclass(frozen=True)
class DroppedPath:
    path: str  # absolute, normalised
    kind: str  # "dir" or "file"

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"

    @property
    def parent(self) -> str:
        return str(Path(self.path).parent)


def _uri_to_path(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
        return None
    return unquote(parts.path) or None


def _variants(raw: str) -> list[str]:
    out: list[str] = []
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = []
    if len(tokens) == 1:
        out.append(tokens[0])
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in _QUOTES:
        out.append(raw[1:-1])
    out.append(raw)  # an unquoted, unescaped path that has spaces in it
    return out


def _looks_like_path(value: str) -> bool:
    return value.startswith(("/", "~", "./", "../"))


def parse_dropped_path(text: str, *, cwd: str | os.PathLike[str] | None = None) -> DroppedPath | None:
    """The existing folder or file `text` names, or None.

    `text` must be one line. Relative paths count only with an explicit `./`
    or `../` (a bare word is never a path here)."""
    raw = (text or "").strip()
    if not raw or len(raw) > MAX_LENGTH or "\n" in raw or "\r" in raw:
        return None
    if any(ord(c) < 32 for c in raw):
        return None
    for candidate in _variants(raw):
        if candidate.lower().startswith("file://"):
            converted = _uri_to_path(candidate)
            if converted is None:
                continue
            candidate = converted
        if not _looks_like_path(candidate):
            continue
        try:
            expanded = os.path.expanduser(candidate)
            if not os.path.isabs(expanded):
                base = os.fspath(cwd) if cwd is not None else os.getcwd()
                expanded = os.path.join(base, expanded)
            normal = os.path.normpath(expanded)
            if os.path.isdir(normal):
                return DroppedPath(normal, "dir")
            if os.path.isfile(normal):
                return DroppedPath(normal, "file")
        except (OSError, ValueError):
            continue
    return None


FILE_OFFER = "Docket works with folders - use its folder?"


__all__ = ["DroppedPath", "FILE_OFFER", "parse_dropped_path"]
