"""Tiered indexing driver: Tier 1 (quick keyword pass) then Tier 2 (full index)
over the persisted work queue (`docket.services.ingestion.queue`).

Responsibilities kept here, not in the pipeline: choosing what to run next,
pausing for questions, stopping, retry bookkeeping and progress. The pipeline
methods it calls (`ingest_tier1_path`, `ingest_tier2_path`) each take the
writer lock for ONE file, so a queue that runs for hours never starves other
writers, and every file boundary is a safe stop/yield point.

Yielding: `pause()` is called between files, and (via
`IndexManager.batch_hook`) between embedding batches inside a file. The TUI
passes `lambda: gate.wait_clear(give_up=stop_requested)` from its existing
`YieldGate`, so a question pauses deep indexing exactly like a one-pass run.
Tier 1 is cheap enough to run first and to completion for the whole queue;
whenever a Tier 1 job is claimable it goes before any Tier 2 job (new and
changed files become keyword-searchable before older files are deepened).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from docket.services.ingestion.coverage import library_coverage, version_counts
from docket.services.ingestion.failure import condense_error
from docket.services.ingestion.queue import (
    DEAD,
    DONE,
    QUEUED,
    RUNNING,
    TIER1,
    TIER2,
    IngestQueue,
    QueueJob,
)

logger = logging.getLogger(__name__)

# Tier-1 results that finish the job (nothing further to do for the tier).
_TIER1_OK = {"ingested", "unchanged", "no_fast_path", "skipped", "deleted"}


@dataclass
class TierRunSummary:
    tier1_done: int = 0
    tier2_done: int = 0
    retried: int = 0
    dead: int = 0
    failed_paths: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False
    elapsed_s: float = 0.0
    first_searchable_s: float | None = None  # first Tier-1 file made keyword-searchable


class TieredIndexer:
    def __init__(
        self,
        *,
        pipeline,
        queue: IngestQueue,
        pause: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        progress: Callable[[str, QueueJob, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pipeline = pipeline
        self._queue = queue
        self._pause = pause or (lambda: None)
        self._stop = should_stop or (lambda: False)
        self._progress = progress
        self._clock = clock
        self._ctx: dict[str, tuple] = {}

    # -- enqueue ---------------------------------------------------------------

    def enqueue_source(
        self, source_id: str, *, touched: list[Path] = (), tiers: tuple[int, ...] = (TIER1, TIER2)
    ) -> dict[str, int]:
        """Discover the source's files and add/refresh their jobs."""
        source, root, rules = self._pipeline.tier_context(source_id)
        report = self._pipeline._discover(root, getattr(source, "rules_json", None))
        return self._queue.enqueue(source_id, report.files, tiers=tiers, touched=touched)

    def enqueue_paths(self, source_id: str, paths: list[Path], **kw) -> dict[str, int]:
        return self._queue.enqueue(source_id, paths, **kw)

    # -- run -------------------------------------------------------------------

    def run(
        self,
        source_id: str | None = None,
        *,
        tier1: bool = True,
        tier2: bool = True,
        max_files: int | None = None,
    ) -> TierRunSummary:
        """Process claimable jobs until none are left (or stop). Jobs waiting
        out a retry backoff are left queued; the next run picks them up."""
        summary = TierRunSummary()
        started = self._clock()
        self._queue.recover()
        self._pipeline._index_manager.batch_hook = self._pause_checked
        touched_sources: set[str] = set()
        processed = 0
        try:
            while not self._stop():
                if max_files is not None and processed >= max_files:
                    break
                self._pause()
                if self._stop():
                    break
                job = self._queue.claim(TIER1, source_id) if tier1 else None
                if job is None and tier2:
                    job = self._queue.claim(TIER2, source_id)
                if job is None:
                    break
                processed += 1
                ok = self._run_job(job, summary, started)
                if ok and job.tier == TIER2:
                    touched_sources.add(job.source_id)
                    self._pipeline.note_file_done()
            summary.stopped = self._stop()
        finally:
            self._pipeline._index_manager.batch_hook = None
            self._pipeline.reset_parse_pool()
            for sid in touched_sources:
                try:
                    self._pipeline.finish_source_pass(sid)
                except Exception:  # noqa: BLE001 -- repair pass is best effort
                    logger.warning("end-of-pass reconcile failed", exc_info=True)
            summary.elapsed_s = self._clock() - started
        return summary

    def _pause_checked(self) -> None:
        self._pause()

    def _context(self, source_id: str):
        ctx = self._ctx.get(source_id)
        if ctx is None:
            ctx = self._ctx[source_id] = self._pipeline.tier_context(source_id)
        return ctx

    def _run_job(self, job: QueueJob, summary: TierRunSummary, started: float) -> bool:
        try:
            _source, root, rules = self._context(job.source_id)
            if job.tier == TIER2:
                self._pipeline.prefetch_paths(
                    job.source_id, [job.path, *self._queue.peek(TIER2, 4, job.source_id)]
                )
                result = self._pipeline.ingest_tier2_path(job.source_id, job.path, root, rules)
            else:
                result = self._pipeline.ingest_tier1_path(job.source_id, job.path, root, rules)
            if job.tier == TIER1 and result.status not in _TIER1_OK:
                raise RuntimeError(f"unexpected tier-1 result {result.status!r}")
        except Exception as exc:  # noqa: BLE001 -- one bad file never stops the queue
            self._ctx.pop(job.source_id, None)  # re-resolve (root may have gone away)
            err = condense_error(exc)
            state = self._queue.fail(job, err)
            if state == DEAD:
                summary.dead += 1
            else:
                summary.retried += 1
            summary.failed_paths.append((str(job.path), err))
            if self._progress:
                self._progress("failed", job, err)
            return False
        self._queue.complete(job)
        if job.tier == TIER1:
            summary.tier1_done += 1
            if result.status == "ingested" and summary.first_searchable_s is None:
                summary.first_searchable_s = self._clock() - started
        else:
            summary.tier2_done += 1
        if self._progress:
            self._progress("done", job, result.status)
        return True


# -- status -----------------------------------------------------------------

_RATE_WINDOW_S = 900.0


def payload_from_dbapi(conn, *, now: float | None = None, source_id: str | None = None) -> dict:
    """Status payload from a plain DB-API (sqlite3) connection, so it can run
    read-only (doctor) or from a SQLAlchemy raw connection.

    `versions` counts non-superseded versions of ACTIVE sources by lifecycle
    state; `queue` counts jobs by tier and state; `rate_files_per_s` is jobs
    done over the wall-clock span they occupied in the last ~15 minutes
    (None until two jobs of the tier have finished); `eta_s` is remaining jobs
    over that rate (None while unknown, 0.0 when nothing is left)."""
    now = time.time() if now is None else now
    cur = conn.cursor()
    cur.execute(
        "SELECT v.status, COUNT(*) FROM evidence_versions v JOIN sources s ON s.id = v.source_id "
        "WHERE s.status = 'ACTIVE' AND v.status != 'SUPERSEDED'"
        + (" AND v.source_id = ?" if source_id else "")
        + " GROUP BY v.status",
        (source_id,) if source_id else (),
    )
    counts = {r[0]: int(r[1]) for r in cur.fetchall()}
    q = {t: {QUEUED: 0, RUNNING: 0, DONE: 0, DEAD: 0} for t in (TIER1, TIER2)}
    cur.execute(
        "SELECT tier, state, COUNT(*) FROM ingest_queue"
        + (" WHERE source_id = ?" if source_id else "")
        + " GROUP BY tier, state",
        (source_id,) if source_id else (),
    )
    for tier, state, n in cur.fetchall():
        q.setdefault(tier, {QUEUED: 0, RUNNING: 0, DONE: 0, DEAD: 0})[state] = int(n)
    out: dict = {
        "versions": {
            "ready": counts.get("READY", 0),
            "keyword_only": counts.get("TEXT_READY", 0),
            "pending": counts.get("PENDING", 0),
            "failed": counts.get("FAILED", 0),
        },
        "queue": {
            f"tier{t}": {
                "queued": q[t][QUEUED], "running": q[t][RUNNING], "done": q[t][DONE], "dead": q[t][DEAD],
            }
            for t in (TIER1, TIER2)
        },
        "rate_files_per_s": {},
        "eta_s": {},
    }
    for t in (TIER1, TIER2):
        cur.execute(
            "SELECT started_at, finished_at FROM ingest_queue WHERE tier = ? AND state = 'done' "
            "AND finished_at >= ? AND started_at IS NOT NULL ORDER BY finished_at DESC LIMIT 200",
            (t, now - _RATE_WINDOW_S),
        )
        rows = cur.fetchall()
        rate = None
        if len(rows) >= 2:
            span = max(r[1] for r in rows) - min(r[0] for r in rows)
            rate = len(rows) / span if span > 0 else None
        remaining = q[t][QUEUED] + q[t][RUNNING]
        out["rate_files_per_s"][f"tier{t}"] = rate
        out["eta_s"][f"tier{t}"] = 0.0 if not remaining else (remaining / rate if rate else None)
    return out


def tier_status(engine, queue: IngestQueue, source_id: str | None = None) -> dict:
    """Status payload for the CLI, the TUI and any daemon status call: counts
    per state, ETAs from measured recent speeds, coverage and the newest
    dead-lettered files."""
    raw = engine.raw_connection()
    try:
        out = payload_from_dbapi(raw, now=queue._clock(), source_id=source_id)
    finally:
        raw.close()
    out["coverage"] = library_coverage(engine)
    out["dead_letters"] = queue.dead_letters(source_id, 5)
    return out


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "estimating"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def status_line(payload: dict) -> str:
    """One human line for `docket status` / doctor."""
    v, q = payload["versions"], payload["queue"]
    total = v["ready"] + v["keyword_only"] + v["pending"] + v["failed"]
    if total == 0 and not any(q[t][s] for t in q for s in ("queued", "running", "dead")):
        return "nothing indexed yet"
    parts = [f"{v['ready']} of {total} files fully indexed"]
    if v["keyword_only"]:
        parts.append(f"{v['keyword_only']} keyword-only")
    if v["pending"]:
        parts.append(f"{v['pending']} waiting")
    if v["failed"]:
        parts.append(f"{v['failed']} failed")
    dead = q["tier1"]["dead"] + q["tier2"]["dead"]
    if dead:
        parts.append(f"{dead} gave up after retries")
    todo1 = q["tier1"]["queued"] + q["tier1"]["running"]
    todo2 = q["tier2"]["queued"] + q["tier2"]["running"]
    if todo1:
        parts.append(f"quick pass: {todo1} left, ETA {_fmt_eta(payload['eta_s']['tier1'])}")
    if todo2:
        parts.append(f"full index: {todo2} left, ETA {_fmt_eta(payload['eta_s']['tier2'])}")
    return "; ".join(parts)
