"""Cheap evidence eligibility for terminal status views; no parser or inference."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import Chunk, EvidenceVersion, Source, SourceStatus, VersionStatus


@dataclass(frozen=True)
class SearchReadiness:
    files: int
    chunks: int


class ReadinessService:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def snapshot(self) -> SearchReadiness:
        """Count eligible versions with chunks, under the resolver's rules.

        READY is written after indexing succeeds. An existing vector table
        alone proves nothing about connected, servable evidence. These counts
        describe SQLite eligibility, not a full index consistency audit.
        """
        stmt = (
            select(func.count(func.distinct(EvidenceVersion.id)), func.count(Chunk.id))
            .select_from(Chunk)
            .join(Source, Chunk.source_id == Source.id)
            .join(EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id)
            .where(Source.status == SourceStatus.ACTIVE)
            .where(EvidenceVersion.status == VersionStatus.READY)
        )
        with self._session_factory() as session:
            files, chunks = session.execute(stmt).one()
        return SearchReadiness(files=files, chunks=chunks)
