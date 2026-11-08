"""How much of the library is searchable, and how deeply (tiered indexing).

One cheap grouped count over `evidence_versions` (current lineage slots of
ACTIVE sources). Used by the query layer ("N of M files fully indexed, the
others are keyword-only"), `docket status` and the doctor line.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from docket.core.db.models import SourceStatus

_COUNTS_SQL = text(
    "SELECT evidence_versions.status, COUNT(*) FROM evidence_versions "
    "JOIN sources ON sources.id = evidence_versions.source_id "
    "WHERE sources.status = :active AND evidence_versions.status != 'SUPERSEDED' "
    "GROUP BY evidence_versions.status"
)


def version_counts(engine: Engine) -> dict[str, int]:
    """`{status name: count}` of non-superseded versions of ACTIVE sources."""
    with engine.connect() as conn:
        return {r[0]: int(r[1]) for r in conn.execute(_COUNTS_SQL, {"active": SourceStatus.ACTIVE.name})}


def library_coverage(engine: Engine, *, used_provisional: bool = False) -> dict | None:
    """Coverage dict in the shape the interfaces already understand
    (`indexed`/`total`/`indexing`), plus `keyword_only`; `None` when nothing is
    keyword-only (full coverage needs no statement)."""
    counts = version_counts(engine)
    keyword_only = counts.get("TEXT_READY", 0)
    if not keyword_only:
        return None
    ready = counts.get("READY", 0)
    return {
        "indexed": ready,
        "total": ready + keyword_only,
        "keyword_only": keyword_only,
        "indexing": True,
        "used_provisional": used_provisional,
    }


def coverage_statement(cov: dict) -> str:
    n, m = cov["indexed"], cov["total"]
    return f"{n} of {m} files fully indexed; the other {m - n} are keyword-only so far."
