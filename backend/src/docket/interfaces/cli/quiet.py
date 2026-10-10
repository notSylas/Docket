"""Quieter, concise ingest output for the interactive and one-shot CLI paths:
one short line per failed file, de-duplicated, and third-party log/progress
noise (RapidOCR, torch, transformers, huggingface_hub) suppressed while
ingesting -- unless DOCKET_DEBUG is set."""

from __future__ import annotations

import contextlib
import logging
import os
import warnings
from collections.abc import Iterator
from pathlib import Path

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


@contextlib.contextmanager
def quiet_ingest() -> Iterator[None]:
    """Raise noisy third-party loggers to WARNING and disable tqdm/HF
    progress bars for the duration only. No-op when DOCKET_DEBUG is set."""
    if debug_enabled():
        yield
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
    try:
        with warnings.catch_warnings():
            for module in ("torch", "transformers", "huggingface_hub"):
                warnings.filterwarnings("ignore", module=module)
            yield
    finally:
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        for key, old in saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


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
    """Dim one-liners after an ingest: what discovery skipped, or (when it
    skipped nothing) that already-indexed files are now in ignored folders."""
    from docket.services.ingestion.ignore import invalid_summary, skipped_summary

    skipped = int(getattr(result, "files_skipped", 0) or 0)
    invalid = int(getattr(result, "invalid_skipped", 0) or 0)
    lines = [
        line
        for line in (skipped_summary(skipped), invalid_summary(invalid))
        if line is not None
    ]
    if lines:
        return lines
    try:
        count = int(prune_service.count(source_id))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- a hint must never break ingestion output
        return []
    if count > 0:
        noun = "file is" if count == 1 else "files are"
        return [
            f"{count} indexed {noun} now in ignored folders; "
            f"run `docket sources prune {source_id}` to remove them."
        ]
    return []


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
