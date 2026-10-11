"""`SourcePruneService` -- remove already-indexed files that the CURRENT
discovery ignore rules (`services/ingestion/ignore.py`) would no longer pick
up (e.g. a virtualenv's site-packages indexed before the rules existed), and
FAILED versions of files that are not real documents (bytes contradict the
extension; discovery now skips those).

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

from docket.core.writer_lock import WriterLock, lock_for_sessions, writer_operation
from docket.core.db.models import EvidenceVersion, Source, VersionStatus
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.index.manager import IndexManager
from docket.infra.parsing.filecheck import InvalidFileError, check_file_signature
from docket.services.ingestion.ignore import IgnoreRules
from docket.services.sources.manager import SourceNotFoundError

_PRUNABLE = (VersionStatus.READY, VersionStatus.FAILED)


def supersede_and_deindex(
    evidence_manager: EvidenceManager,
    index_manager: IndexManager | None,
    version_id: str,
) -> None:
    """The one "this file is no longer served" transition: the version moves
    to SUPERSEDED and its FTS/vector/page index entries are removed. Shared
    by prune (rules now ignore the file) and freshness (the file was deleted
    or renamed away); rows and blobs stay for the usual reference-counted GC."""
    evidence_manager.mark_version_status(version_id, VersionStatus.SUPERSEDED)
    if index_manager is not None:
        index_manager.delete_version(version_id)


def _is_invalid_file(path: Path) -> bool:
    """True when the file exists but its bytes contradict its extension --
    discovery now skips these, so an old FAILED version of one is dead weight."""
    try:
        check_file_signature(path)
    except InvalidFileError:
        return True
    except OSError:
        return False
    return False


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
        writer_lock: WriterLock | None = None,
    ) -> None:
        self._writer_lock = writer_lock or lock_for_sessions(session_factory)
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
            rules_json = source.rules_json
            rows = session.execute(
                select(EvidenceVersion.id, EvidenceVersion.file_path, EvidenceVersion.status).where(
                    EvidenceVersion.source_id == source_id,
                    EvidenceVersion.status.in_(_PRUNABLE),
                )
            ).all()
        # With ignoring disabled nothing counts as ignored, but invalid
        # files are skipped by discovery regardless of that setting.
        rules = IgnoreRules.for_root(root, rules_json) if self._enabled else IgnoreRules()
        out: list[PruneCandidate] = []
        for version_id, file_path, status in rows:
            if not file_path:
                continue
            path = Path(file_path)
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if (self._enabled and rules.path_ignored(rel)) or (
                status == VersionStatus.FAILED and _is_invalid_file(path)
            ):
                out.append(PruneCandidate(version_id, path, rel, status.value))
        out.sort(key=lambda c: c.relative_path.as_posix())
        return out

    def count(self, source_id: str) -> int:
        return len(self.find(source_id))

    @writer_operation("removing files from the index")
    def apply(self, source_id: str) -> list[PruneCandidate]:
        """Supersede and de-index every candidate; returns what was pruned."""
        return self.apply_candidates(self.find(source_id))

    @writer_operation("removing files from the index")
    def apply_candidates(self, candidates: list[PruneCandidate]) -> list[PruneCandidate]:
        """Supersede and de-index exactly these candidates (as found by
        `find`); "Ignore this file" uses it to touch only that one file."""
        for candidate in candidates:
            supersede_and_deindex(
                self._evidence_manager, self._index_manager, candidate.version_id
            )
        return candidates
