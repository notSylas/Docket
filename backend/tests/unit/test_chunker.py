"""Tests for docket.parsing.chunker (split_into_units, chunk_document)."""

from __future__ import annotations

import hashlib

import pytest

from docket.parsing.chunker import chunk_document, split_into_units
from docket.parsing.recipes import ChunkRecipe


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _recipe(chunk_size: int = 200, overlap: int = 40) -> ChunkRecipe:
    return ChunkRecipe(
        chunk_size=chunk_size,
        overlap=overlap,
        splitter="heading_then_sliding_window",
        parser_name="docling",
        parser_version="test-version",
    )


def _words(n: int, prefix: str = "word") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


# ---------------------------------------------------------------------------
# split_into_units
# ---------------------------------------------------------------------------


def test_split_no_heading_document_is_one_unit() -> None:
    text = "Just some plain text.\n\nNo headings anywhere in this document."
    units = split_into_units(text)
    assert len(units) == 1
    assert units[0].heading is None
    assert units[0].unit_index == 0
    assert units[0].text == text.strip()


def test_split_multiple_headings_correct_count_and_boundaries() -> None:
    text = (
        "# Title\n"
        "Intro text.\n\n"
        "## Section A\n"
        "Body of section A.\n\n"
        "## Section B\n"
        "Body of section B.\n"
    )
    units = split_into_units(text)
    assert len(units) == 3
    assert [u.heading for u in units] == ["Title", "Section A", "Section B"]
    assert [u.unit_index for u in units] == [0, 1, 2]
    assert "Intro text." in units[0].text
    assert "Body of section A." in units[1].text
    assert "Body of section B." in units[2].text
    # Boundaries: section A's text must not leak into section B's.
    assert "Section B" not in units[1].text
    assert "Body of section A" not in units[2].text


def test_split_preamble_before_first_heading_is_its_own_unit() -> None:
    text = "Preamble with no heading.\n\n# First Heading\nBody text.\n"
    units = split_into_units(text)
    assert len(units) == 2
    assert units[0].heading is None
    assert "Preamble" in units[0].text
    assert units[1].heading == "First Heading"


def test_split_consecutive_headings_with_no_body_produce_no_empty_units() -> None:
    text = "# Heading One\n## Heading Two\nOnly this has a body.\n"
    units = split_into_units(text)
    # No blank/whitespace-only unit is produced; each heading still gets
    # its own unit even though "Heading One" has no body text of its own.
    assert all(u.text.strip() for u in units)
    assert [u.heading for u in units] == ["Heading One", "Heading Two"]
    assert units[1].text.strip().endswith("Only this has a body.")


def test_split_empty_document_produces_no_units() -> None:
    assert split_into_units("") == []
    assert split_into_units("   \n\n   ") == []


def test_split_heading_text_captured_correctly() -> None:
    text = "### A Heading With   Extra   Words\nBody.\n"
    units = split_into_units(text)
    assert units[0].heading == "A Heading With   Extra   Words"


def test_split_content_hash_matches_sha256_of_text() -> None:
    text = "# H\nSome body text here.\n"
    units = split_into_units(text)
    assert units[0].content_hash == _sha256(units[0].text)


# ---------------------------------------------------------------------------
# chunk_document
# ---------------------------------------------------------------------------


def test_chunk_ordinals_are_deterministic_and_span_whole_document() -> None:
    text = (
        "# Section A\n" + _words(250, "a") + "\n\n"
        "# Section B\n" + _words(250, "b") + "\n"
    )
    recipe = _recipe(chunk_size=200, overlap=40)
    units, chunks = chunk_document(text, recipe)

    assert len(units) == 2
    ordinals = [c.ordinal for c in chunks]
    assert ordinals == list(range(len(chunks)))  # 0, 1, 2, ... with no reset per-unit

    # Section A's chunks come first (lower ordinals) and belong to unit 0;
    # section B's chunks come after and belong to unit 1.
    a_chunks = [c for c in chunks if c.evidence_unit_index == 0]
    b_chunks = [c for c in chunks if c.evidence_unit_index == 1]
    assert len(a_chunks) >= 2  # 250 words, chunk_size=200 => at least 2 windows
    assert len(b_chunks) >= 2
    assert max(c.ordinal for c in a_chunks) < min(c.ordinal for c in b_chunks)


def test_chunk_overlap_matches_recipe_overlap_exactly() -> None:
    # A single unit long enough to require multiple sub-chunks.
    text = "# Big Section\n" + _words(500)
    recipe = _recipe(chunk_size=200, overlap=40)
    units, chunks = chunk_document(text, recipe)

    assert len(units) == 1
    unit_chunks = [c for c in chunks if c.evidence_unit_index == 0]
    assert len(unit_chunks) >= 3

    for prev, curr in zip(unit_chunks, unit_chunks[1:]):
        prev_words = prev.text.split()
        curr_words = curr.text.split()
        # The actual shared words: the tail of prev == the head of curr,
        # `overlap` words long.
        shared_tail = prev_words[-recipe.overlap :]
        shared_head = curr_words[: recipe.overlap]
        assert shared_tail == shared_head
        assert len(shared_tail) == recipe.overlap


def test_chunk_stride_is_chunk_size_minus_overlap() -> None:
    # No heading, so the unit's text is exactly the word sequence below --
    # word indices in the chunk text map 1:1 onto indices in `all_words`.
    text = _words(500)
    recipe = _recipe(chunk_size=200, overlap=40)
    units, chunks = chunk_document(text, recipe)

    assert units[0].heading is None
    assert units[0].text == text

    # word index that each chunk starts at should advance by
    # stride = chunk_size - overlap = 160 each time.
    stride = recipe.chunk_size - recipe.overlap
    all_words = units[0].text.split()
    starts = [all_words.index(c.text.split()[0]) for c in chunks]
    for prev_start, curr_start in zip(starts, starts[1:]):
        assert curr_start - prev_start == stride


def test_chunk_short_unit_produces_exactly_one_chunk_no_overlap() -> None:
    text = "# Small Section\n" + _words(50)
    recipe = _recipe(chunk_size=200, overlap=40)
    units, chunks = chunk_document(text, recipe)

    assert len(units) == 1
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    # Chunk text is the whole unit's words rejoined with single spaces
    # (word-split/rejoin normalizes whitespace, e.g. the heading's newline).
    assert chunks[0].text.split() == units[0].text.split()


def test_chunk_content_hash_matches_sha256_of_chunk_text() -> None:
    text = "# Section\n" + _words(300)
    recipe = _recipe(chunk_size=200, overlap=40)
    _, chunks = chunk_document(text, recipe)
    for c in chunks:
        assert c.content_hash == _sha256(c.text)


def test_chunk_heading_is_inherited_from_owning_unit() -> None:
    text = "## My Heading\n" + _words(300)
    recipe = _recipe(chunk_size=200, overlap=40)
    _, chunks = chunk_document(text, recipe)
    assert all(c.heading == "My Heading" for c in chunks)


def test_sliding_window_rejects_overlap_gte_chunk_size() -> None:
    text = "# Section\n" + _words(300)
    recipe = _recipe(chunk_size=100, overlap=100)
    with pytest.raises(ValueError):
        chunk_document(text, recipe)
