"""Context prefix for what the indexes see of a chunk (design 04 §8).

A chunk's embedded text and its FTS5 text are `"<file name> > <h1> > <h2>\\n<chunk
text>"`, so words that live only in the file name or the heading breadcrumb
still match and embed, for every window of a long section. `Chunk.text`
(SQLite, what citations and the resolver quote) and the LanceDB `text` column
stay verbatim: the prefix exists only in the vector and the FTS5 row.

Ingestion (`ChunkWriter`) and `docket reindex` both call `index_text_for_chunk`,
so a rebuilt index is identical to a freshly ingested one. Deterministic, no
model calls. Only the file's basename is used, never directory names.
"""

from __future__ import annotations

import json
from typing import Sequence

# Recorded in the index manifest (`index_text_version`); bump if the format changes.
INDEX_TEXT_VERSION = 1


def build_index_text(file_name: str | None, heading_path: Sequence[str], text: str) -> str:
    """`text` prefixed with `file name > heading > ...` on one line. Missing or
    blank segments are omitted; with none left, `text` is returned unchanged."""
    name = _basename(file_name)
    segments = [s for s in (name, *(str(h).strip() for h in heading_path)) if s]
    if not segments:
        return text
    return " > ".join(segments) + "\n" + text


def index_text_for_chunk(
    file_path: str | None, locator_json: str | None, heading: str | None, text: str
) -> str:
    """`build_index_text` from a chunk's stored fields: the unit's
    `heading_path` when it has one, else the chunk's own `heading` (XLSX sheet,
    PPTX "Slide N")."""
    path = _heading_path(locator_json)
    if not path and heading:
        path = [heading]
    return build_index_text(file_path, path, text)


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
