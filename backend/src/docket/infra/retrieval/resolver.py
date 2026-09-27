"""Resolves chunk_ids (as returned by `docket.infra.retrieval.hybrid`) into
citation-ready evidence.

This is the ONE place in the codebase that builds citation tags. The
validation spike (see `spike/RESULTS.md`, "Known rough edges") let the
generation prompt construct citation tags ad hoc from parts (e.g.
`[source_file#chunk_id]`), and the model sometimes mangled the format --
emitting malformed tags like `[source_file#chunk_id: file.docx#chunk_id]`.
Centralizing tag construction here means a system prompt only has to say
"use the citation_label exactly as given," never "build a tag from these
parts."
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import Chunk, EvidenceVersion, Source, SourceStatus


@dataclass(frozen=True)
class ResolvedEvidence:
    chunk_id: str
    text: str
    source_display_name: str  # e.g. the source's path/filename, human-readable
    evidence_version_id: str
    heading: str | None
    citation_label: str  # centralized, correctly-formatted citation tag
    source_id: str = ""
    formula_regions: list[dict] = field(default_factory=list)  # document-level coordinates


class ChunkNotFoundError(Exception):
    def __init__(self, chunk_id: str):
        self.chunk_id = chunk_id
        super().__init__(f"chunk not found: {chunk_id}")


def _citation_label(source_display_name: str, chunk_id: str) -> str:
    """Build the citation tag for a chunk.

    Format chosen: ``[{source_display_name} #{chunk_id[:12]}]``, e.g.
    ``[report.pdf #a1b2c3d4e5f6]``. Rationale:
    - Square brackets make it visually unambiguous as a citation, matching
      the bracketed-tag convention already used in the spike's prompt.
    - Leading with the human-readable filename means a citation is legible
      at a glance even without resolving the id.
    - The chunk_id is truncated to 12 hex characters -- enough to disambiguate
      between chunks from the same source without bloating every citation
      with a 68-character `chk_<sha256>` string. This is a display label
      only; nothing parses it back into a full chunk_id (lookups use the
      resolver's own `resolve()`/`resolve_many()`, not label parsing).
    - Single format, single call site (this function) -- nothing else in the
      codebase should construct a citation string by hand.
    """
    return f"[{source_display_name} #{chunk_id[:12]}]"


class EvidenceResolver:
    """Resolves chunk_ids into citation-ready `ResolvedEvidence`.

    Reads from the same `chunks` table CP5's index writers ingest into (via
    the ORM, joining `Source` and `EvidenceVersion` for a display name --
    the chunk's own file when known, else the source's path, see
    `resolve_many`) -- this class only reads, it never writes.
    """

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def resolve(self, chunk_id: str) -> ResolvedEvidence:
        """Look up the `Chunk` row (joining `Source` for a display name) and
        build a `ResolvedEvidence`. Raises `ChunkNotFoundError` if `chunk_id`
        doesn't exist in the `chunks` table."""
        return self.resolve_many([chunk_id])[0]

    def resolve_many(self, chunk_ids: list[str]) -> list[ResolvedEvidence]:
        """Batched version of `resolve` -- one query for all ids, not N.
        Preserves the input order in the output. Raises `ChunkNotFoundError`
        on the first missing id it encounters (in input order); it never
        silently drops missing ids. Inactive sources and superseded versions
        are unavailable, even when their rows remain for historical provenance."""
        if not chunk_ids:
            return []

        with self._session_factory() as session:
            rows = session.execute(
                select(Chunk, Source, EvidenceVersion)
                .join(Source, Chunk.source_id == Source.id)
                .join(EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id)
                .where(Chunk.id.in_(chunk_ids))
                .where(Source.status == SourceStatus.ACTIVE)
                .where(EvidenceVersion.is_current.is_(True))
            ).all()

        by_id = {chunk.id: (chunk, source, ev) for chunk, source, ev in rows}

        resolved: list[ResolvedEvidence] = []
        for chunk_id in chunk_ids:
            if chunk_id not in by_id:
                raise ChunkNotFoundError(chunk_id)
            chunk, source, evidence_version = by_id[chunk_id]
            # Prefer the chunk's own file (CP8's multi-file "local_folder"
            # sources put several files under one Source row, each tracked
            # via EvidenceVersion.file_path -- see migration
            # 0002_evidence_version_file_path); fall back to Source.path for
            # pre-CP8 rows where a source mapped to exactly one file and
            # file_path was never recorded.
            display_path = evidence_version.file_path or source.path
            source_display_name = Path(display_path).name
            resolved.append(
                ResolvedEvidence(
                    chunk_id=chunk.id,
                    text=chunk.text,
                    source_display_name=source_display_name,
                    evidence_version_id=chunk.evidence_version_id,
                    heading=chunk.heading,
                    citation_label=_citation_label(source_display_name, chunk.id),
                    source_id=source.id,
                    formula_regions=json.loads(evidence_version.formula_regions_json or "[]"),
                )
            )
        return resolved
