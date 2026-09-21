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

from attest.db.models import EvidenceVersion
from attest.evidence.store import ContentAddressedStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceManager:
    """Ingests files into the evidence store and tracks their versions."""

    def __init__(self, store: ContentAddressedStore, session_factory: sessionmaker) -> None:
        self.store = store
        self.session_factory = session_factory

    def _current_version(self, session: Session, source_id: str) -> EvidenceVersion | None:
        stmt = select(EvidenceVersion).where(
            EvidenceVersion.source_id == source_id,
            EvidenceVersion.is_current.is_(True),
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

        with self.session_factory() as session:
            current = self._current_version(session, source_id)

            if current is not None and current.content_hash == content_hash:
                # Unchanged rescan: idempotent no-op, return the existing row.
                return current

            if current is not None:
                current.is_current = False
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
                content_hash=content_hash,
                byte_size=len(data),
                mime_type=mime_type,
                observed_at=_utcnow(),
                parser_name=parser_name,
                parser_version=parser_version,
                is_current=True,
            )
            session.add(evidence_version)
            session.commit()
            session.refresh(evidence_version)
            return evidence_version

    def get_evidence_bytes(self, evidence_version: EvidenceVersion) -> bytes:
        return self.store.get(evidence_version.content_hash)
