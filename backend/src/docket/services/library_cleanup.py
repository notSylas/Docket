"""Explicit retention cleanup. A preview never advances lifecycle state."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from docket.core.db.models import Source, SourceStatus
from docket.core.writer_lock import lock_for_sessions


class CleanupError(ValueError):
    pass


@dataclass(frozen=True)
class CleanupPlan:
    source_ids: tuple[str, ...]
    trash_objects: tuple[str, ...]
    applied: bool = False


class LibraryCleanup:
    def __init__(self, sessions, sources, purge, store, settings, *, writer_lock=None):
        self.sessions, self.sources, self.purge = sessions, sources, purge
        self.store, self.settings = store, settings
        self.lock = writer_lock or lock_for_sessions(sessions)

    def purge_disconnected(self, source_id):
        with self.lock.hold("purging a disconnected folder"):
            source = self.sources.get_source(source_id)
            if source.status not in (SourceStatus.REVOKED, SourceStatus.HARD_DELETE_PENDING):
                raise CleanupError("Disconnect this folder before purging it. Original files will never be touched.")
            if source.status == SourceStatus.REVOKED:
                self.sources.request_hard_delete(source_id)
            return self.purge.purge_source(source_id)

    def clean(self, *, apply=False, now=None):
        now = now or datetime.now(timezone.utc)
        grace = timedelta(days=max(0, self.settings.blob_trash_grace_days))
        with self.lock.hold("cleaning retained library data" if apply else "previewing library cleanup"):
            with self.sessions() as db:
                expired = tuple(db.execute(select(Source.id).where(
                    (Source.status == SourceStatus.HARD_DELETE_PENDING)
                    | ((Source.status == SourceStatus.TOMBSTONED)
                       & Source.retention_deadline.is_not(None)
                       & (Source.retention_deadline <= now))
                ).order_by(Source.id)).scalars())
            trash = tuple(sorted(p.name for p in self.store.trash_dir.iterdir()
                                 if p.is_file() and not p.is_symlink()
                                 and p.stat().st_mtime <= (now - grace).timestamp())) if self.store.trash_dir.exists() else ()
            if apply:
                self.sources.sweep_expired_retentions(now=now)
                for source_id in expired:
                    self.purge.purge_source(source_id)
                deleted = self.store.sweep_trash(grace, now=now)
                return CleanupPlan(expired, tuple(deleted), True)
            return CleanupPlan(expired, trash)
