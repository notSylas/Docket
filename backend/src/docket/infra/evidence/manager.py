"""Evidence ingestion: ties the content-addressed store to EvidenceVersion rows.

``EvidenceManager`` is the per-source ingestion entrypoint: given a source and
a file on disk, it stores the file's bytes in the ``ContentAddressedStore``
(deduped globally by content hash) and records a per-source ``EvidenceVersion``
row tracking which content that source currently points at.
"""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.core.db.models import EvidenceVersion, VersionStatus
from docket.infra.evidence.references import add_blob_reference
from docket.infra.evidence.store import ContentAddressedStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceManager:
    """Ingests files into the evidence store and tracks their versions."""

    def __init__(self, store: ContentAddressedStore, session_factory: sessionmaker) -> None:
        self.store = store
        self.session_factory = session_factory

    def _current_version(
        self, session: Session, source_id: str, file_path: str
    ) -> EvidenceVersion | None:
        # Scoped by (source_id, file_path), not source_id alone: a
        # "local_folder" source (CP8) can hold many files, each with its own
        # independent version lineage. Also match legacy rows written before
        # `file_path` existed (NULL) when `file_path` happens to match -- see
        # migration 0002's docstring. In practice this only matters for a
        # source that has never recorded a file_path before; once
        # `ingest_file` runs once for a given path, that path's rows always
        # carry it from then on.
        #
        # "Current" here means "the latest lineage slot", not "servable" --
        # status != SUPERSEDED, not status == READY. A version can be the
        # latest-known content for a file while still PENDING (not yet
        # processed) or FAILED (processing raised); either way it's still
        # the row this file's *next* ingest needs to compare against, and the
        # one supersession (on changed content) must flip to SUPERSEDED. See
        # Upgrade doc 03 section 4.
        stmt = select(EvidenceVersion).where(
            EvidenceVersion.source_id == source_id,
            EvidenceVersion.status != VersionStatus.SUPERSEDED,
            EvidenceVersion.file_path == file_path,
        )
        return session.execute(stmt).scalar_one_or_none()

    def ingest_file(
        self,
        source_id: str,
        path: Path,
        *,
        parser_name: str,
        parser_version: str,
    ) -> EvidenceVersion:
        path = Path(path)
        data = path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        file_path = str(path)

        with self.session_factory() as session:
            current = self._current_version(session, source_id, file_path)

            if current is not None and current.content_hash == content_hash:
                # Unchanged bytes. Always return the same row without
                # touching the store or DB further -- but whether that's a
                # true no-op or something the *pipeline* should still (re)
                # process depends on `current.status`, which this manager
                # deliberately doesn't decide: PENDING/FAILED means
                # processing never finished successfully for this exact
                # content, so the caller (`IngestionPipeline`) re-drives it
                # to READY instead of treating it as settled. Only a READY
                # row is a genuine no-op. See Upgrade doc 03 section 4's
                # Failed->Ready transition.
                return current

            if current is not None:
                # Supersession always wins immediately, regardless of where
                # `current` was in its own lifecycle (PENDING, READY, or
                # FAILED all flip straight to SUPERSEDED) -- see
                # `mark_version_status` for the mirror-image guard that keeps
                # an in-flight processing job from overwriting this.
                current.status = VersionStatus.SUPERSEDED
                session.add(current)

            # Store bytes (globally deduped; no-op if content already exists).
            self.store.put(data)

            mime_type, _ = mimetypes.guess_type(str(path))
            manifest = {
                "byte_size": len(data),
                "mime_type": mime_type,
                "original_filename": path.name,
            }
            self.store.write_manifest(content_hash, manifest)

            evidence_version = EvidenceVersion(
                source_id=source_id,
                file_path=file_path,
                content_hash=content_hash,
                byte_size=len(data),
                mime_type=mime_type,
                observed_at=_utcnow(),
                parser_name=parser_name,
                parser_version=parser_version,
                status=VersionStatus.PENDING,
            )
            session.add(evidence_version)
            # Flush (not commit) so `evidence_version.id` is populated
            # before the blob-reference row needs it -- same transaction,
            # same pattern `SourceManager.register_source` already uses for
            # an analogous "need the child's id before inserting the next
            # row" case.
            session.flush()

            # Upgrade doc 03 section 8: record the live reference this
            # version holds into the ContentAddressedStore, in the SAME
            # transaction as the version row itself, so refcounts can never
            # drift from what's actually stored.
            add_blob_reference(
                session,
                content_hash=content_hash,
                referencing_table="evidence_versions",
                referencing_id=evidence_version.id,
                role="content",
            )

            session.commit()
            session.refresh(evidence_version)
            return evidence_version

    def mark_version_status(self, evidence_version_id: str, status: VersionStatus) -> None:
        """Transition a version to `READY` or `FAILED` once its processing
        (parse + chunk + index) finishes, one way or the other.

        No-ops if the row has already moved to `SUPERSEDED` -- a newer
        version for the same (source_id, file_path) was stored while this
        one was still being processed, and supersession always wins
        regardless of how the in-flight processing job turns out (Upgrade
        doc 03 section 4). No-ops if the row is missing entirely, which
        shouldn't happen but isn't this method's job to raise on.
        """
        with self.session_factory() as session:
            version = session.get(EvidenceVersion, evidence_version_id)
            if version is None or version.status == VersionStatus.SUPERSEDED:
                return
            version.status = status
            session.add(version)
            session.commit()

    def get_evidence_bytes(self, evidence_version: EvidenceVersion) -> bytes:
        return self.store.get(evidence_version.content_hash)
