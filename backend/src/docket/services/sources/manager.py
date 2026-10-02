"""`SourceManager` -- registers/lists/looks up/transitions ``Source`` rows
through the `SourceStatus` state machine (Upgrade doc 03 section 6).

Solo-use scope note: the approved plan defers multi-workspace support, so
this checkpoint works against a single implicit "default" workspace
(``get_or_create_default_workspace``) rather than requiring every caller to
juggle a workspace id. Callers that DO have a specific ``workspace_id`` (e.g.
a future multi-workspace UI) can still pass one explicitly to
``register_source``/``list_sources``.

State machine note: no connector exists yet to automatically drive most of
these transitions (``ACTIVE``/``MISSING`` outage detection, a confirmed
upstream-deletion signal, a 403-class access-loss signal), so every method
below is a plain, explicitly-callable entry point a connector can call into
later -- not something this module polls or schedules itself. The one
exception is the local-folder root-reachability check, which
``IngestionPipeline`` already has a natural hook for (see that module) and
drives via ``mark_unreachable``/``mark_reachable`` on every ingestion run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.config import Settings
from docket.core.config import settings as _default_settings
from docket.core.db.models import AuthorizedSource, Source, SourceStatus, Workspace

DEFAULT_WORKSPACE_NAME = "default"

# Allowed "from" statuses for each transition target -- the state machine's
# edge list (Upgrade doc 03 section 6's table / mermaid diagram), minus
# REVOKED/TOMBSTONED -> HARD_DELETE_PENDING -> DELETED's shared target
# (handled separately below since both REVOKED and TOMBSTONED feed it).
_MISSING_ALLOWED_FROM = {SourceStatus.ACTIVE}
_ACTIVE_ALLOWED_FROM = {SourceStatus.MISSING}
_TOMBSTONED_ALLOWED_FROM = {SourceStatus.MISSING}
_REVOKED_ALLOWED_FROM = {SourceStatus.ACTIVE, SourceStatus.MISSING}
_HARD_DELETE_PENDING_ALLOWED_FROM = {SourceStatus.REVOKED, SourceStatus.TOMBSTONED}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceNotFoundError(Exception):
    def __init__(self, source_id: str):
        self.source_id = source_id
        super().__init__(f"source not found: {source_id}")


class InvalidSourceTransitionError(Exception):
    """Raised when a caller asks for a `SourceStatus` transition that isn't
    reachable from the source's current status -- e.g. trying to
    `mark_tombstoned` a source that's still ACTIVE (TOMBSTONED is only
    reachable from MISSING; see Upgrade doc 03 section 6's transition
    table)."""

    def __init__(self, source_id: str, from_status: SourceStatus, to_status: SourceStatus):
        self.source_id = source_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"source {source_id}: cannot transition {from_status.value} -> {to_status.value}"
        )


class SourceManager:
    def __init__(self, session_factory: sessionmaker, *, settings: Settings | None = None) -> None:
        self._session_factory = session_factory
        self._settings = settings if settings is not None else _default_settings

    def get_or_create_default_workspace(self) -> Workspace:
        """Return the single default workspace, creating it if it doesn't
        exist yet. Idempotent: repeated calls return the same row."""
        with self._session_factory() as session:
            workspace = session.execute(
                select(Workspace).where(Workspace.name == DEFAULT_WORKSPACE_NAME)
            ).scalar_one_or_none()
            if workspace is not None:
                return workspace

            workspace = Workspace(name=DEFAULT_WORKSPACE_NAME)
            session.add(workspace)
            session.commit()
            session.refresh(workspace)
            return workspace

    def register_source(self, path: Path, *, workspace_id: str | None = None) -> Source:
        """Register a local folder as a source.

        Validates ``path`` exists and is a directory before touching the DB
        -- registering a bogus path would silently create a Source that can
        never successfully ingest anything, so we fail fast here instead.
        """
        path = Path(path)
        if not path.exists():
            raise ValueError(f"source path does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"source path is not a directory: {path}")

        if workspace_id is None:
            workspace_id = self.get_or_create_default_workspace().id

        with self._session_factory() as session:
            authorized_source = AuthorizedSource(
                workspace_id=workspace_id, scope_path=str(path)
            )
            session.add(authorized_source)
            session.flush()

            source = Source(
                workspace_id=workspace_id,
                authorized_source_id=authorized_source.id,
                source_type="local_folder",
                path=str(path),
                status=SourceStatus.ACTIVE,
            )
            session.add(source)
            session.commit()
            session.refresh(source)
            return source

    def list_sources(self, *, workspace_id: str | None = None) -> list[Source]:
        with self._session_factory() as session:
            stmt = select(Source)
            if workspace_id is not None:
                stmt = stmt.where(Source.workspace_id == workspace_id)
            return list(session.execute(stmt).scalars().all())

    def get_source(self, source_id: str) -> Source:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            return source

    # -- state machine transitions (Upgrade doc 03 section 6) ------------

    def mark_unreachable(self, source_id: str) -> None:
        """``ACTIVE -> MISSING``: the source's root is currently unreachable
        (e.g. a local folder whose root path doesn't exist/isn't a directory
        during a scan, or a future connector's transient network/API
        failure). Retains all content; retrieval keeps serving the last
        known ``READY`` versions untouched -- this only flags degraded
        freshness, it never removes or hides anything.

        Idempotent: a no-op if the source is already ``MISSING``. Does
        **not** escalate to ``TOMBSTONED`` on repeated calls -- that
        requires a separate, explicit, *confirmed*-deletion signal
        (``mark_tombstoned``); no local-folder signal today can tell "still
        trying" from "confirmed gone" apart (section 6's explicit decision).
        """
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status == SourceStatus.MISSING:
                return
            if source.status not in _MISSING_ALLOWED_FROM:
                raise InvalidSourceTransitionError(source_id, source.status, SourceStatus.MISSING)
            source.status = SourceStatus.MISSING
            session.add(source)
            session.commit()

    def mark_reachable(self, source_id: str) -> None:
        """``MISSING -> ACTIVE``: the next successful sync/scan confirms the
        source is reachable again. Idempotent: a no-op if the source is
        already ``ACTIVE``. No data changes -- this only clears the
        degraded-freshness indicator `mark_unreachable` set."""
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status == SourceStatus.ACTIVE:
                return
            if source.status not in _ACTIVE_ALLOWED_FROM:
                raise InvalidSourceTransitionError(source_id, source.status, SourceStatus.ACTIVE)
            source.status = SourceStatus.ACTIVE
            session.add(source)
            session.commit()

    def mark_tombstoned(
        self,
        source_id: str,
        *,
        reason: str | None = None,
        retention_days: int | None = None,
    ) -> None:
        """``MISSING -> TOMBSTONED``: an explicit, CONFIRMED deletion signal
        (a Graph delta deletion marker, an explicit local-deletion signal --
        never automatic escalation from repeated `mark_unreachable` calls
        alone; see that method's docstring and Upgrade doc 03 section 6).

        Sets ``retention_deadline = now + retention_days`` (default:
        ``settings.tombstone_retention_days``, a single global default, not
        per-source/per-connector-type). ``sweep_expired_retentions`` later
        advances this source to ``HARD_DELETE_PENDING`` once that deadline
        passes.
        """
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status not in _TOMBSTONED_ALLOWED_FROM:
                raise InvalidSourceTransitionError(
                    source_id, source.status, SourceStatus.TOMBSTONED
                )
            days = (
                retention_days
                if retention_days is not None
                else self._settings.tombstone_retention_days
            )
            source.status = SourceStatus.TOMBSTONED
            source.retention_deadline = _utcnow() + timedelta(days=days)
            if reason is not None:
                source.status_reason = reason
            session.add(source)
            session.commit()

    def deactivate_source(self, source_id: str, *, reason: str | None = None) -> None:
        """``ACTIVE``/``MISSING -> REVOKED``. Covers both a user-initiated
        "disconnect" action and a connector-detected access-loss signal
        (e.g. a 403-class response) -- Upgrade doc 03 section 6's explicit
        decision reuses this one transition for both rather than adding a
        second `SourceStatus` value, since the practical effect (excluded
        from retrieval, no auto-cleanup) is identical either way. ``reason``
        lets a caller record *why* (e.g. ``"user_disconnected"`` vs.
        ``"access_revoked"``) so the UI can tell them apart later.

        Idempotent: calling this again on an already-``REVOKED`` source
        succeeds (optionally updating ``reason``) rather than raising.

        Does not delete any data -- actual data removal only happens once a
        hard-delete is requested (`request_hard_delete`) and its purge job
        runs (`SourcePurgeService.purge_source`, section 8); `REVOKED` can
        persist indefinitely, since "disconnect" and "delete stored data"
        are deliberately separate actions.
        """
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status != SourceStatus.REVOKED and source.status not in _REVOKED_ALLOWED_FROM:
                raise InvalidSourceTransitionError(source_id, source.status, SourceStatus.REVOKED)
            source.status = SourceStatus.REVOKED
            if reason is not None:
                source.status_reason = reason
            session.add(source)
            session.commit()

    def request_hard_delete(self, source_id: str, *, reason: str | None = None) -> None:
        """``REVOKED``/``TOMBSTONED -> HARD_DELETE_PENDING`` via explicit
        user confirmation of "delete stored data". Only flips the scheduling
        state -- queues the purge (section 8) but makes no visible behavior
        change and touches no evidence/blob data itself; the actual deletion
        happens when `SourcePurgeService.purge_source` runs."""
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status not in _HARD_DELETE_PENDING_ALLOWED_FROM:
                raise InvalidSourceTransitionError(
                    source_id, source.status, SourceStatus.HARD_DELETE_PENDING
                )
            source.status = SourceStatus.HARD_DELETE_PENDING
            if reason is not None:
                source.status_reason = reason
            session.add(source)
            session.commit()

    def sweep_expired_retentions(self, *, now: datetime | None = None) -> list[str]:
        """``TOMBSTONED -> HARD_DELETE_PENDING`` for every source whose
        ``retention_deadline`` has passed.

        A plain, explicitly-called function -- NOT a background daemon
        (Upgrade doc 03 section 6: "a simple explicit function... that a
        caller [a CLI command, or an existing periodic path like `docket
        watch`] can invoke is sufficient"). Returns the list of source ids
        advanced, so a caller can log/report what it did.
        """
        now = now if now is not None else _utcnow()
        advanced: list[str] = []
        with self._session_factory() as session:
            stmt = select(Source).where(
                Source.status == SourceStatus.TOMBSTONED,
                Source.retention_deadline.is_not(None),
                Source.retention_deadline <= now,
            )
            expired = session.execute(stmt).scalars().all()
            for source in expired:
                source.status = SourceStatus.HARD_DELETE_PENDING
                source.status_reason = "retention_deadline_expired"
                session.add(source)
                advanced.append(source.id)
            session.commit()
        return advanced

    # -- pause/resume sync (orthogonal to SourceStatus; section 6) -------

    def pause_sync(self, source_id: str) -> None:
        """Suspend background refresh for a source without affecting its
        status -- a paused source is still ``ACTIVE`` and searchable on its
        last-synced content (Upgrade doc 03 section 6: deliberately NOT part
        of `SourceStatus`, which would otherwise need an awkward
        "paused-but-still-active" state)."""
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            source.sync_paused_at = _utcnow()
            session.add(source)
            session.commit()

    def resume_sync(self, source_id: str) -> None:
        """Clear a previously-set `pause_sync`. A no-op if the source isn't
        currently paused."""
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            source.sync_paused_at = None
            session.add(source)
            session.commit()
