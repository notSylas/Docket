"""Private output helpers for eval artifacts (runs, judged files, reports, label sheets).

Eval outputs hold full prompts, answers and passage text of the user's documents, so every
file the tooling creates is mode 0600 inside a 0700 directory (directories the tool creates;
existing directories are never re-permissioned). `--redact` style helpers strip the
text-bearing fields so an exported copy can be shared as aggregate data only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Any

from docket.eval.schema import RecordedChunk, RunRecord

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
REDACTED = "[redacted]"

# Keys whose values are document/answer text in report or judged dictionaries.
TEXT_KEYS = frozenset(
    {"answer", "prompt", "system", "text", "passage", "passages", "reason", "statement", "question",
     "quote", "quotes", "gold", "cited_evidence", "standalone_query", "retrieved", "agent_trace"}
)


def ensure_private_dir(path: Path | str) -> Path:
    """Create `path` (and parents) if missing; a directory created here is chmod 0700.
    An already existing directory is left untouched."""
    path = Path(path)
    if path.is_dir():
        return path
    missing = [p for p in (path, *path.parents) if not p.exists()]
    path.mkdir(parents=True, exist_ok=True)
    for created in missing:
        # only the leaf is guaranteed private; parents created on the way are tightened too
        os.chmod(created, PRIVATE_DIR_MODE)
    return path


def open_private(path: Path | str, *, overwrite: bool = True, binary: bool = False) -> IO[Any]:
    """Open for writing a file that is mode 0600 (created with that mode, and tightened if it
    already existed). `overwrite=False` is create-only (raises FileExistsError)."""
    path = Path(path)
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    os.fchmod(fd, PRIVATE_FILE_MODE)
    return os.fdopen(fd, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"}))


def write_private_text(path: Path | str, text: str, *, overwrite: bool = True) -> None:
    with open_private(path, overwrite=overwrite) as fh:
        fh.write(text)


# -- redaction ----------------------------------------------------------------------------


def redact_record(record: RunRecord) -> RunRecord:
    """Copy of `record` with prompt/answer/passage text removed; ids, citations' chunk ids,
    verdict-relevant flags, timings and fingerprints are kept."""
    data = record.model_copy(deep=True)
    data.answer = REDACTED if record.answer else ""
    data.prompt = None
    data.system = None
    data.standalone_query = None
    data.agent_trace = []
    data.model_calls = []
    data.claim_records = None
    data.compute = None
    data.ambiguity = None
    data.retrieved = [RecordedChunk.model_validate({**c.model_dump(), "text": ""}) for c in record.retrieved]
    return data


def redact_dict(value: Any) -> Any:
    """Recursively replace values under text-bearing keys in a report/judged dictionary."""
    if isinstance(value, dict):
        return {k: (REDACTED if k in TEXT_KEYS and v else redact_dict(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_dict(v) for v in value]
    return value
