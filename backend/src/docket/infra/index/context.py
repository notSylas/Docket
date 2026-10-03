"""Context prefix for what the indexes see of a chunk (design 04 §8).

A chunk's embedded text and its FTS5 text are `"<file name> > <h1> > <h2>\\n<chunk
text>"`, so words that live only in the file name or the heading breadcrumb
still match and embed, for every window of a long section. `Chunk.text`
(SQLite, what citations and the resolver quote) and the LanceDB `text` column
stay verbatim: the prefix exists only in the vector and the FTS5 row.

Ingestion (`ChunkWriter`) and `docket reindex` both call `index_text_for_chunk`,
so a rebuilt index is identical to a freshly ingested one. Deterministic, no
model calls. Only the file's basename is used, never directory names.

Version 2 (doc 05 step 3): a spreadsheet row whose unit locator carries a
`context` list (fiscal-year labels, units, month names; see
`docket.infra.parsing.xlsx_context`) gets one extra line,
`Context: a; b; c`, between the `<file> > <heading>` line and the row text.
Other chunks are unchanged. Indexes built at version 1 (or adopted/legacy
ones, which read as 0) lack that line and need `docket reindex`; rows only have
the locator context if they were re-chunked after the feature shipped.
"""

from __future__ import annotations

import json
from typing import Sequence

# Recorded in the index manifest (`index_text_version`); bump if the format changes.
INDEX_TEXT_VERSION = 2

# Hard cap on the rendered `Context:` line (characters, excluding the label).
CONTEXT_LINE_MAX_CHARS = 400


def build_index_text(
    file_name: str | None,
    heading_path: Sequence[str],
    text: str,
    context: Sequence[str] = (),
) -> str:
    """`text` prefixed with `file name > heading > ...` on one line, then an
    optional `Context: ...` line. Missing or blank segments are omitted; with
    none left, `text` is returned unchanged."""
    name = _basename(file_name)
    segments = [s for s in (name, *(str(h).strip() for h in heading_path)) if s]
    context_line = _context_line(context)
    if not segments and not context_line:
        return text
    prefix = " > ".join(segments)
    if context_line:
        prefix = prefix + "\n" + context_line if prefix else context_line
    return prefix + "\n" + text


def index_text_for_chunk(
    file_path: str | None, locator_json: str | None, heading: str | None, text: str
) -> str:
    """`build_index_text` from a chunk's stored fields: the unit's
    `heading_path` when it has one, else the chunk's own `heading` (XLSX sheet,
    PPTX "Slide N")."""
    path = _heading_path(locator_json)
    if not path and heading:
        path = [heading]
    return build_index_text(file_path, path, text, _context(locator_json))


def _basename(file_path: str | None) -> str:
    if not file_path:
        return ""
    return file_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()


def _heading_path(locator_json: str | None) -> list[str]:
    if not locator_json:
        return []
    try:
        path = json.loads(locator_json).get("heading_path")
    except (ValueError, AttributeError):
        return []
    return [h for h in path if isinstance(h, str)] if isinstance(path, list) else []


def _context(locator_json: str | None) -> list[str]:
    if not locator_json:
        return []
    try:
        items = json.loads(locator_json).get("context")
    except (ValueError, AttributeError):
        return []
    return [i for i in items if isinstance(i, str)] if isinstance(items, list) else []


def _context_line(context: Sequence[str]) -> str:
    joined = "; ".join(c.strip() for c in context if c and c.strip())
    if not joined:
        return ""
    return "Context: " + joined[:CONTEXT_LINE_MAX_CHARS]
