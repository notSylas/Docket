"""`SourcePruneService` -- remove already-indexed files that the CURRENT
discovery ignore rules (`services/ingestion/ignore.py`) would no longer pick
up (e.g. a virtualenv's site-packages indexed before the rules existed).

Mechanism (existing lifecycle, no schema change): each matching READY/FAILED
`EvidenceVersion` is moved to `VersionStatus.SUPERSEDED` via
`EvidenceManager.mark_version_status` -- the state the lifecycle already uses
for "no longer the servable version of this file" -- and its FTS/vector/page
index entries are removed with `IndexManager.delete_version`. Rows and blobs
are left in place: blobs fall to the existing reference-counted trash/GC when
the source is purged. User files on disk are never touched.

If the folder later stops being ignored (setting off / `.docketignore`
edited), the next ingest finds no non-superseded version for those paths and
simply ingests them again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import EvidenceVersion, Source, VersionStatus
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.index.manager import IndexManager
from docket.services.ingestion.ignore import IgnoreRules
from docket.services.sources.manager import SourceNotFoundError

_PRUNABLE = (VersionStatus.READY, VersionStatus.FAILED)


@dataclass(frozen=True)
class PruneCandidate:
    version_id: str
    path: Path
    relative_path: Path
    status: str


class SourcePruneService:
    def __init__(
        self,
        session_factory: sessionmaker,
        evidence_manager: EvidenceManager,
        index_manager: IndexManager | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._evidence_manager = evidence_manager
        self._index_manager = index_manager
        self._enabled = enabled

    def find(self, source_id: str) -> list[PruneCandidate]:
        """READY/FAILED versions whose path, relative to the source root, is
        ignored by the current rules. Empty when ignoring is disabled."""
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            root = Path(source.path)
            rows = session.execute(
                select(EvidenceVersion.id, EvidenceVersion.file_path, EvidenceVersion.status).where(
                    EvidenceVersion.source_id == source_id,
                    EvidenceVersion.status.in_(_PRUNABLE),
                )
            ).all()
        if not self._enabled:
            return []
        rules = IgnoreRules.for_root(root)
        out: list[PruneCandidate] = []
        for version_id, file_path, status in rows:
            if not file_path:
                continue
            path = Path(file_path)
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if rules.path_ignored(rel):
                out.append(PruneCandidate(version_id, path, rel, status.value))
        out.sort(key=lambda c: c.relative_path.as_posix())
        return out

    def count(self, source_id: str) -> int:
        return len(self.find(source_id))

    def apply(self, source_id: str) -> list[PruneCandidate]:
        """Supersede and de-index every candidate; returns what was pruned."""
        candidates = self.find(source_id)
        for candidate in candidates:
            self._evidence_manager.mark_version_status(
                candidate.version_id, VersionStatus.SUPERSEDED
            )
            if self._index_manager is not None:
                self._index_manager.delete_version(candidate.version_id)
        return candidates
