"""Token-capped Docling chunking: window cap, pipe tables, heading paths, recipe."""

from __future__ import annotations

import json

from docket.infra.parsing.chunker import chunk_document, split_into_units
from docket.infra.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.core.db.identity import compute_recipe_id


class WordCounter:
    def count(self, text: str) -> int:
        return len(text.split())

    name = "test:words"


COUNTER = WordCounter()


def _recipe(size: int = 200, overlap: int = 40) -> ChunkRecipe:
    return ChunkRecipe(
        chunk_size=size,
        overlap=overlap,
        splitter="heading_then_sliding_window",
        parser_name="docling",
        parser_version="t",
    )


def _words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def _table(n_rows: int, cols: int = 2) -> str:
    head = "| " + " | ".join(f"H{c}" for c in range(cols)) + " |"
    sep = "|" + "---|" * cols
    rows = [
        "| " + " | ".join(f"r{r}c{c}" for c in range(cols)) + " |" for r in range(n_rows)
    ]
    return "\n".join([head, sep, *rows])


# -- window cap ---------------------------------------------------------------


def test_window_over_cap_is_split_by_words() -> None:
    _, chunks = chunk_document(_words(200), _recipe(), COUNTER, max_tokens=60)
    assert len(chunks) > 1
    assert all(COUNTER.count(c.text) <= 60 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert " ".join(c.text for c in chunks).split() == _words(200).split()


def test_window_under_cap_is_unchanged() -> None:
    capped = chunk_document(_words(500), _recipe(), COUNTER, max_tokens=512)[1]
    loose = chunk_document(_words(500), _recipe(), COUNTER, max_tokens=10_000)[1]
    assert [c.text for c in capped] == [c.text for c in loose]
    assert [c.content_hash for c in capped] == [c.content_hash for c in loose]


def test_page_spans_survive_cap_splitting() -> None:
    text = f"<!--PAGE:1-->{_words(35, 'a')} <!--PAGE:2-->{_words(25, 'b')}"
    _, chunks = chunk_document(text, _recipe(), COUNTER, max_tokens=40)
    assert len(chunks) == 2
    assert chunks[0].page_start == chunks[0].page_end == 1
    assert (chunks[-1].page_start, chunks[-1].page_end) == (1, 2)  # straddles the marker
    assert all("PAGE" not in c.text for c in chunks)


def test_single_giant_word_is_cut_by_characters() -> None:
    _, chunks = chunk_document("x" * 500, _recipe(), HeuristicCounter(), max_tokens=20)
    assert all(HeuristicCounter().count(c.text) <= 20 for c in chunks)
    assert "".join(c.text for c in chunks) == "x" * 500


class HeuristicCounter:
    name = "h"

    def count(self, text: str) -> int:
        return -(-len(text) // 3)


# -- tables --------------------------------------------------------------------


def test_table_split_on_rows_with_header_repeated_and_newlines_kept() -> None:
    text = "# Data\nSome intro.\n\n" + _table(30)
    _, chunks = chunk_document(text, _recipe(), COUNTER, max_tokens=40)
    table_chunks = [c for c in chunks if c.text.startswith("| H0")]
    assert len(table_chunks) > 1
    for chunk in table_chunks:
        lines = chunk.text.split("\n")
        assert lines[0] == "| H0 | H1 |" and lines[1] == "|---|---|"
        assert all(line.startswith("|") and line.endswith("|") for line in lines)  # no mid-row cut
        assert COUNTER.count(chunk.text) <= 40
    rows = [line for c in table_chunks for line in c.text.split("\n")[2:]]
    assert rows == [f"| r{r}c0 | r{r}c1 |" for r in range(30)]
    assert chunks[0].text.startswith("# Data")  # prose before the table is its own chunk


def test_small_table_is_one_chunk_with_newlines() -> None:
    _, chunks = chunk_document(_table(3), _recipe(), COUNTER, max_tokens=512)
    assert len(chunks) == 1
    assert chunks[0].text == _table(3)


def test_oversize_row_is_split_by_cells_keeping_header_slice() -> None:
    wide = "| " + " | ".join(f"cell{c} " + _words(10, f"v{c}_") for c in range(4)) + " |"
    text = "| A | B | C | D |\n|---|---|---|---|\n" + wide
    _, chunks = chunk_document(text, _recipe(), COUNTER, max_tokens=30)
    assert len(chunks) > 1
    assert all(COUNTER.count(c.text) <= 30 for c in chunks)
    first = chunks[0].text.split("\n")
    assert first[0].startswith("| A") and first[1].startswith("| ---")
    assert all(c.text.split("\n")[2].startswith("| cell") or "v" in c.text for c in chunks)


def test_oversize_single_cell_is_cut_by_words() -> None:
    text = "| A |\n|---|\n| " + _words(100) + " |"
    _, chunks = chunk_document(text, _recipe(), COUNTER, max_tokens=20)
    assert all(COUNTER.count(c.text) <= 20 for c in chunks)
    body = " ".join(line.strip("| ") for c in chunks for line in c.text.split("\n")[2:])
    assert body.split() == _words(100).split()


def test_table_pages_with_markers_before_rows() -> None:
    table = _table(12).split("\n")
    table[8] = "<!--PAGE:2-->" + table[8]
    text = "<!--PAGE:1-->Intro words here.\n\n" + "\n".join(table)
    _, chunks = chunk_document(text, _recipe(), COUNTER, max_tokens=22)
    table_chunks = [c for c in chunks if c.text.startswith("| H0")]
    assert table_chunks[0].page_start == 1
    assert table_chunks[-1].page_end == 2
    assert all("PAGE" not in c.text for c in chunks)


def test_marker_only_line_inside_table_does_not_end_it() -> None:
    table = _table(6).split("\n")
    table.insert(4, "<!--PAGE:2-->")
    _, chunks = chunk_document("\n".join(table), _recipe(), COUNTER, max_tokens=512)
    assert len(chunks) == 1 and chunks[0].text == _table(6)
    assert chunks[0].page_end == 2


def test_non_table_text_keeps_flattening() -> None:
    _, chunks = chunk_document("# T\nline one\nline two", _recipe(), COUNTER)
    assert chunks[0].text == "# T line one line two"


# -- heading path ----------------------------------------------------------------


def test_heading_path_in_unit_locator() -> None:
    md = "intro\n# A\ntext\n## B\ntext\n### C\ntext\n## D\ntext\n# E\ntext"
    units, _ = chunk_document(md, _recipe(), COUNTER)
    paths = [json.loads(u.locator_json)["heading_path"] if u.locator_json else None for u in units]
    assert paths == [None, ["A"], ["A", "B"], ["A", "B", "C"], ["A", "D"], ["E"]]
    assert [u.heading for u in units] == [None, "A", "B", "C", "D", "E"]
    assert all(u.unit_kind == "section" for u in units)
    assert [u.locator_json for u in split_into_units(md)] == [u.locator_json for u in units]


# -- recipe ------------------------------------------------------------------------


def test_recipe_id_changes_with_splitter_and_cap() -> None:
    def rid(splitter: str) -> str:
        return compute_recipe_id(
            chunk_size=200, overlap=40, splitter=splitter, parser_name="p", parser_version="1"
        )

    assert rid(DEFAULT_SPLITTER) != rid("heading_then_sliding_window")
    assert rid(f"{DEFAULT_SPLITTER}:max_tokens=512") != rid(f"{DEFAULT_SPLITTER}:max_tokens=256")


# -- merge-fit, padding normalization ---------------------------------------


def test_mixed_section_under_cap_is_one_chunk_with_newlines() -> None:
    md = "# Title\n\nSome intro prose here.\n\n" + _table(3)
    _, chunks = chunk_document(md, _recipe(), COUNTER, max_tokens=200)
    assert len(chunks) == 1
    lines = chunks[0].text.split("\n")
    assert lines[0] == "# Title"
    assert lines[1] == "Some intro prose here."
    assert lines[2:] == _table(3).split("\n")


def test_mixed_section_over_cap_still_splits_on_rows() -> None:
    md = "# Title\n\nIntro prose.\n\n" + _table(40)
    _, chunks = chunk_document(md, _recipe(), COUNTER, max_tokens=40)
    assert len(chunks) > 2
    assert all(COUNTER.count(c.text) <= 40 for c in chunks)
    parts = [c.text for c in chunks if c.text.startswith("| H0")]
    assert len(parts) > 1
    assert all(p.split("\n")[:2] == _table(1).split("\n")[:2] for p in parts)


def test_merged_section_page_span() -> None:
    md = "<!--PAGE:1-->\n# Title\n\nIntro.\n\n<!--PAGE:2-->\n" + _table(2)
    _, chunks = chunk_document(md, _recipe(), COUNTER, max_tokens=200)
    assert len(chunks) == 1
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 2)
    assert "PAGE" not in chunks[0].text


def test_table_padding_normalized_without_changing_cells() -> None:
    md = (
        "| **Project status**        | Architecture defined; a \\| b   |\n"
        "|---------------------------|:-----------------:|\n"
        "| x                         |    y z       |"
    )
    _, chunks = chunk_document(md, _recipe(), COUNTER, max_tokens=200)
    assert chunks[0].text.split("\n") == [
        "| **Project status** | Architecture defined; a \\| b |",
        "|---|:---:|",
        "| x | y z |",
    ]


def test_recipe_splitter_id_bumped() -> None:
    assert DEFAULT_SPLITTER == "heading_then_sliding_window_tokcap2"
