"""Deterministic identifier follow-up retrieval.

After the first hybrid search, identifiers named in the question (or referenced
by the top retrieved chunks) whose own chunk was not retrieved get one exact
phrase lookup in the lexical index. The extra chunks are appended AFTER the
original candidates, so the ranking of the originals is unchanged and the
prompt fitter drops them first if the budget is tight. No model call.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Engine, bindparam, text

from docket.core.db.models import SourceStatus, VersionStatus

_ID_RES = (
    re.compile(r"\b[A-Z]{1,4}-\d+\b"),
    re.compile(r"\bClauses?\s+\d+(?:\.\d+)*", re.IGNORECASE),
    re.compile(r"\bSection\s+\d+(?:\.\d+)*", re.IGNORECASE),
)
_WORD_RE = re.compile(r"\w+")

_BASE = (
    "SELECT fts_chunks.chunk_id FROM fts_chunks "
    "JOIN chunks ON chunks.id = fts_chunks.chunk_id "
    "JOIN sources ON sources.id = chunks.source_id "
    "JOIN evidence_versions ON evidence_versions.id = chunks.evidence_version_id "
    "WHERE fts_chunks MATCH :query "
    "AND sources.status = :active_status "
    "AND evidence_versions.status = :ready_status "
)
_SQL = text(_BASE + "ORDER BY bm25(fts_chunks) LIMIT :limit")
_SQL_SCOPED = text(
    _BASE + "AND evidence_versions.id IN :scope_version_ids "
    "ORDER BY bm25(fts_chunks) LIMIT :limit"
).bindparams(bindparam("scope_version_ids", expanding=True))


def extract_identifiers(text_: str) -> list[str]:
    """Identifier-like tokens in first-seen order, normalised (single spaces,
    'clauses' -> 'clause'), de-duplicated case-insensitively."""
    found: list[tuple[int, str]] = []
    for rx in _ID_RES:
        for m in rx.finditer(text_):
            ident = " ".join(m.group(0).split())
            if ident.lower().startswith("clauses "):
                ident = "Clause " + ident[8:]
            found.append((m.start(), ident))
    seen: set[str] = set()
    out: list[str] = []
    for _, ident in sorted(found):
        if ident.lower() not in seen:
            seen.add(ident.lower())
            out.append(ident)
    return out


def _norm(s: str) -> str:
    return " ".join(_WORD_RE.findall(s.lower()))


def phrase_search(
    engine: Engine, phrase: str, limit: int, scope_version_ids: list[str] | None = None
) -> list[str]:
    words = _WORD_RE.findall(phrase.lower())
    if not words:
        return []
    params: dict[str, Any] = {
        "query": '"' + " ".join(words) + '"',
        "limit": limit,
        "active_status": SourceStatus.ACTIVE.name,
        "ready_status": VersionStatus.READY.name,
    }
    statement = _SQL
    if scope_version_ids is not None:
        if not scope_version_ids:
            return []
        statement = _SQL_SCOPED
        params["scope_version_ids"] = list(scope_version_ids)
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(statement, params)]


def followup_chunk_ids(
    engine: Engine,
    question: str,
    retrieved: list[tuple[str, str]],
    *,
    per_id: int = 2,
    max_total: int = 6,
    inspect_top: int = 5,
    scope_version_ids: list[str] | None = None,
) -> list[str]:
    """Extra chunk ids for identifiers that lack their own retrieved chunk.

    `retrieved` is (chunk_id, text) in rank order. A question identifier is
    unresolved when no retrieved chunk contains it; an identifier only
    referenced by the top chunks is unresolved when just one retrieved chunk
    contains it (a mention without its defining row)."""
    norm_texts = [_norm(t) for _, t in retrieved]

    def occurrences(ident: str) -> int:
        n = _norm(ident)
        return sum(1 for t in norm_texts if f" {n} " in f" {t} ")

    candidates: list[tuple[str, int]] = []  # (identifier, required occurrences)
    seen: set[str] = set()
    for ident in extract_identifiers(question):
        seen.add(ident.lower())
        candidates.append((ident, 1))
    for _, t in retrieved[:inspect_top]:
        for ident in extract_identifiers(t):
            if ident.lower() not in seen:
                seen.add(ident.lower())
                candidates.append((ident, 2))

    have = {cid for cid, _ in retrieved}
    extras: list[str] = []
    for ident, need in candidates:
        if len(extras) >= max_total:
            break
        if occurrences(ident) >= need:
            continue
        added = 0
        for cid in phrase_search(engine, ident, per_id + len(have), scope_version_ids):
            if cid in have or cid in extras:
                continue
            extras.append(cid)
            added += 1
            if added >= per_id or len(extras) >= max_total:
                break
    return extras
