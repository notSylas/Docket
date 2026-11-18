"""Strict parameter parsing for the worker's ``evidence.get`` (no I/O, no logging).

Raises ``ValueError`` with no message content; callers must not echo values.
"""

from __future__ import annotations

_ALLOWED = {"chunk_id", "allowed_version_ids", "before", "after", "answer_excerpt"}
MAX_ID_CHARS = 200
MAX_VERSION_IDS = 10000
MAX_CONTEXT = 3
MAX_EXCERPT_CHARS = 1000


def _id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_ID_CHARS or "\x00" in value:
        raise ValueError
    return value


def _window(value: object) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_CONTEXT:
        raise ValueError
    return value


def parse_evidence_params(params: dict) -> dict:
    if not set(params) <= _ALLOWED:
        raise ValueError
    ids = params.get("allowed_version_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_VERSION_IDS:
        raise ValueError
    excerpt = params.get("answer_excerpt")
    if excerpt is not None and (not isinstance(excerpt, str) or len(excerpt) > MAX_EXCERPT_CHARS):
        raise ValueError
    return {
        "chunk_id": _id(params.get("chunk_id")),
        "allowed_version_ids": [_id(v) for v in ids],
        "before": _window(params.get("before")),
        "after": _window(params.get("after")),
        "answer_excerpt": excerpt,
    }
