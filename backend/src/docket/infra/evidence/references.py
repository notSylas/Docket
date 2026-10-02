"""Helpers for writing/querying `EvidenceBlobReference` rows -- Upgrade doc
03 section 8's single source of truth for "how many live references point at
this content_hash in the `ContentAddressedStore`".

Kept as plain functions taking an already-open `Session` (rather than a
class wrapping a `session_factory`) because every call site here needs to
run in the SAME transaction as the surrounding write that put a hash into
the store (`EvidenceManager.ingest_file`, `VisualIndexer.save_page_images`)
or the surrounding purge transaction (`SourcePurgeService.purge_source`) --
a helper that opened its own session could never satisfy that.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from docket.core.db.models import EvidenceBlobReference


def add_blob_reference(
    session: Session,
    *,
    content_hash: str,
    referencing_table: str,
    referencing_id: str,
    role: str,
) -> None:
    """Idempotently record that `referencing_id` (a row in
    `referencing_table`) holds a live reference to `content_hash` under
    `role`. A no-op if the exact (content_hash, referencing_table,
    referencing_id, role) tuple is already recorded.

    Idempotency matters here: a `FAILED` `EvidenceVersion` can be retried
    with the same row (same id) and, since its bytes/page-images are
    unchanged, the exact same hashes -- without this check, every retry
    would insert duplicate reference rows for the same logical reference.
    """
    exists = session.execute(
        select(EvidenceBlobReference.id).where(
            EvidenceBlobReference.content_hash == content_hash,
            EvidenceBlobReference.referencing_table == referencing_table,
            EvidenceBlobReference.referencing_id == referencing_id,
            EvidenceBlobReference.role == role,
        )
    ).scalar_one_or_none()
    if exists is not None:
        return
    session.add(
        EvidenceBlobReference(
            content_hash=content_hash,
            referencing_table=referencing_table,
            referencing_id=referencing_id,
            role=role,
        )
    )


def count_blob_references(session: Session, content_hash: str) -> int:
    """How many live reference rows point at `content_hash`, system-wide --
    the safe-delete check: a blob is eligible for removal only when this is
    zero."""
    return session.execute(
        select(func.count())
        .select_from(EvidenceBlobReference)
        .where(EvidenceBlobReference.content_hash == content_hash)
    ).scalar_one()


def delete_blob_references_for(
    session: Session, *, referencing_table: str, referencing_id: str
) -> None:
    """Remove every reference row recorded for one referencing row (e.g. one
    `EvidenceVersion` being purged) -- called before/alongside deleting that
    row itself, so a stale reference never outlives the thing it referenced."""
    session.execute(
        delete(EvidenceBlobReference).where(
            EvidenceBlobReference.referencing_table == referencing_table,
            EvidenceBlobReference.referencing_id == referencing_id,
        )
    )
