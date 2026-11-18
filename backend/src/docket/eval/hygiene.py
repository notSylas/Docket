"""Eval hygiene guards: library integrity and gold/benchmark binding.

* `servable_source_duplicates` / `require_one_source_per_document`: a scale
  library must present each contract once (one servable source per distinct
  content hash). Read-only SQLite access.
* `verify_gold_binding`: records/judgments must be bound to the gold set (and
  frozen benchmark) being reported on.

The scale-driver import-path guard lives next to the drivers
(`evals/suites/scale/scale_guard.py`) because it must run before `docket` is
imported from a possibly wrong checkout.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from docket.eval.schema import GoldSet, GoldSetError, RunRecord, fingerprint

SERVABLE_SOURCE_STATUS = "ACTIVE"
SERVABLE_VERSION_STATUSES = ("READY", "TEXT_READY")


class LibraryHygieneError(RuntimeError):
    pass


@dataclass(frozen=True)
class DuplicateContent:
    content_hash: str
    source_ids: tuple[str, ...]


def servable_source_duplicates(conn: sqlite3.Connection) -> list[DuplicateContent]:
    """Content hashes presented by more than one non-revoked (ACTIVE) source
    through a servable version. Case-insensitive on status names."""
    marks = ",".join("?" for _ in SERVABLE_VERSION_STATUSES)
    rows = conn.execute(
        "SELECT ev.content_hash, s.id FROM evidence_versions ev JOIN sources s ON s.id = ev.source_id "
        f"WHERE upper(s.status) = ? AND upper(ev.status) IN ({marks}) "
        "GROUP BY ev.content_hash, s.id",
        (SERVABLE_SOURCE_STATUS, *SERVABLE_VERSION_STATUSES),
    ).fetchall()
    by_hash: dict[str, list[str]] = {}
    for content_hash, source_id in rows:
        by_hash.setdefault(content_hash, []).append(source_id)
    return [
        DuplicateContent(h, tuple(sorted(ids))) for h, ids in sorted(by_hash.items()) if len(ids) > 1
    ]


def library_stats(conn: sqlite3.Connection) -> dict:
    """Library size and sources-per-document stats over servable evidence."""
    marks = ",".join("?" for _ in SERVABLE_VERSION_STATUSES)
    params = (SERVABLE_SOURCE_STATUS, *SERVABLE_VERSION_STATUSES)
    per_hash = [
        r[0]
        for r in conn.execute(
            "SELECT COUNT(DISTINCT s.id) FROM evidence_versions ev JOIN sources s ON s.id = ev.source_id "
            f"WHERE upper(s.status) = ? AND upper(ev.status) IN ({marks}) GROUP BY ev.content_hash",
            params,
        )
    ]
    total_sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    return {
        "sources_total": total_sources,
        "servable_documents": len(per_hash),
        "sources_per_document_max": max(per_hash, default=0),
        "sources_per_document_mean": (sum(per_hash) / len(per_hash)) if per_hash else 0.0,
        "documents_in_multiple_sources": sum(1 for n in per_hash if n > 1),
    }


def require_one_source_per_document(db_path: str | Path) -> dict:
    """Open `db_path` read-only; raise LibraryHygieneError when any contract is
    presented by more than one servable source. Returns the library stats."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        dups = servable_source_duplicates(conn)
        stats = library_stats(conn)
    finally:
        conn.close()
    if dups:
        sample = "; ".join(f"{d.content_hash[:12]} in {', '.join(d.source_ids)}" for d in dups[:3])
        raise LibraryHygieneError(
            f"{len(dups)} contract(s) are presented by more than one servable source ({sample}); "
            "revoke the duplicates (see cuad_query_clean.py) before measuring"
        )
    return stats


def verify_gold_binding(
    gold: GoldSet, records: list[RunRecord], *, benchmark_fingerprint: str | None = None
) -> None:
    """Fail when recorded runs were produced against a different gold set (or
    benchmark) than the one being reported. Records without a fingerprint
    (older files) are not rejected here; callers may report how many are unbound."""
    digest = fingerprint(gold)
    bad = [r for r in records if r.gold_fingerprint is not None and r.gold_fingerprint != digest]
    if bad:
        raise GoldSetError(
            f"{len(bad)} run(s) were recorded against a different gold set "
            f"(e.g. {bad[0].question_id}#{bad[0].repeat}); gold changed since the run"
        )
    if benchmark_fingerprint is not None:
        wrong = [r for r in records if r.benchmark_fingerprint not in (None, benchmark_fingerprint)]
        if wrong:
            raise GoldSetError(f"{len(wrong)} run(s) do not match the frozen benchmark")


def unbound_records(records: list[RunRecord]) -> int:
    return sum(r.gold_fingerprint is None for r in records)
