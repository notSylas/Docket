"""Splits parsed markdown into evidence units and sliding-window chunks.

Pure functions only: no DB access, no id minting beyond the derivable
``content_hash`` values. The caller (a later checkpoint's Index Manager) is
responsible for turning these drafts into real ``EvidenceUnit``/``Chunk``
rows, which requires ``evidence_version_id`` that this module never has.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from docket.parsing.recipes import ChunkRecipe

_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.*)$", re.MULTILINE)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class EvidenceUnitDraft:
    unit_index: int
    heading: str | None
    text: str  # the full section text for this unit (heading line + body)
    content_hash: str  # sha256 of `text`


@dataclass
class ChunkDraft:
    evidence_unit_index: int  # which EvidenceUnitDraft this belongs to (by unit_index)
    ordinal: int  # position within the FULL document's chunk sequence
    heading: str | None
    text: str
    content_hash: str  # sha256 of `text`


def _make_unit(unit_index: int, heading: str | None, text: str) -> EvidenceUnitDraft:
    return EvidenceUnitDraft(
        unit_index=unit_index,
        heading=heading,
        text=text,
        content_hash=_sha256_hex(text),
    )


def split_into_units(markdown_text: str) -> list[EvidenceUnitDraft]:
    """Split markdown into sections on ATX heading boundaries (#, ##, ###).

    - No headings at all: the whole (stripped) document is a single unit
      with ``heading=None``.
    - Text before the first heading (if any): its own unit with
      ``heading=None``.
    - Each heading starts a new unit running up to (not including) the next
      heading; the heading line itself is included in that unit's text.
    - Whitespace-only sections (e.g. from a doc that is empty, or trailing
      whitespace after the last heading) are dropped -- never produce an
      empty unit. Consecutive headings with no body between them each still
      produce their own (non-empty, heading-only) unit rather than being
      merged or dropped.
    """
    text = markdown_text.strip()
    if not text:
        return []

    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [_make_unit(0, None, text)]

    units: list[EvidenceUnitDraft] = []

    preamble = text[: matches[0].start()].strip()
    if preamble:
        units.append(_make_unit(len(units), None, preamble))

    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        if not section:
            continue
        heading_text = match.group(2).strip()
        units.append(_make_unit(len(units), heading_text, section))

    return units


def _sliding_word_windows(words: list[str], chunk_size: int, overlap: int) -> list[list[str]]:
    """Split a word list into sliding windows of ``chunk_size`` words with
    ``overlap`` words shared between consecutive windows (stride =
    chunk_size - overlap). A list no longer than ``chunk_size`` produces
    exactly one window (the whole list, no padding).
    """
    n = len(words)
    if n == 0:
        return []
    if n <= chunk_size:
        return [words]

    stride = chunk_size - overlap
    if stride <= 0:
        raise ValueError(
            f"chunk_size ({chunk_size}) must be greater than overlap ({overlap})"
        )

    windows: list[list[str]] = []
    start = 0
    while start < n:
        end = start + chunk_size
        windows.append(words[start:end])
        if end >= n:
            break
        start += stride
    return windows


def chunk_document(
    markdown_text: str, recipe: ChunkRecipe
) -> tuple[list[EvidenceUnitDraft], list[ChunkDraft]]:
    """Split ``markdown_text`` into units, then sub-chunk each unit by
    ``recipe.chunk_size`` words with ``recipe.overlap`` words of sliding
    overlap. ``ordinal`` runs across the whole document's chunk sequence,
    starting at 0, not reset per-unit.
    """
    units = split_into_units(markdown_text)
    chunks: list[ChunkDraft] = []
    ordinal = 0

    for unit in units:
        words = unit.text.split()
        for window in _sliding_word_windows(words, recipe.chunk_size, recipe.overlap):
            chunk_text = " ".join(window)
            chunks.append(
                ChunkDraft(
                    evidence_unit_index=unit.unit_index,
                    ordinal=ordinal,
                    heading=unit.heading,
                    text=chunk_text,
                    content_hash=_sha256_hex(chunk_text),
                )
            )
            ordinal += 1

    return units, chunks
