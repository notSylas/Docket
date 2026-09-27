"""Splits parsed markdown into evidence units and sliding-window chunks.

Pure functions only: no DB access, no id minting beyond the derivable
``content_hash`` values. The caller (a later checkpoint's Index Manager) is
responsible for turning these drafts into real ``EvidenceUnit``/``Chunk``
rows, which requires ``evidence_version_id`` that this module never has.

Page provenance: ``docket.parsing.docling_wrapper`` inserts inline sentinel
comments of the form ``<!--PAGE:{page_no}-->`` into the markdown at points
where Docling's page number changes (see that module's ``_insert_page_markers``).
This module recognizes those markers, strips them out of every unit's/chunk's
``text`` (they must never appear in stored/displayed/embedded text), and uses
their positions to derive ``page_start``/``page_end`` for each unit and chunk.
If a unit/chunk's text contains no marker and none has appeared anywhere
earlier in the document either, both fields are ``None`` -- an honest
"unknown" rather than a guessed page number.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from docket.infra.parsing.recipes import ChunkRecipe

_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.*)$", re.MULTILINE)
_PAGE_MARKER_RE = re.compile(r"<!--PAGE:(\d+)-->")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_page_markers(text: str) -> str:
    return _PAGE_MARKER_RE.sub("", text)


@dataclass
class EvidenceUnitDraft:
    unit_index: int
    heading: str | None
    text: str  # the full section text for this unit (heading line + body), marker-free
    content_hash: str  # sha256 of `text`
    page_start: int | None = None  # 1-indexed page in effect at the start of this unit
    page_end: int | None = None  # 1-indexed page in effect at the end of this unit


@dataclass
class ChunkDraft:
    evidence_unit_index: int  # which EvidenceUnitDraft this belongs to (by unit_index)
    ordinal: int  # position within the FULL document's chunk sequence
    heading: str | None
    text: str  # marker-free
    content_hash: str  # sha256 of `text`
    page_start: int | None = None
    page_end: int | None = None


def _words_with_pages(
    text_with_markers: str, current_page: int | None
) -> tuple[list[str], list[int | None], int | None]:
    """Split ``text_with_markers`` on whitespace into words (identical
    tokenization to ``str.split()``), dropping ``<!--PAGE:N-->`` markers and
    returning, in parallel, the page number in effect for each word.

    ``current_page`` is the page already in effect *before* this text
    begins (``None`` if no marker has been seen anywhere earlier in the
    document). Returns ``(words, word_pages, updated_current_page)`` where
    ``updated_current_page`` is the page in effect after this text, to be
    threaded into the next call so tracking is continuous across units.
    """
    parts = _PAGE_MARKER_RE.split(text_with_markers)
    # re.split with a capturing group yields [text, marker, text, marker, ..., text]
    words: list[str] = []
    pages: list[int | None] = []
    page = current_page

    leading_words = parts[0].split()
    words.extend(leading_words)
    pages.extend([page] * len(leading_words))

    for i in range(1, len(parts), 2):
        page = int(parts[i])
        following_words = parts[i + 1].split()
        words.extend(following_words)
        pages.extend([page] * len(following_words))

    return words, pages, page


def _iter_sections(markdown_text: str) -> list[tuple[str | None, str]]:
    """Yield ``(heading, raw_section_text)`` pairs in document order.

    ``raw_section_text`` may still contain ``<!--PAGE:N-->`` markers --
    callers derive marker-free text/page spans themselves (via
    ``_words_with_pages``/``_strip_page_markers``). Unlike the final
    unit/chunk list, this yields *every* raw section, including ones whose
    marker-stripped content turns out to be empty (e.g. a preamble that is
    only a page marker) -- a section must still be walked for its page
    marker(s) even when it produces no visible unit, or that page
    transition would be silently lost for every section after it. Callers
    are responsible for dropping sections that end up with empty
    marker-stripped text (mirrors the "never produce an empty unit" rule).
    """
    text = markdown_text.strip()
    if not text:
        return []

    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(None, text)]

    sections: list[tuple[str | None, str]] = []

    preamble = text[: matches[0].start()]
    if preamble:
        sections.append((None, preamble))

    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end]
        heading_text = _strip_page_markers(match.group(2)).strip()
        sections.append((heading_text, section))

    return sections


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
    - ``<!--PAGE:N-->`` markers (see module docstring) are stripped from
      ``text`` and drive ``page_start``/``page_end``.
    """
    units: list[EvidenceUnitDraft] = []
    current_page: int | None = None

    for heading, raw_section in _iter_sections(markdown_text):
        # Always walk the section for its page marker(s) -- even one that
        # ends up producing no unit (e.g. a marker-only preamble) still
        # advances the running page tracker for every section after it.
        _, word_pages, current_page = _words_with_pages(raw_section, current_page)
        clean_text = _strip_page_markers(raw_section).strip()
        if not clean_text:
            continue
        units.append(
            EvidenceUnitDraft(
                unit_index=len(units),
                heading=heading,
                text=clean_text,
                content_hash=_sha256_hex(clean_text),
                page_start=word_pages[0] if word_pages else None,
                page_end=word_pages[-1] if word_pages else None,
            )
        )

    return units


def _sliding_window_ranges(n: int, chunk_size: int, overlap: int) -> list[tuple[int, int]]:
    """Index ranges ``[start, end)`` of sliding windows over a sequence of
    length ``n``, ``chunk_size`` long with ``overlap`` shared between
    consecutive windows (stride = chunk_size - overlap). A sequence no
    longer than ``chunk_size`` produces exactly one window (the whole
    sequence, no padding).
    """
    if n == 0:
        return []
    if n <= chunk_size:
        return [(0, n)]

    stride = chunk_size - overlap
    if stride <= 0:
        raise ValueError(
            f"chunk_size ({chunk_size}) must be greater than overlap ({overlap})"
        )

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = start + chunk_size
        ranges.append((start, end))
        if end >= n:
            break
        start += stride
    return ranges


def _sliding_word_windows(words: list[str], chunk_size: int, overlap: int) -> list[list[str]]:
    """Split a word list into sliding windows of ``chunk_size`` words with
    ``overlap`` words shared between consecutive windows (stride =
    chunk_size - overlap). A list no longer than ``chunk_size`` produces
    exactly one window (the whole list, no padding).
    """
    return [words[start:end] for start, end in _sliding_window_ranges(len(words), chunk_size, overlap)]


def chunk_document(
    markdown_text: str, recipe: ChunkRecipe
) -> tuple[list[EvidenceUnitDraft], list[ChunkDraft]]:
    """Split ``markdown_text`` into units, then sub-chunk each unit by
    ``recipe.chunk_size`` words with ``recipe.overlap`` words of sliding
    overlap. ``ordinal`` runs across the whole document's chunk sequence,
    starting at 0, not reset per-unit.

    ``markdown_text`` may contain ``<!--PAGE:N-->`` page-boundary markers
    (see module docstring); they are stripped from every unit's and chunk's
    ``text`` and used to derive ``page_start``/``page_end``. Passing plain,
    marker-free text (e.g. from a caller that doesn't care about page
    provenance) works exactly as before and yields ``page_start=page_end=None``
    everywhere.
    """
    units: list[EvidenceUnitDraft] = []
    chunks: list[ChunkDraft] = []
    current_page: int | None = None
    ordinal = 0

    for heading, raw_section in _iter_sections(markdown_text):
        # Always walk the section for its page marker(s) -- even one that
        # ends up producing no unit (e.g. a marker-only preamble) still
        # advances the running page tracker for every section after it.
        words, word_pages, current_page = _words_with_pages(raw_section, current_page)
        clean_text = _strip_page_markers(raw_section).strip()
        if not clean_text:
            continue
        unit_index = len(units)
        units.append(
            EvidenceUnitDraft(
                unit_index=unit_index,
                heading=heading,
                text=clean_text,
                content_hash=_sha256_hex(clean_text),
                page_start=word_pages[0] if word_pages else None,
                page_end=word_pages[-1] if word_pages else None,
            )
        )

        for start, end in _sliding_window_ranges(len(words), recipe.chunk_size, recipe.overlap):
            window_words = words[start:end]
            window_pages = word_pages[start:end]
            chunk_text = " ".join(window_words)
            chunks.append(
                ChunkDraft(
                    evidence_unit_index=unit_index,
                    ordinal=ordinal,
                    heading=heading,
                    text=chunk_text,
                    content_hash=_sha256_hex(chunk_text),
                    page_start=window_pages[0] if window_pages else None,
                    page_end=window_pages[-1] if window_pages else None,
                )
            )
            ordinal += 1

    return units, chunks
