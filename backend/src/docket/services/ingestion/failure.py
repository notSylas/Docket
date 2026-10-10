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


# -- plain-language reasons ---------------------------------------------------

_LEGACY = {
    ".doc": ("Word", ".docx"),
    ".xls": ("Excel", ".xlsx"),
    ".xlsm": ("Excel (macro-enabled)", ".xlsx"),
    ".ppt": ("PowerPoint", ".pptx"),
}

_PASSWORD = re.compile(r"password|encrypt|decrypt|protected", re.I)
_CORRUPT = re.compile(
    r"corrupt|damaged|bad ?zip|not a zip|not a valid|invalid|malformed|truncated|"
    r"unexpected end|end of file|\beof\b|cannot (?:be )?(?:read|opened|open|parsed)|"
    r"could not (?:be )?(?:read|opened|parse)|unreadable|failed to (?:read|open)|xref|"
    r"badly formed|no /root",
    re.I,
)
_EMPTY = re.compile(r"\bempty\b|no (?:text|content|pages)|zero[- ]byte|0 bytes", re.I)


def plain_failure_reason(reason: object, path: object = None) -> str:
    """One plain sentence for a stored failure reason, with what to do.

    Maps the common causes (password-protected, an old Office format,
    unreadable or damaged, empty); anything else keeps the stored one-line
    text so nothing is hidden. `path` (the file name) lets an old format be
    recognised from its extension even when the stored text is terse."""
    suffix = ""
    if path is not None:
        name = str(path)
        dot = name.rfind(".")
        suffix = name[dot:].lower() if dot > name.rfind("/") else ""
    text = "" if reason is None else str(reason).strip()
    if suffix in _LEGACY and (not text or "unsupported" in text.lower() or "format" in text.lower()):
        family, modern = _LEGACY[suffix]
        return (
            f"An old {family} format ({suffix}) that Docket cannot read. "
            f"Save a copy as {modern} and it will be picked up."
        )
    if not text:
        return "Docket could not read this file. Retry it, or ignore it if you do not need it."
    low = text.lower()
    if _PASSWORD.search(low):
        return "Password-protected. Remove the password and save a copy, or ignore this file."
    if "unsupported file format" in low or "legacy" in low:
        return "A file format Docket cannot read yet. Save a copy as .docx, .xlsx or .pptx."
    if _EMPTY.search(low):
        return "The file is empty, so there is nothing to read."
    if _CORRUPT.search(low):
        return "The file looks damaged or unreadable. Open it, save a fresh copy, then retry."
    return text  # an unrecognised cause keeps its stored wording; nothing is hidden
