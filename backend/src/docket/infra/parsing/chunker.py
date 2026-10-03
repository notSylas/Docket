"""Splits parsed markdown into evidence units and sliding-window chunks.

Pure functions only: no DB access, no id minting beyond the derivable
``content_hash`` values. The caller (a later checkpoint's Index Manager) is
responsible for turning these drafts into real ``EvidenceUnit``/``Chunk``
rows, which requires ``evidence_version_id`` that this module never has.

Page provenance: ``docket.infra.parsing.docling_wrapper`` inserts inline sentinel
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
import json
import re
from dataclasses import dataclass

from docket.core.config import settings
from docket.infra.parsing.recipes import ChunkRecipe
from docket.infra.parsing.tokens import TokenCounter, get_token_counter, pack_parts, split_to_fit

_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.*)$", re.MULTILINE)
_PAGE_MARKER_RE = re.compile(r"<!--PAGE:(\d+)-->")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


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
    # Structural-element kind (`EvidenceUnit.unit_kind`) and kind-specific
    # structured locator (`EvidenceUnit.locator_json`, already JSON-encoded
    # by the caller) -- see `docket.core.db.models.EvidenceUnit`. Defaulted to
    # the only kind that existed before CP "xlsx ingestion" (a Docling
    # markdown-heading section with no structured locator) so every existing
    # caller of this module (the Docling path) is unaffected; a non-Docling
    # adapter (e.g. `docket.infra.parsing.xlsx_chunker`) builds its own
    # `EvidenceUnitDraft`s directly rather than through `chunk_document`, and
    # sets these explicitly.
    unit_kind: str = "section"
    locator_json: str | None = None


@dataclass
class ChunkDraft:
    evidence_unit_index: int  # which EvidenceUnitDraft this belongs to (by unit_index)
    ordinal: int  # position within the FULL document's chunk sequence
    heading: str | None
    text: str  # marker-free
    content_hash: str  # sha256 of `text`
    page_start: int | None = None
    page_end: int | None = None
    # `Chunk.provenance` -- see that column's docstring in
    # `docket.core.db.models`. Every chunk produced by this module comes
    # straight from parsed document text, so `"extracted"` is the correct
    # default for all existing and new callers of `chunk_document`.
    provenance: str = "extracted"


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


def _iter_sections(markdown_text: str) -> list[tuple[str | None, str, list[str]]]:
    """Yield ``(heading, raw_section_text, heading_path)`` triples in document
    order. ``heading_path`` is the heading plus its ancestors (levels 1-3,
    outermost first); empty before the first heading.

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
        return [(None, text, [])]

    sections: list[tuple[str | None, str, list[str]]] = []

    preamble = text[: matches[0].start()]
    if preamble:
        sections.append((None, preamble, []))

    stack: list[tuple[int, str]] = []  # (level, heading) of the open ancestors

    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end]
        heading_text = _strip_page_markers(match.group(2)).strip()
        level = len(match.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading_text))
        sections.append((heading_text, section, [h for _, h in stack]))

    return sections


def _heading_locator(heading_path: list[str]) -> str | None:
    """`locator_json` for a Docling section unit: its heading path as
    structure (the leaf stays in `heading`). `None` before any heading."""
    if not heading_path:
        return None
    return json.dumps({"heading_path": heading_path}, sort_keys=True)


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

    for heading, raw_section, heading_path in _iter_sections(markdown_text):
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
                locator_json=_heading_locator(heading_path),
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


def _fit_word_ranges(
    words: list[str], start: int, end: int, counter: TokenCounter, cap: int
) -> list[tuple[int, int]]:
    """Halve ``words[start:end]`` until each piece is within ``cap`` tokens.
    A single word over the cap is returned as is (the caller cuts it by
    characters)."""
    if end - start <= 1 or counter.count(" ".join(words[start:end])) <= cap:
        return [(start, end)]
    mid = (start + end) // 2
    return _fit_word_ranges(words, start, mid, counter, cap) + _fit_word_ranges(
        words, mid, end, counter, cap
    )


def _window_chunks(
    words: list[str],
    word_pages: list[int | None],
    recipe: ChunkRecipe,
    counter: TokenCounter,
    cap: int,
) -> list[tuple[str, int | None, int | None]]:
    """``(text, page_start, page_end)`` per chunk for plain prose: the usual
    sliding word windows, with any window over ``cap`` tokens split further
    by words. Pieces split from one window do not overlap each other; the
    windows they came from still do."""
    out: list[tuple[str, int | None, int | None]] = []
    for w_start, w_end in _sliding_window_ranges(len(words), recipe.chunk_size, recipe.overlap):
        for start, end in _fit_word_ranges(words, w_start, min(w_end, len(words)), counter, cap):
            text = " ".join(words[start:end])
            pieces = split_to_fit(text, counter, cap) if end - start == 1 else [text]
            for piece in pieces:
                out.append((piece, word_pages[start], word_pages[end - 1]))
    return out


def _segment_section(raw_section: str) -> list[tuple[str, list[str]]]:
    """Split a raw section into ``("text", lines)`` and ``("table", lines)``
    segments. A table is a Markdown pipe table: a ``|`` header row, a
    ``|---|`` separator row, then ``|`` data rows. Page-marker-only lines
    inside a table are kept with it, so they do not end the table."""
    lines = raw_section.split("\n")
    clean = [_strip_page_markers(line) for line in lines]
    marker_only = [not c.strip() and line != c for line, c in zip(lines, clean)]

    def next_content(i: int) -> int | None:
        while i < len(lines) and marker_only[i]:
            i += 1
        return i if i < len(lines) else None

    segments: list[tuple[str, list[str]]] = []
    text_lines: list[str] = []
    i = 0
    while i < len(lines):
        j = next_content(i + 1) if _TABLE_ROW_RE.match(clean[i]) else None
        if j is not None and _TABLE_SEPARATOR_RE.match(clean[j]):
            if text_lines:
                segments.append(("text", text_lines))
                text_lines = []
            table_lines = lines[i : j + 1]
            i = j + 1
            while True:
                k = next_content(i)
                if k is None or not _TABLE_ROW_RE.match(clean[k]):
                    break
                table_lines.extend(lines[i : k + 1])
                i = k + 1
            segments.append(("table", table_lines))
        else:
            text_lines.append(lines[i])
            i += 1
    if text_lines:
        segments.append(("text", text_lines))
    return segments


def _cells(row: str) -> list[str]:
    row = row.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") else row
    return [c.strip() for c in row.split("|")]


def _render_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _split_row_by_cells(
    header: str, separator: str, row: str, counter: TokenCounter, cap: int
) -> list[str]:
    """Last resort for a single row too big for ``cap``: pack its cells into
    narrower tables, each with the matching slice of the header and separator
    so columns stay labelled. A single oversized cell is cut by words."""
    head, sep, body = _cells(header), _cells(separator), _cells(row)
    sep += ["---"] * (len(body) - len(sep))
    head += [""] * (len(body) - len(head))

    def render(lo: int, hi: int, cells: list[str] | None = None) -> str:
        return "\n".join(
            _render_row(part)
            for part in (head[lo:hi], sep[lo:hi], cells if cells is not None else body[lo:hi])
        )

    out: list[str] = []
    lo = 0
    while lo < len(body):
        hi = lo + 1
        while hi < len(body) and counter.count(render(lo, hi + 1)) <= cap:
            hi += 1
        if hi == lo + 1 and counter.count(render(lo, hi)) > cap:
            # `- 4`: room for the cell's surrounding pipes.
            prefix = render(lo, hi, [""])
            out.extend(render(lo, hi, [p]) for p in split_to_fit(body[lo], counter, cap - 4, prefix))
        else:
            out.append(render(lo, hi))
        lo = hi
    return out


def _table_chunks(
    lines: list[str], page: int | None, counter: TokenCounter, cap: int
) -> tuple[list[tuple[str, int | None, int | None]], int | None]:
    """``(text, page_start, page_end)`` per chunk for one pipe table, plus the
    page in effect after it. Newlines are kept (unlike prose chunks) so rows
    stay rows. Cuts only fall between rows, and the header and separator rows
    are repeated at the top of every part, so a part is self-describing. That
    repetition duplicates extracted content on purpose: ``Chunk.text`` of a
    later part is not a verbatim slice of the document. A row too big for
    ``cap`` on its own is split by cells."""
    rows: list[tuple[str, int | None]] = []
    for line in lines:
        words, pages, page = _words_with_pages(line, page)
        text = _strip_page_markers(line).strip()
        if text:
            rows.append((text, pages[0] if words else page))
    texts = [t for t, _ in rows]

    if counter.count("\n".join(texts)) <= cap:
        return [("\n".join(texts), rows[0][1], rows[-1][1])], page

    header = "\n".join(texts[:2])
    # A header that takes up half the cap leaves no room to repeat; then only
    # the first part carries it.
    repeat = header if counter.count(header) <= cap // 2 else ""
    start = 2 if repeat else 0
    parts = texts[start:]
    out: list[tuple[str, int | None, int | None]] = []
    index = start
    for group in pack_parts(parts, "\n", counter, cap, repeat):
        # The first part also covers the header row's page.
        first = rows[0][1] if index == start else rows[index][1]
        last = rows[index + len(group) - 1][1]
        body = "\n".join(group)
        text = f"{repeat}\n{body}" if repeat else body
        if len(group) == 1 and counter.count(text) > cap and repeat:
            for piece in _split_row_by_cells(texts[0], texts[1], group[0], counter, cap):
                out.append((piece, first, last))
        elif len(group) == 1 and counter.count(text) > cap:
            for piece in split_to_fit(body, counter, cap):
                out.append((piece, first, last))
        else:
            out.append((text, first, last))
        index += len(group)
    return out, page


def chunk_document(
    markdown_text: str,
    recipe: ChunkRecipe,
    counter: TokenCounter | None = None,
    max_tokens: int | None = None,
) -> tuple[list[EvidenceUnitDraft], list[ChunkDraft]]:
    """Split ``markdown_text`` into units, then sub-chunk each unit by
    ``recipe.chunk_size`` words with ``recipe.overlap`` words of sliding
    overlap, never exceeding ``max_tokens`` (default
    ``settings.chunk_max_tokens``) as measured by ``counter`` (default
    ``get_token_counter()``). Pipe tables inside a unit are chunked
    separately, on row boundaries (see ``_table_chunks``). ``ordinal`` runs
    across the whole document's chunk sequence, starting at 0, not reset
    per-unit.

    ``markdown_text`` may contain ``<!--PAGE:N-->`` page-boundary markers
    (see module docstring); they are stripped from every unit's and chunk's
    ``text`` and used to derive ``page_start``/``page_end``. Passing plain,
    marker-free text (e.g. from a caller that doesn't care about page
    provenance) works exactly as before and yields ``page_start=page_end=None``
    everywhere.
    """
    counter = counter or get_token_counter()
    cap = max_tokens or settings.chunk_max_tokens
    units: list[EvidenceUnitDraft] = []
    chunks: list[ChunkDraft] = []
    current_page: int | None = None
    ordinal = 0

    for heading, raw_section, heading_path in _iter_sections(markdown_text):
        # Always walk the section for its page marker(s) -- even one that
        # ends up producing no unit (e.g. a marker-only preamble) still
        # advances the running page tracker for every section after it.
        section_start_page = current_page
        _, word_pages, current_page = _words_with_pages(raw_section, current_page)
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
                locator_json=_heading_locator(heading_path),
            )
        )

        drafts: list[tuple[str, int | None, int | None]] = []
        page = section_start_page
        for kind, seg_lines in _segment_section(raw_section):
            if kind == "table":
                table_drafts, page = _table_chunks(seg_lines, page, counter, cap)
                drafts.extend(table_drafts)
            else:
                words, pages, page = _words_with_pages("\n".join(seg_lines), page)
                drafts.extend(_window_chunks(words, pages, recipe, counter, cap))

        for chunk_text, page_start, page_end in drafts:
            chunks.append(
                ChunkDraft(
                    evidence_unit_index=unit_index,
                    ordinal=ordinal,
                    heading=heading,
                    text=chunk_text,
                    content_hash=_sha256_hex(chunk_text),
                    page_start=page_start,
                    page_end=page_end,
                )
            )
            ordinal += 1

    return units, chunks
