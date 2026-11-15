"""Build / rebuild document-layer rows from stored chunks.

Idempotent: a version's `documents` row (and its fields) is replaced in one
transaction, so rerunning never duplicates and a crash leaves either the old or
the new row. Only READY versions of ACTIVE sources get rows; rows for versions
that stop being servable are removed (`prune_documents`), and purge deletes them
through the ORM cascade on `EvidenceVersion.document`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import PurePath

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from docket.core.db.models import (
    Chunk, Document, DocumentField, EvidenceVersion, Source, SourceStatus, VersionStatus,
)
from docket.services.documents import classify, family
from docket.services.documents.fields import EXTRACTOR_VERSION, ChunkText, extract_all

logger = logging.getLogger(__name__)

_CONTRACTISH = {"contract", "agreement", "amendment"}


@dataclass
class BuildStats:
    built: int = 0
    skipped: int = 0
    removed: int = 0
    fields: int = 0
    linked: int = 0


def _chunks_for(session: Session, version_id: str) -> list[Chunk]:
    return list(
        session.execute(
            select(Chunk).where(Chunk.evidence_version_id == version_id).order_by(Chunk.ordinal)
        ).scalars()
    )


def _preamble(chunks: list[Chunk], limit: int = 3500) -> str:
    out, n = [], 0
    for c in chunks:
        out.append(c.text)
        n += len(c.text)
        if n >= limit:
            break
    return "\n".join(out)[:limit]


def build_document(session: Session, version: EvidenceVersion, kag: bool = False) -> Document | None:
    """Replace the document row for `version` (caller commits). None when the
    version has no chunks."""
    chunks = _chunks_for(session, version.id)
    if not chunks:
        return None
    file_name = PurePath(version.file_path).name if version.file_path else ""
    parent = PurePath(version.file_path).parent.name if version.file_path else ""
    first_text = _preamble(chunks)
    first_heading = next((c.heading for c in chunks if c.heading), None)
    title = classify.guess_title(file_name, chunks[0].text, first_heading)
    doc_type = classify.classify_doc_type(file_name, title, first_text)
    parties = classify.extract_parties(first_text) if doc_type in _CONTRACTISH else []
    dates = classify.preamble_dates(first_text[:2500]) if doc_type in _CONTRACTISH else {}
    pages = [c.page_end for c in chunks if c.page_end]
    headings = [c.heading for c in chunks if c.heading]
    doc_text = classify.build_doc_text(title, headings, first_text)
    row = Document(
        evidence_version_id=version.id,
        source_id=version.source_id,
        file_name=file_name,
        doc_type=doc_type,
        title=title,
        parties_json=json.dumps(parties) if parties else None,
        dates_json=json.dumps(dates) if dates else None,
        page_count=max(pages) if pages else None,
        family_key=classify.family_key(file_name, doc_type, parent),
        source_mtime=version.source_mtime,
        doc_text=doc_text,
        extractor_version=EXTRACTOR_VERSION,
    )
    existing = session.execute(
        select(Document).where(Document.evidence_version_id == version.id)
    ).scalar_one_or_none()
    if existing is not None:
        session.delete(existing)
        session.flush()
    session.add(row)
    session.flush()
    if kag:
        from docket.services.documents import kag as kag_graph

        kag_graph.write_graph(session, row.id, chunks, parties, first_text)
    if doc_type in _CONTRACTISH or doc_type in ("other", "report"):
        cts = [ChunkText(c.id, c.text, c.ordinal, c.heading) for c in chunks]
        if doc_type in _CONTRACTISH:
            for fv in extract_all(cts, parties):
                session.add(DocumentField(
                    document_id=row.id, field=fv.field, value=fv.value, normalized=fv.normalized,
                    evidence_chunk_id=fv.chunk_id,
                    char_span=f"{fv.span[0]}:{fv.span[1]}" if fv.span else None,
                    confidence=fv.confidence, extractor_version=EXTRACTOR_VERSION,
                ))
    return row


def build_for_version(session_factory: sessionmaker, version_id: str, kag: bool = False) -> bool:
    """Ingestion hook: never raises (a document-layer failure must not fail an
    ingest). Returns True when a row was written."""
    try:
        with session_factory() as session:
            version = session.get(EvidenceVersion, version_id)
            if version is None or version.status not in (VersionStatus.READY, VersionStatus.PENDING):
                return False
            row = build_document(session, version, kag)
            if row is not None:
                family.link_document(session, row)
            session.commit()
            return row is not None
    except Exception:  # noqa: BLE001
        logger.warning("document build failed for version %s", version_id, exc_info=True)
        return False


def prune_documents(session_factory: sessionmaker) -> int:
    """Delete rows whose version is no longer READY or whose source is not
    ACTIVE (superseded / revoked / tombstoned)."""
    with session_factory() as session:
        stale = session.execute(
            select(Document.id)
            .join(EvidenceVersion, EvidenceVersion.id == Document.evidence_version_id)
            .join(Source, Source.id == EvidenceVersion.source_id)
            .where((EvidenceVersion.status != VersionStatus.READY) | (Source.status != SourceStatus.ACTIVE))
        ).scalars().all()
        for i in range(0, len(stale), 500):
            ids = stale[i:i + 500]
            session.execute(delete(DocumentField).where(DocumentField.document_id.in_(ids)))
            session.execute(delete(Document).where(Document.id.in_(ids)))
        session.commit()
        return len(stale)


def delete_for_version(session_factory: sessionmaker, version_id: str) -> None:
    """Hook for supersede/revoke paths. Never raises."""
    try:
        with session_factory() as session:
            ids = session.execute(select(Document.id).where(Document.evidence_version_id == version_id)).scalars().all()
            if ids:
                session.execute(delete(DocumentField).where(DocumentField.document_id.in_(ids)))
                session.execute(delete(Document).where(Document.id.in_(ids)))
                session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("document delete failed for version %s", version_id, exc_info=True)


def delete_for_source(session_factory: sessionmaker, source_id: str) -> None:
    try:
        with session_factory() as session:
            ids = session.execute(select(Document.id).where(Document.source_id == source_id)).scalars().all()
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                session.execute(delete(DocumentField).where(DocumentField.document_id.in_(chunk)))
                session.execute(delete(Document).where(Document.id.in_(chunk)))
            session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("document delete failed for source %s", source_id, exc_info=True)


def build_all(session_factory: sessionmaker, source_id: str | None = None, *, force: bool = False, kag: bool = False) -> BuildStats:
    """Backfill for an existing library. Skips versions that already have a
    current-extractor row unless `force`; prunes rows that are no longer
    servable first."""
    stats = BuildStats()
    stats.removed = prune_documents(session_factory)
    with session_factory() as session:
        q = (
            select(EvidenceVersion.id)
            .join(Source, Source.id == EvidenceVersion.source_id)
            .where(EvidenceVersion.status == VersionStatus.READY, Source.status == SourceStatus.ACTIVE)
        )
        if source_id:
            q = q.where(EvidenceVersion.source_id == source_id)
        ids = list(session.execute(q).scalars())
        have = {
            vid: ver
            for vid, ver in session.execute(
                select(Document.evidence_version_id, Document.extractor_version)
            ).all()
        }
        if kag:  # a row without graph edges is not current for a KAG build
            has_graph = set(session.execute(text(
                "SELECT d.evidence_version_id FROM documents d WHERE EXISTS "
                "(SELECT 1 FROM kag_document_clauses c WHERE c.document_id = d.id)")).scalars())
            have = {v: x for v, x in have.items() if v in has_graph}
    for vid in ids:
        if not force and have.get(vid) == EXTRACTOR_VERSION:
            stats.skipped += 1
            continue
        if build_for_version(session_factory, vid, kag):
            stats.built += 1
        else:
            stats.skipped += 1
    stats.linked = family.relink_all(session_factory, source_id)
    with session_factory() as session:
        stats.fields = session.execute(text("SELECT COUNT(*) FROM document_fields")).scalar_one()
    return stats


def counts(session_factory: sessionmaker) -> tuple[int, int, int]:
    """(ready_versions, documents, fields) -- for doctor."""
    with session_factory() as session:
        ready = session.execute(
            select(text("COUNT(*)")).select_from(EvidenceVersion).where(EvidenceVersion.status == VersionStatus.READY)
        ).scalar_one()
        docs = session.execute(text("SELECT COUNT(*) FROM documents")).scalar_one()
        flds = session.execute(text("SELECT COUNT(*) FROM document_fields")).scalar_one()
    return ready, docs, flds
