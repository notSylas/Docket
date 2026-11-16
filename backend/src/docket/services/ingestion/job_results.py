"""Commit each file before publishing its completion to the UI."""
from datetime import datetime, timezone
from sqlalchemy import select
from docket.core.db.models import IngestionFileResult
from docket.services.ingestion.failure import plain_failure_reason, condense_error
from docket.services.ingestion.skips import SkippedFile, reason_text


class JobFiles:
    def __init__(self, session_factory):
        self.sessions = session_factory

    def prepare(self, job_id, paths):
        with self.sessions() as db:
            db.add_all(IngestionFileResult(job_id=job_id, path=str(p)) for p in dict.fromkeys(paths))
            db.commit()

    def update(self, job_id, path, result=None):
        with self.sessions() as db:
            row = db.execute(select(IngestionFileResult).where(
                IngestionFileResult.job_id == job_id, IngestionFileResult.path == str(path))).scalar_one()
            now = datetime.now(timezone.utc)
            if result is None:
                row.outcome, row.started_at = "running", now
            else:
                row.outcome = result.status
                row.chunks_written = result.chunks_written
                row.finished_at = now
                if result.status == "failed":
                    row.failure_reason = plain_failure_reason(condense_error(result.error), path)
                elif result.status == "skipped" and getattr(result, "skip_reason", None):
                    row.failure_reason = reason_text(result.skip_reason)
            db.commit()

    def record_skipped(self, job_id, skipped):
        """Files discovery never handed to a parser (unsupported type, not a
        real document): durable "skipped" rows carrying the reason sentence.
        Paths already prepared for this job are left alone."""
        with self.sessions() as db:
            seen = set(db.execute(select(IngestionFileResult.path).where(
                IngestionFileResult.job_id == job_id)).scalars())
            now = datetime.now(timezone.utc)
            db.add_all(
                IngestionFileResult(job_id=job_id, path=str(path), outcome="skipped",
                                    failure_reason=reason_text(code), finished_at=now)
                for path, code in dict.fromkeys(skipped) if str(path) not in seen)
            db.commit()

    def finish(self, job_id):
        with self.sessions() as db:
            for row in db.execute(select(IngestionFileResult).where(
                IngestionFileResult.job_id == job_id,
                IngestionFileResult.outcome.in_(("pending", "running")))).scalars():
                row.failure_reason = "Stopped before this file finished. Retry the library update."
                row.outcome = "interrupted" if row.started_at else "cancelled"
                row.finished_at = datetime.now(timezone.utc)
            db.commit()
