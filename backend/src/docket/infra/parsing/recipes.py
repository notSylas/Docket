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
from pathlib import Path

from docket.core.db.identity import compute_recipe_id

# `_tokcap` marks the checkpoint that added token-bounded chunks and table-aware
# splitting: chunk boundaries changed, so new chunks get a new recipe id.
# `_tokcap2`: mixed prose+table sections that fit the cap stay one chunk, and
# table padding is normalized.
DEFAULT_SPLITTER = "heading_then_sliding_window_tokcap2"


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


# Row/unit chunkers for native formats: their own splitter ids, so a change to
# the docling splitter above never reports spreadsheets/decks as stale.
XLSX_SPLITTER = "xlsx_row_tokcap2"
PPTX_SPLITTER = "pptx_unit_tokcap2"

FAMILY_DOCLING = "docling"
FAMILY_XLSX = "xlsx"
FAMILY_PPTX = "pptx"


def recipe_family(file_path: str | None) -> str:
    """Parser family for a stored file path (by extension; unknown/legacy
    paths are the Docling text path, matching how ingestion routes files)."""
    suffix = Path(file_path).suffix.lower() if file_path else ""
    if suffix == ".xlsx":
        return FAMILY_XLSX
    if suffix == ".pptx":
        return FAMILY_PPTX
    return FAMILY_DOCLING


def docling_recipe(settings, parser_name: str, parser_version: str) -> ChunkRecipe:
    return ChunkRecipe(
        chunk_size=settings.chunk_size_words,
        overlap=settings.chunk_overlap_words,
        # The cap moves chunk boundaries, so it is part of the recipe identity.
        splitter=f"{DEFAULT_SPLITTER}:max_tokens={settings.chunk_max_tokens}",
        parser_name=parser_name,
        parser_version=parser_version,
    )


def xlsx_recipe(settings, parser_name: str, parser_version: str) -> ChunkRecipe:
    """Hashes only what the row chunker depends on (no word window)."""
    from docket.infra.parsing.xlsx_context import XLSX_CONTEXT_VERSION

    return ChunkRecipe(
        chunk_size=0,
        overlap=0,
        splitter=(
            f"{XLSX_SPLITTER}:max_tokens={settings.chunk_max_tokens}"
            f":period_context={int(bool(settings.xlsx_period_context_enabled))}"
            f":context_v={XLSX_CONTEXT_VERSION}"
        ),
        parser_name=parser_name,
        parser_version=parser_version,
    )


def pptx_recipe(settings, parser_name: str, parser_version: str) -> ChunkRecipe:
    return ChunkRecipe(
        chunk_size=0,
        overlap=0,
        splitter=f"{PPTX_SPLITTER}:max_tokens={settings.chunk_max_tokens}",
        parser_name=parser_name,
        parser_version=parser_version,
    )
