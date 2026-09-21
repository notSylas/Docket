"""Deterministic id helpers for content-addressed rows.

``chunk_recipes.id`` and ``chunks.id`` are not random surrogate keys: they are
derived from the content/config they represent so that re-ingesting the same
evidence with the same chunking recipe produces the same ids. This makes the
primary key itself the dedup mechanism (inserting the same logical chunk
twice raises an IntegrityError instead of silently duplicating rows).
"""

from __future__ import annotations

import hashlib
import json


def compute_recipe_id(
    *,
    chunk_size: int,
    overlap: int,
    splitter: str,
    parser_name: str,
    parser_version: str,
) -> str:
    """Compute the deterministic id for a chunk recipe.

    The id is ``"rcp_" + sha256(json of the recipe fields, sorted keys)[:24]``.
    """
    payload = {
        "chunk_size": chunk_size,
        "overlap": overlap,
        "splitter": splitter,
        "parser_name": parser_name,
        "parser_version": parser_version,
    }
    serialized = json.dumps(payload, sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"rcp_{digest[:24]}"


def compute_chunk_id(
    evidence_version_id: str,
    chunk_recipe_id: str,
    ordinal: int,
    content_hash: str,
) -> str:
    """Compute the deterministic id for a chunk.

    The id is ``"chk_" + sha256(f"{evidence_version_id}|{chunk_recipe_id}|{ordinal}|{content_hash}")``.
    """
    raw = f"{evidence_version_id}|{chunk_recipe_id}|{ordinal}|{content_hash}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"chk_{digest}"
