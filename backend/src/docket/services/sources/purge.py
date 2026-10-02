"""`SourcePurgeService` -- the ``HARD_DELETE_PENDING -> DELETED`` purge job
(Upgrade doc 03 sections 6 and 8).

Ties together the blob-reference bookkeeping in
``docket.infra.evidence.references`` with the source-status state machine in
``SourceManager``: for a source already in ``HARD_DELETE_PENDING`` (reached
via ``SourceManager.request_hard_delete`` or ``sweep_expired_retentions``),
``purge_source`` deletes its ``EvidenceVersion`` rows (cascading to
``EvidenceUnit``/``Chunk`` via the ORM relationship cascade already declared
on ``EvidenceVersion`` -- see ``core/db/models.py``), removes its indexed
chunks from both the FTS5 and vector indexes, moves any now-unreferenced
blob to ``trash/`` (a *separate*, independent sweep --
``ContentAddressedStore.sweep_trash`` -- later deletes it for good), and
finally flips the source to ``DELETED``.

A plain, explicitly-callable method -- not a background daemon, per Upgrade
doc 03's "keep this mechanically simple and testable" guidance (sections 6
and 8).

The ``Source`` row itself is deliberately NEVER deleted here: once
``status == DELETED``, the row (its id, ``status_reason``, and
``updated_at`` as the deletion timestamp, via `Source`'s existing
``onupdate``) IS the "minimal tombstone record" section 6 calls for -- a
previously issued citation can still resolve the source id to an explicit
"this evidence was deleted on <date>" message rather than a dangling or
silently reassigned reference. No separate tombstone table/citation-snapshot
feature is needed (section 6's explicit decision).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import EvidenceVersion, Source, SourceStatus
from docket.infra.evidence.references import count_blob_references, delete_blob_references_for
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.manager import IndexManager
from docket.services.sources.manager import SourceNotFoundError


class SourceNotPurgeableError(Exception):
    """Raised when `purge_source` is asked to purge a source that isn't
    `HARD_DELETE_PENDING` -- purging is only reachable via that scheduling
    state (see `SourceManager.request_hard_delete`/`sweep_expired_retentions`),
    never directly from `ACTIVE`/`REVOKED`/`TOMBSTONED`."""

    def __init__(self, source_id: str, status: SourceStatus):
        self.source_id = source_id
        self.status = status
        super().__init__(
            f"source {source_id} is not HARD_DELETE_PENDING (status={status.value}); "
            "refusing to purge"
        )


@dataclass
class PurgeResult:
    source_id: str
    versions_deleted: int
    blobs_trashed: list[str] = field(default_factory=list)
    blobs_retained: list[str] = field(default_factory=list)


class SourcePurgeService:
    def __init__(
        self,
        session_factory: sessionmaker,
        store: ContentAddressedStore,
        *,
        index_manager: IndexManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._index_manager = index_manager

    def purge_source(self, source_id: str, *, reason: str | None = None) -> PurgeResult:
        """Permanently delete `source_id`'s evidence rows and (where safe)
        its blobs, then flip it to `DELETED`.

        Raises `SourceNotFoundError`/`SourceNotPurgeableError` without
        touching anything if the source doesn't exist or isn't
        `HARD_DELETE_PENDING`.
        """
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            if source.status != SourceStatus.HARD_DELETE_PENDING:
                raise SourceNotPurgeableError(source_id, source.status)

            versions = list(
                session.execute(
                    select(EvidenceVersion).where(EvidenceVersion.source_id == source_id)
                ).scalars()
            )

            # Collect every content_hash this source's versions touch
            # (primary bytes + any page-image hashes) BEFORE deleting
            # anything, so the zero-reference check below has the full set
            # to re-count against.
            touched_hashes: set[str] = set()
            for version in versions:
                touched_hashes.add(version.content_hash)
                if version.page_images_json:
                    touched_hashes.update(json.loads(version.page_images_json).values())

                # Remove this version's own reference rows first -- cheap
                # and explicit, rather than relying on the version delete
                # below to somehow imply it (evidence_blob_references has no
                # real FK/cascade to evidence_versions; see that table's
                # docstring for why).
                delete_blob_references_for(
                    session, referencing_table="evidence_versions", referencing_id=version.id
                )
                # Deleting the EvidenceVersion row cascades (ORM
                # cascade="all, delete-orphan") to its EvidenceUnit and
                # Chunk rows -- no separate cleanup needed for those.
                session.delete(version)

            session.flush()

            # Safe-delete rule (section 8): a blob is eligible for removal
            # only when it now has zero rows in evidence_blob_references --
            # i.e. no OTHER version (this source's or any other source's)
            # still references the same hash.
            blobs_trashed: list[str] = []
            blobs_retained: list[str] = []
            for content_hash in touched_hashes:
                remaining = count_blob_references(session, content_hash)
                if remaining == 0:
                    self._store.move_to_trash(content_hash)
                    blobs_trashed.append(content_hash)
                else:
                    blobs_retained.append(content_hash)

            source.status = SourceStatus.DELETED
            source.status_reason = reason or source.status_reason or "purged"
            session.add(source)
            session.commit()

        # Index removal happens after the DB transaction commits
        # successfully -- the SQL rows are the source of truth; FTS5/LanceDB
        # are downstream indexes kept in sync best-effort, same posture as
        # `IngestionPipeline`'s own end-of-run reconcile pass.
        if self._index_manager is not None:
            self._index_manager.delete_source(source_id)

        return PurgeResult(
            source_id=source_id,
            versions_deleted=len(versions),
            blobs_trashed=blobs_trashed,
            blobs_retained=blobs_retained,
        )
