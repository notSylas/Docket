"""Quieter, concise ingest output for the interactive and one-shot CLI paths:
one short line per failed file, de-duplicated, and third-party log/progress
noise (RapidOCR, torch, transformers, huggingface_hub) suppressed while
ingesting -- unless DOCKET_DEBUG is set."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from docket.services.ingestion.failure import condense_error as _condense_error

_NOISY_LOGGERS = (
    "RapidOCR",
    "rapidocr",
    "rapidocr_onnxruntime",
    "transformers",
    "huggingface_hub",
    "torch",
    "docling",
    "docling_ibm_models",
    "docling_core",
    "PIL",
)
_NOISY_ENV = {"TQDM_DISABLE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}


def debug_enabled() -> bool:
    return bool(os.environ.get("DOCKET_DEBUG"))


# quiet_ingest is entered from several threads at once in the full-screen UI
# (an indexing worker and a question worker overlap), and both
# `warnings.catch_warnings` and the logger/env tweaks are process-global. A
# reference count makes the first entrant apply them and the last leave restore
# them, so overlapping scopes can never leave stale filters or levels behind.
_ingest_lock = threading.Lock()
_ingest_depth = 0
_ingest_undo: list = []


def _ingest_enter() -> None:
    global _ingest_depth
    with _ingest_lock:
        _ingest_depth += 1
        if _ingest_depth > 1:
            return
        saved_levels: dict[str, int] = {}
        for name in _NOISY_LOGGERS:
            logger = logging.getLogger(name)
            saved_levels[name] = logger.level
            if logger.level < logging.WARNING:  # includes NOTSET (0)
                logger.setLevel(logging.WARNING)
        saved_env = {k: os.environ.get(k) for k in _NOISY_ENV}
        for key, value in _NOISY_ENV.items():
            os.environ.setdefault(key, value)
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        for module in ("torch", "transformers", "huggingface_hub"):
            warnings.filterwarnings("ignore", module=module)
        _ingest_undo[:] = [saved_levels, saved_env, catcher]


def _ingest_exit() -> None:
    global _ingest_depth
    with _ingest_lock:
        _ingest_depth -= 1
        if _ingest_depth > 0 or not _ingest_undo:
            return
        saved_levels, saved_env, catcher = _ingest_undo
        _ingest_undo.clear()
        catcher.__exit__(None, None, None)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        for key, old in saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@contextlib.contextmanager
def quiet_ingest() -> Iterator[None]:
    """Raise noisy third-party loggers to WARNING and disable tqdm/HF
    progress bars for the duration only. No-op when DOCKET_DEBUG is set.
    Re-entrant and safe to enter from several threads at once."""
    if debug_enabled():
        yield
        return
    _ingest_enter()
    try:
        yield
    finally:
        _ingest_exit()


_sink_lock = threading.Lock()
_sink: Any = None


def _null_sink() -> Any:
    """One process-long, never-closed text sink. A library that captures
    ``sys.stderr`` while it is redirected (a logging handler created in a
    worker) keeps working after the redirect ends instead of failing on a
    closed file."""
    global _sink
    with _sink_lock:
        if _sink is None or _sink.closed:
            _sink = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        return _sink


def _stream_handlers() -> list[logging.StreamHandler]:
    registry = logging.Logger.manager.loggerDict
    for _ in range(5):  # another thread may register a logger while we copy
        try:
            known = list(registry.values())
            break
        except RuntimeError:
            continue
    else:
        known = []
    seen: list[logging.StreamHandler] = []
    for lg in [logging.root, *[x for x in known if isinstance(x, logging.Logger)]]:
        for h in list(lg.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) and h not in seen:
                seen.append(h)
    return seen


@contextlib.contextmanager
def quiet_streams(log_path: Path | str | None = None) -> Iterator[None]:
    """Keep library writes off a full-screen terminal UI.

    For the duration, Python-level ``sys.stdout``/``sys.stderr`` (tqdm bars,
    ``print``, warnings) and logging stream handlers that point at the real
    terminal streams go to a null sink -- or, with DOCKET_DEBUG set and a
    ``log_path``, are appended to that file so debugging output survives
    without corrupting the screen. Everything is restored afterwards.

    Limit: this is Python-level only. A native library writing straight to
    file descriptor 1 or 2 is not intercepted (the UI's own output shares fd 1).
    The UI keeps writing through the stream object it captured at start-up.
    """
    sink: Any = _null_sink()
    owned = None
    if debug_enabled() and log_path is not None:
        try:
            owned = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
            sink = owned
        except OSError:
            owned = None
    real_out, real_err = sys.stdout, sys.stderr
    swapped: list[tuple[logging.StreamHandler, Any]] = []
    for h in _stream_handlers():
        try:
            if h.stream in (real_out, real_err, sys.__stdout__, sys.__stderr__):
                swapped.append((h, h.stream))
                h.setStream(sink)
        except Exception:  # noqa: BLE001 -- best effort; never block the UI
            continue
    sys.stdout = sys.stderr = sink
    try:
        yield
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        for h, stream in swapped:
            try:
                h.setStream(stream)
            except Exception:  # noqa: BLE001
                pass
        if owned is not None:
            owned.close()


def condense_error(error: object) -> str:
    """One short line for an error (shared with the stored failure reason)."""
    return _condense_error(error)


def display_path(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            pass
    return str(path)


def ignore_notices(result: object, prune_service: object, source_id: str) -> list[str]:
    """Dim one-liners after an ingest: what discovery skipped (folders and
    not-real-document files), and whether already-indexed files now sit in
    ignored folders (a plain statement; the update removes them, nothing here
    names a command)."""
    from docket.services.ingestion.ignore import invalid_summary, skipped_summary

    skipped = int(getattr(result, "files_skipped", 0) or 0)
    invalid = int(getattr(result, "invalid_skipped", 0) or 0)
    notices: list[str] = [
        line
        for line in (skipped_summary(skipped), invalid_summary(invalid))
        if line is not None
    ]
    try:
        count = int(prune_service.count(source_id))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- a hint must never break ingestion output
        return notices
    if count > 0:
        noun = "file is" if count == 1 else "files are"
        notices.append(
            f"{count} indexed {noun} now in ignored folders and will be removed "
            "from the index in the next update."
        )
    return notices


def stale_recipe_notice(count: int) -> str:
    """Plain statement for files prepared with an older chunk recipe (no
    command name: the update step handles it)."""
    noun = "file was" if count == 1 else "files were"
    return (
        f"{count} already-read {noun} prepared with an older recipe and will be "
        "re-prepared in the next update."
    )


def index_format_notice() -> str:
    """Plain statement for an index that uses an older text format."""
    return (
        "The search index still uses an older text format; it will be rebuilt "
        "in the next update."
    )


class FailureReporter:
    """Formats one concise line per failed file and drops exact repeats."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._seen: set[tuple[str, str]] = set()

    def line(self, path: Path, error: object) -> str | None:
        """`FAILED rel/path.pdf: message`, or None if already reported."""
        shown = display_path(path, self._root)
        message = condense_error(error)
        key = (shown, message)
        if key in self._seen:
            return None
        self._seen.add(key)
        return f"FAILED {shown}: {message}"
