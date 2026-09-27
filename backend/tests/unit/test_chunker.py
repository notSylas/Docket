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


# ---------------------------------------------------------------------------
# Page markers (<!--PAGE:N-->) -- provenance for visual retrieval checkpoint 2/3.
# ---------------------------------------------------------------------------


def test_no_markers_anywhere_yields_none_page_span_on_every_unit_and_chunk() -> None:
    text = (
        "# Section A\n" + _words(50, "a") + "\n\n"
        "# Section B\n" + _words(50, "b") + "\n"
    )
    recipe = _recipe(chunk_size=200, overlap=40)
    units, chunks = chunk_document(text, recipe)

    assert len(units) == 2
    for u in units:
        assert u.page_start is None
        assert u.page_end is None
    for c in chunks:
        assert c.page_start is None
        assert c.page_end is None


def test_markers_produce_correct_per_unit_page_span_and_no_leakage() -> None:
    # Each marker sits at the tail of the *previous* section (right before
    # the next heading), mirroring how docling_wrapper inserts a marker
    # right before the first matched text item on a new page -- so each
    # unit here lands entirely on one page.
    text = (
        "<!--PAGE:1-->\n"
        "# Section A\n"
        "Alpha bravo charlie.\n\n"
        "<!--PAGE:2-->\n"
        "# Section B\n"
        "Delta echo foxtrot.\n\n"
        "<!--PAGE:3-->\n"
        "# Section C\n"
        "Golf hotel india.\n"
    )
    units = split_into_units(text)

    assert [u.heading for u in units] == ["Section A", "Section B", "Section C"]
    assert [(u.page_start, u.page_end) for u in units] == [(1, 1), (2, 2), (3, 3)]
    for u in units:
        assert "<!--PAGE:" not in u.text
        assert u.content_hash == _sha256(u.text)


def test_split_empty_document_produces_no_units_even_with_only_markers() -> None:
    # A document consisting only of page markers (no real content) must not
    # produce a spurious "empty" unit.
    assert split_into_units("<!--PAGE:1-->\n<!--PAGE:2-->\n") == []


def test_chunk_spanning_a_page_marker_mid_window_gets_differing_start_and_end() -> None:
    # 30 words, chunk_size=10, overlap=2 -> stride=8, windows at
    # [0,10), [8,18), [16,26), [24,30). The page-2 marker sits between word
    # index 14 and 15, so only the second window (covering indices 8..17)
    # straddles it.
    page1_words = " ".join(f"w{i}" for i in range(15))
    page2_words = " ".join(f"w{i}" for i in range(15, 30))
    text = f"<!--PAGE:1--> {page1_words} <!--PAGE:2--> {page2_words}"
    recipe = _recipe(chunk_size=10, overlap=2)

    units, chunks = chunk_document(text, recipe)

    assert len(units) == 1
    assert units[0].page_start == 1
    assert units[0].page_end == 2
    assert "<!--PAGE:" not in units[0].text

    assert len(chunks) == 4
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)
    assert (chunks[1].page_start, chunks[1].page_end) == (1, 2)
    assert chunks[1].page_start != chunks[1].page_end
    assert (chunks[2].page_start, chunks[2].page_end) == (2, 2)
    assert (chunks[3].page_start, chunks[3].page_end) == (2, 2)

    for c in chunks:
        assert "<!--PAGE:" not in c.text


def test_content_hash_is_identical_with_or_without_page_markers() -> None:
    # The regression this checkpoint must not introduce: two documents with
    # identical *real* content -- one with page markers inserted, one
    # without -- must chunk to byte-identical text and therefore identical
    # content_hash (and therefore identical chunk_id, since compute_chunk_id
    # is keyed off content_hash). Otherwise every existing chunk's id would
    # silently change the moment docling_wrapper starts annotating pages.
    plain = "# Section\n" + " ".join(f"w{i}" for i in range(30))
    annotated = (
        "# Section\n"
        + " ".join(f"w{i}" for i in range(15))
        + " <!--PAGE:2--> "
        + " ".join(f"w{i}" for i in range(15, 30))
    )
    recipe = _recipe(chunk_size=10, overlap=2)

    plain_units, plain_chunks = chunk_document(plain, recipe)
    annotated_units, annotated_chunks = chunk_document(annotated, recipe)

    assert len(plain_chunks) == len(annotated_chunks) > 0
    for p, a in zip(plain_chunks, annotated_chunks):
        assert p.text == a.text
        assert p.content_hash == a.content_hash

    # The annotated run does carry real page info (proving the marker was
    # not simply ignored)...
    assert any(c.page_start is not None for c in annotated_chunks)
    # ...while the plain run has none, as expected.
    assert all(c.page_start is None and c.page_end is None for c in plain_chunks)
