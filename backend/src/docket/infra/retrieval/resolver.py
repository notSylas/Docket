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

from docket.core.db.models import Chunk, EvidenceUnit, EvidenceVersion, Source, SourceStatus, VersionStatus


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
    # One-line, human-readable position derived from stored structure (see
    # `format_location`); `None` when the chunk has none. Shown in the context
    # block and never part of `text`.
    location: str | None = None
    # Spreadsheet chunks only: the sheet name and the unit's stored context
    # lines (`locator_json["context"]`: fiscal-year labels, units/scope lines).
    # Metadata for query-time signals (ambiguity detection); never evidence.
    sheet: str | None = None
    context: list[str] = field(default_factory=list)


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


def _clean(value: object) -> str:
    """One line, with no characters that could form a citation tag."""
    return " ".join(str(value).replace("[", "(").replace("]", ")").replace("#", "").split())


def format_location(
    *,
    file_name: str,
    heading: str | None,
    unit_kind: str | None,
    locator_json: str | None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> str | None:
    """Location line for a chunk, from the unit's stored locator (not the
    chunk text): ``Section: A > B (pages 4-5)`` for Docling sections,
    ``Location: file.xlsx > Sheet, range B5:F5`` for XLSX,
    ``Location: deck.pptx > Slide 3, table "T", row 2`` for PPTX. `None` when
    nothing is known. Never contains ``[``, ``]`` or ``#``."""
    try:
        locator = json.loads(locator_json) if locator_json else {}
    except ValueError:
        locator = {}
    if not isinstance(locator, dict):
        locator = {}

    pages = ""
    if page_start is not None:
        if page_end is None or page_end == page_start:
            pages = f"page {page_start}"
        else:
            pages = f"pages {page_start}-{page_end}"

    part = ""
    if "part" in locator and "of" in locator:
        part = f", part {locator['part']} of {locator['of']}"

    if "sheet" in locator:
        text = f"Location: {_clean(file_name)} > {_clean(locator['sheet'])}"
        if locator.get("range"):
            text += f", range {_clean(locator['range'])}"
        return text + part
    if "slide" in locator:
        text = f"Location: {_clean(file_name)} > Slide {_clean(locator['slide'])}"
        name = locator.get("shape_name")
        if unit_kind == "notes":
            text += ", speaker notes"
        elif unit_kind == "table_row":
            text += f", table \"{_clean(name)}\"" if name else ", table"
            if "row" in locator:
                text += f", row {_clean(locator['row'])}"
        elif unit_kind == "chart_data":
            text += f", chart \"{_clean(name)}\"" if name else ", chart"
        elif name:
            text += f", shape \"{_clean(name)}\""
        return text + part

    path = locator.get("heading_path")
    segments = [_clean(h) for h in path if isinstance(h, str)] if isinstance(path, list) else []
    if not segments and heading:
        segments = [_clean(heading)]
    segments = [s for s in segments if s]
    if segments:
        text = "Section: " + " > ".join(segments)
        return f"{text} ({pages})" if pages else text
    return f"Location: {pages}" if pages else None


def _locator_sheet_and_context(locator_json: str | None) -> tuple[str | None, list[str]]:
    try:
        locator = json.loads(locator_json) if locator_json else {}
    except ValueError:
        return None, []
    if not isinstance(locator, dict):
        return None, []
    sheet = locator.get("sheet")
    context = locator.get("context")
    return (
        sheet if isinstance(sheet, str) else None,
        [c for c in context if isinstance(c, str)] if isinstance(context, list) else [],
    )


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
        silently drops missing ids. Inactive sources and non-servable
        versions (not `READY` -- still `PENDING`, `FAILED`, or superseded by
        newer content) are unavailable, even when their rows remain for
        historical provenance."""
        if not chunk_ids:
            return []

        with self._session_factory() as session:
            rows = session.execute(
                select(Chunk, Source, EvidenceVersion, EvidenceUnit)
                .join(Source, Chunk.source_id == Source.id)
                .join(EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id)
                .join(EvidenceUnit, Chunk.evidence_unit_id == EvidenceUnit.id)
                .where(Chunk.id.in_(chunk_ids))
                .where(Source.status == SourceStatus.ACTIVE)
                .where(EvidenceVersion.status == VersionStatus.READY)
            ).all()

        by_id = {chunk.id: (chunk, source, ev, unit) for chunk, source, ev, unit in rows}

        resolved: list[ResolvedEvidence] = []
        for chunk_id in chunk_ids:
            if chunk_id not in by_id:
                raise ChunkNotFoundError(chunk_id)
            chunk, source, evidence_version, unit = by_id[chunk_id]
            # Prefer the chunk's own file (CP8's multi-file "local_folder"
            # sources put several files under one Source row, each tracked
            # via EvidenceVersion.file_path -- see migration
            # 0002_evidence_version_file_path); fall back to Source.path for
            # pre-CP8 rows where a source mapped to exactly one file and
            # file_path was never recorded.
            display_path = evidence_version.file_path or source.path
            source_display_name = Path(display_path).name
            sheet, context = _locator_sheet_and_context(unit.locator_json)
            resolved.append(
                ResolvedEvidence(
                    chunk_id=chunk.id,
                    text=chunk.text,
                    source_display_name=source_display_name,
                    evidence_version_id=chunk.evidence_version_id,
                    heading=chunk.heading,
                    citation_label=_citation_label(source_display_name, chunk.id),
                    source_id=source.id,
                    sheet=sheet,
                    context=context,
                    formula_regions=json.loads(evidence_version.formula_regions_json or "[]"),
                    location=format_location(
                        file_name=source_display_name,
                        heading=chunk.heading,
                        unit_kind=unit.unit_kind,
                        locator_json=unit.locator_json,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                    ),
                )
            )
        return resolved
