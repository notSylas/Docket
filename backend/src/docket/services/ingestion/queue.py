"""Persisted, prioritised, resumable work queue for tiered indexing.

One row per (source, file, tier): tier 1 = quick keyword pass (TEXT_READY),
tier 2 = full indexing (READY). Every state change is committed on its own, so
a killed process loses at most the file it was working on, and that file is
simply claimed again (its version-level work is idempotent; see
`IngestionPipeline`).

States::

    queued --claim--> running --complete--> done
       ^                 |
       |   fail (attempts < max): back to queued, not_before = now + backoff
       +-----------------+
                         |  fail (attempts >= max)
                         +-----------------------> dead   (dead letter)

`recover()` returns rows left `running` by a dead process to `queued` and
counts the interrupted attempt (so a file that kills the process every time,
e.g. an out-of-memory parse, ends in `dead` instead of looping forever).

Claim order (configurable through `QueuePolicy`):
  1. `prio_class`: 0 = user-touched folders/files (`boost`), 1 = modified in
     the last `recent_days`, 2 = everything else;
  2. recency: calendar day of modification, newest first (a static column, so
     the claim is an index walk, not a sort of the whole queue);
  3. size ascending (small files first: they finish, and become useful, fastest);
  4. mtime, id (deterministic tie-break).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import _new_id

QUEUED, RUNNING, DONE, DEAD = "queued", "running", "done", "dead"
TIER1, TIER2 = 1, 2
PRIO_TOUCHED, PRIO_RECENT, PRIO_REST = 0, 1, 2
_DAY = 86400.0


@dataclass(frozen=True)
class QueuePolicy:
    recent_days: float = 14.0
    max_attempts: int = 3
    backoff_base_s: float = 30.0
    backoff_cap_s: float = 3600.0

    def backoff(self, attempts: int) -> float:
        return min(self.backoff_cap_s, self.backoff_base_s * (2 ** max(0, attempts - 1)))

    @classmethod
    def from_settings(cls, settings) -> "QueuePolicy":
        return cls(
            recent_days=getattr(settings, "tier_recent_days", cls.recent_days),
            max_attempts=getattr(settings, "tier_max_attempts", cls.max_attempts),
            backoff_base_s=getattr(settings, "tier_backoff_s", cls.backoff_base_s),
        )


@dataclass(frozen=True)
class QueueJob:
    id: str
    source_id: str
    path: Path
    tier: int
    attempts: int
    prio_class: int
    byte_size: int


_ORDER = "ORDER BY prio_class, recency_day DESC, byte_size, mtime DESC, id"
# Jobs of a revoked/tombstoned/purged source are never claimed (they wait; a
# purge deletes them), so a revoked folder cannot be indexed in the background.
_CLAIMABLE = (
    "tier = :tier AND state = 'queued' AND not_before <= :now "
    "AND source_id IN (SELECT id FROM sources WHERE status IN ('ACTIVE', 'MISSING'))"
)


def _norm(path: str | Path) -> str:
    return str(path)


class IngestQueue:
    def __init__(
        self,
        session_factory: sessionmaker,
        policy: QueuePolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sf = session_factory
        self.policy = policy or QueuePolicy()
        self._clock = clock

    # -- enqueue -------------------------------------------------------------

    def _prio_class(self, mtime: float, now: float) -> int:
        return PRIO_RECENT if now - mtime <= self.policy.recent_days * _DAY else PRIO_REST

    def enqueue(
        self,
        source_id: str,
        paths: Iterable[Path],
        *,
        tiers: tuple[int, ...] = (TIER1, TIER2),
        touched: Iterable[Path] = (),
    ) -> dict[str, int]:
        """Add (or refresh) jobs for `paths`. A path whose size/mtime differs
        from its existing row is reset to `queued` (attempts cleared, also from
        `dead`); an unchanged `done`/`dead` row is left alone; `queued`/`running`
        rows keep their state but get their priority refreshed. Returns counts
        `{"new", "reset", "kept"}`."""
        now = self._clock()
        touched_prefixes = [str(Path(t)) for t in touched]
        new = reset = kept = 0
        paths = list(dict.fromkeys(Path(p) for p in paths))
        with self._sf() as session:
            conn = session.connection()
            for start in range(0, len(paths), 900):
                batch = paths[start : start + 900]
                stats = {}
                for p in batch:
                    try:
                        st = p.stat()
                        stats[str(p)] = (st.st_mtime, st.st_size)
                    except OSError:
                        stats[str(p)] = (0.0, 0)  # gone: the worker records "deleted"
                existing = {
                    (r[0], r[1]): (r[2], r[3], r[4])
                    for r in conn.execute(
                        text(
                            "SELECT file_path, tier, mtime, byte_size, state FROM ingest_queue "
                            "WHERE source_id = :s AND file_path IN :ps"
                        ).bindparams(bindparam("ps", expanding=True)),
                        {"s": source_id, "ps": list(stats)},
                    )
                }
                inserts, updates = [], []
                for fp, (mtime, size) in stats.items():
                    prio = PRIO_TOUCHED if any(_under(fp, t) for t in touched_prefixes) else self._prio_class(mtime, now)
                    for tier in tiers:
                        row = existing.get((fp, tier))
                        if row is None:
                            inserts.append(
                                dict(id=_new_id("q"), s=source_id, fp=fp, tier=tier, state=QUEUED,
                                     prio=prio, mtime=mtime, day=int(mtime // _DAY), size=size, now=now)
                            )
                            new += 1
                        else:
                            old_mtime, old_size, state = row
                            changed = (abs(old_mtime - mtime) > 1e-6) or old_size != size
                            if changed:
                                updates.append(dict(s=source_id, fp=fp, tier=tier, prio=prio, mtime=mtime,
                                                    day=int(mtime // _DAY), size=size, now=now, reset=1))
                                reset += 1
                            else:
                                if state in (QUEUED, RUNNING):
                                    updates.append(dict(s=source_id, fp=fp, tier=tier, prio=prio, mtime=mtime,
                                                        day=int(mtime // _DAY), size=size, now=now, reset=0))
                                kept += 1
                if inserts:
                    conn.execute(
                        text(
                            "INSERT INTO ingest_queue (id, source_id, file_path, tier, state, "
                            "prio_class, mtime, recency_day, byte_size, attempts, not_before, enqueued_at) "
                            "VALUES (:id, :s, :fp, :tier, :state, :prio, :mtime, :day, :size, 0, 0, :now)"
                        ),
                        inserts,
                    )
                if updates:
                    conn.execute(
                        text(
                            "UPDATE ingest_queue SET prio_class = :prio, mtime = :mtime, recency_day = :day, "
                            "byte_size = :size, "
                            "state = CASE WHEN :reset = 1 THEN 'queued' ELSE state END, "
                            "attempts = CASE WHEN :reset = 1 THEN 0 ELSE attempts END, "
                            "last_error = CASE WHEN :reset = 1 THEN NULL ELSE last_error END, "
                            "not_before = CASE WHEN :reset = 1 THEN 0 ELSE not_before END, "
                            "enqueued_at = CASE WHEN :reset = 1 THEN :now ELSE enqueued_at END "
                            "WHERE source_id = :s AND file_path = :fp AND tier = :tier"
                        ),
                        updates,
                    )
            session.commit()
        return {"new": new, "reset": reset, "kept": kept}

    def boost(self, source_id: str, prefix: str | Path) -> int:
        """The user touched/opened a folder or file: its queued jobs jump to
        class 0 (persisted, so it survives a restart). Returns rows changed."""
        prefix = _norm(prefix)
        with self._sf() as session:
            n = session.execute(
                text(
                    "UPDATE ingest_queue SET prio_class = 0 WHERE source_id = :s AND state = 'queued' "
                    "AND prio_class > 0 AND (file_path = :p OR file_path LIKE :like ESCAPE '\\')"
                ),
                {"s": source_id, "p": prefix, "like": _like_escape(prefix.rstrip("/")) + "/%"},
            ).rowcount
            session.commit()
        return int(n)

    # -- claim / complete / fail ----------------------------------------------

    def peek(self, tier: int, n: int = 1, source_id: str | None = None) -> list[Path]:
        now = self._clock()
        sql = f"SELECT file_path FROM ingest_queue WHERE {_CLAIMABLE} "
        params: dict = {"tier": tier, "now": now, "n": n}
        if source_id:
            sql += "AND source_id = :s "
            params["s"] = source_id
        with self._sf() as session:
            return [Path(r[0]) for r in session.execute(text(sql + _ORDER + " LIMIT :n"), params)]

    def claim(self, tier: int, source_id: str | None = None) -> QueueJob | None:
        now = self._clock()
        sql = (
            "SELECT id, source_id, file_path, tier, attempts, prio_class, byte_size "
            f"FROM ingest_queue WHERE {_CLAIMABLE} "
        )
        params: dict = {"tier": tier, "now": now}
        if source_id:
            sql += "AND source_id = :s "
            params["s"] = source_id
        with self._sf() as session:
            row = session.execute(text(sql + _ORDER + " LIMIT 1"), params).first()
            if row is None:
                return None
            claimed = session.execute(
                text(
                    "UPDATE ingest_queue SET state = 'running', started_at = :now "
                    "WHERE id = :id AND state = 'queued'"
                ),
                {"id": row[0], "now": now},
            ).rowcount
            session.commit()
        if not claimed:
            return None
        return QueueJob(row[0], row[1], Path(row[2]), row[3], row[4], row[5], row[6])

    def complete(self, job: QueueJob) -> None:
        now = self._clock()
        with self._sf() as session:
            session.execute(
                text(
                    "UPDATE ingest_queue SET state = 'done', finished_at = :now, last_error = NULL, "
                    "duration_s = :now - COALESCE(started_at, :now) WHERE id = :id"
                ),
                {"id": job.id, "now": now},
            )
            session.commit()

    def fail(self, job: QueueJob, error: str) -> str:
        """Record a failed attempt. Returns the new state: `queued` (retry
        after backoff) or `dead` (attempts exhausted)."""
        now = self._clock()
        attempts = job.attempts + 1
        dead = attempts >= self.policy.max_attempts
        with self._sf() as session:
            session.execute(
                text(
                    "UPDATE ingest_queue SET state = :state, attempts = :a, last_error = :e, "
                    "not_before = :nb, finished_at = :now, duration_s = :now - COALESCE(started_at, :now) "
                    "WHERE id = :id"
                ),
                {
                    "state": DEAD if dead else QUEUED, "a": attempts, "e": error[:2000],
                    "nb": 0.0 if dead else now + self.policy.backoff(attempts), "now": now, "id": job.id,
                },
            )
            session.commit()
        return DEAD if dead else QUEUED

    def recover(self) -> int:
        """Startup repair after a crash/kill -9: rows still `running` go back
        to `queued` (or `dead` once their attempts are used up). Returns how
        many were recovered. Call only when no other worker is running."""
        now = self._clock()
        with self._sf() as session:
            rows = session.execute(
                text("SELECT id, attempts FROM ingest_queue WHERE state = 'running'")
            ).all()
            for rid, attempts in rows:
                attempts += 1
                dead = attempts >= self.policy.max_attempts
                session.execute(
                    text(
                        "UPDATE ingest_queue SET state = :s, attempts = :a, started_at = NULL, "
                        "last_error = 'interrupted while running', not_before = 0 WHERE id = :id"
                    ),
                    {"s": DEAD if dead else QUEUED, "a": attempts, "id": rid},
                )
            session.commit()
        return len(rows)

    def retry_dead(self, source_id: str | None = None) -> int:
        with self._sf() as session:
            n = session.execute(
                text(
                    "UPDATE ingest_queue SET state = 'queued', attempts = 0, not_before = 0 "
                    "WHERE state = 'dead'" + (" AND source_id = :s" if source_id else "")
                ),
                {"s": source_id} if source_id else {},
            ).rowcount
            session.commit()
        return int(n)

    def forget_source(self, source_id: str) -> int:
        with self._sf() as session:
            n = session.execute(
                text("DELETE FROM ingest_queue WHERE source_id = :s"), {"s": source_id}
            ).rowcount
            session.commit()
        return int(n)

    # -- observability ---------------------------------------------------------

    def counts(self, source_id: str | None = None) -> dict[int, dict[str, int]]:
        """`{tier: {state: n}}` (all four states always present)."""
        sql = "SELECT tier, state, COUNT(*) FROM ingest_queue "
        params: dict = {}
        if source_id:
            sql += "WHERE source_id = :s "
            params["s"] = source_id
        out = {t: {QUEUED: 0, RUNNING: 0, DONE: 0, DEAD: 0} for t in (TIER1, TIER2)}
        with self._sf() as session:
            for tier, state, n in session.execute(text(sql + "GROUP BY tier, state"), params):
                out.setdefault(tier, {QUEUED: 0, RUNNING: 0, DONE: 0, DEAD: 0})[state] = int(n)
        return out

    def recent_rate(self, tier: int, window_s: float = 900.0, limit: int = 200) -> float | None:
        """Measured files/second for `tier` from jobs finished in the last
        `window_s` seconds (up to `limit`): jobs done divided by the wall-clock
        span they occupied, which already includes parallelism and pauses."""
        now = self._clock()
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT started_at, finished_at FROM ingest_queue WHERE tier = :t AND state = 'done' "
                    "AND finished_at >= :since AND started_at IS NOT NULL "
                    "ORDER BY finished_at DESC LIMIT :n"
                ),
                {"t": tier, "since": now - window_s, "n": limit},
            ).all()
        if len(rows) < 2:
            return None
        span = max(r[1] for r in rows) - min(r[0] for r in rows)
        return len(rows) / span if span > 0 else None

    def dead_letters(self, source_id: str | None = None, limit: int = 20) -> list[tuple[str, int, str | None]]:
        sql = "SELECT file_path, tier, last_error FROM ingest_queue WHERE state = 'dead' "
        params: dict = {"n": limit}
        if source_id:
            sql += "AND source_id = :s "
            params["s"] = source_id
        with self._sf() as session:
            return [tuple(r) for r in session.execute(text(sql + "ORDER BY finished_at DESC LIMIT :n"), params)]

    def next_retry_at(self) -> float | None:
        with self._sf() as session:
            return session.execute(
                text("SELECT MIN(not_before) FROM ingest_queue WHERE state = 'queued' AND not_before > 0")
            ).scalar()


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")
