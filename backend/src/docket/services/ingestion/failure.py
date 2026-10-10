"""Short plain-string failure reasons stored on FAILED evidence versions.

Lives in services (not interfaces) so the pipeline can record a reason
without importing the CLI layer; `interfaces/cli/quiet.condense_error`
delegates here so both render identically."""

from __future__ import annotations

import re

MAX_REASON_LEN = 140
_PARSE_PREFIX = re.compile(r"^\s*failed to parse .+? for source \S+?:\s*", re.DOTALL)


def condense_error(error: object) -> str:
    """Collapse a (possibly multi-line, traceback-ish) error to one short
    line: the first meaningful line, whitespace-collapsed and truncated."""
    text = "" if error is None else str(error)
    # `ParseError` wraps the real cause: "failed to parse <path> for source
    # <id>: <cause>". The path is shown separately, so keep only the cause.
    text = _PARSE_PREFIX.sub("", text, count=1)
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = re.sub(r"\s+", " ", first) or "unknown error"
    if len(first) > MAX_REASON_LEN:
        first = first[: MAX_REASON_LEN - 1].rstrip() + "…"
    return first
