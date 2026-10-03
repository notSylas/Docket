"""Chunking recipe definitions used by the parser/chunker pipeline.

A ``ChunkRecipe`` here is the pure-data description of "how a document was
chunked" (size, overlap, splitter strategy, parser identity). Its ``id`` is
the same deterministic id that would land in the ``chunk_recipes`` table (see
``docket.core.db.identity.compute_recipe_id`` / ``docket.core.db.models.ChunkRecipe``),
computed here rather than redefined so callers can key/dedupe recipes before
any DB row exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from docket.core.db.identity import compute_recipe_id

# `_tokcap` marks the checkpoint that added token-bounded chunks and table-aware
# splitting: chunk boundaries changed, so new chunks get a new recipe id.
DEFAULT_SPLITTER = "heading_then_sliding_window_tokcap"


@dataclass(frozen=True)
class ChunkRecipe:
    chunk_size: int
    overlap: int
    splitter: str
    parser_name: str
    parser_version: str

    @property
    def id(self) -> str:
        return compute_recipe_id(
            chunk_size=self.chunk_size,
            overlap=self.overlap,
            splitter=self.splitter,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
        )
