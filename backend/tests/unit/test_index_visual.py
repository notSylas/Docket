"""Unit tests for `LancePageIndexWriter` (LanceDB-backed page-description
index, visual retrieval checkpoint 2). Mirrors `test_index_vector.py`'s
structure, adapted for the composite `(evidence_version_id, page_no)` key."""

from __future__ import annotations

from pathlib import Path

from docket.index.visual_index import LancePageIndexWriter, PageRecord


def _record(
    evidence_version_id: str, page_no: int, description: str, source_id: str = "src_1"
) -> PageRecord:
    return PageRecord(
        evidence_version_id=evidence_version_id,
        source_id=source_id,
        page_no=page_no,
        description=description,
    )


def _vec(seed: float) -> list[float]:
    return [seed, seed + 0.1, seed + 0.2]


def test_upsert_creates_table_and_is_queryable(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb", table_name="pages")
    writer.upsert([_record("ev_1", 1, "a page about alpha")], embeddings=[_vec(0.1)])

    table = writer.table
    assert table is not None
    rows = table.search().to_list()
    assert len(rows) == 1
    assert rows[0]["evidence_version_id"] == "ev_1"
    assert rows[0]["page_no"] == 1
    assert rows[0]["description"] == "a page about alpha"


def test_upsert_same_key_twice_no_duplicates_updates_in_place(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert([_record("ev_1", 1, "first description")], embeddings=[_vec(0.1)])
    writer.upsert([_record("ev_1", 1, "updated description")], embeddings=[_vec(0.9)])

    table = writer._open_table()
    rows = table.search().to_list()
    assert len(rows) == 1
    assert rows[0]["description"] == "updated description"


def test_upsert_different_page_no_same_version_are_distinct_rows(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [_record("ev_1", 1, "page one"), _record("ev_1", 2, "page two")],
        embeddings=[_vec(0.1), _vec(0.2)],
    )
    table = writer._open_table()
    rows = {row["page_no"]: row["description"] for row in table.search().to_list()}
    assert rows == {1: "page one", 2: "page two"}


def test_upsert_requires_matching_embeddings_length(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    import pytest

    with pytest.raises(ValueError):
        writer.upsert(
            [_record("ev_1", 1, "a"), _record("ev_1", 2, "b")], embeddings=[_vec(0.1)]
        )


def test_upsert_empty_records_is_a_noop(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert([], embeddings=[])
    assert writer.table is None


def test_delete_removes_rows_by_composite_key(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [_record("ev_1", 1, "a"), _record("ev_1", 2, "b"), _record("ev_2", 1, "c")],
        embeddings=[_vec(0.1), _vec(0.2), _vec(0.3)],
    )
    writer.delete([("ev_1", 1)])

    rows = {(r["evidence_version_id"], r["page_no"]) for r in writer.table.search().to_list()}
    assert rows == {("ev_1", 2), ("ev_2", 1)}


def test_delete_only_removes_exact_key_match(tmp_path: Path) -> None:
    """(ev_1, 1) and (ev_2, 1) share a page_no but not an evidence_version_id
    -- deleting one must not touch the other."""
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [_record("ev_1", 1, "a"), _record("ev_2", 1, "b")],
        embeddings=[_vec(0.1), _vec(0.2)],
    )
    writer.delete([("ev_1", 1)])

    rows = {(r["evidence_version_id"], r["page_no"]) for r in writer.table.search().to_list()}
    assert rows == {("ev_2", 1)}


def test_delete_on_nonexistent_table_is_a_noop(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.delete([("ev_1", 1)])  # table never created -- must not raise


def test_delete_empty_keys_is_a_noop(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert([_record("ev_1", 1, "a")], embeddings=[_vec(0.1)])
    writer.delete([])
    assert len(writer.table.search().to_list()) == 1


def test_delete_by_source_only_removes_that_source(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [
            _record("ev_1", 1, "a", source_id="src_1"),
            _record("ev_2", 1, "b", source_id="src_2"),
        ],
        embeddings=[_vec(0.1), _vec(0.2)],
    )
    writer.delete_by_source("src_1")

    rows = {(r["evidence_version_id"], r["page_no"]) for r in writer.table.search().to_list()}
    assert rows == {("ev_2", 1)}


def test_delete_by_source_on_nonexistent_table_is_a_noop(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.delete_by_source("src_1")  # must not raise


def test_ids_with_sql_special_characters_do_not_break_predicates(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    tricky_id = "ev_o'brien"
    writer.upsert(
        [_record(tricky_id, 1, "a", source_id="src_o'brien")], embeddings=[_vec(0.1)]
    )

    rows = writer.table.search().to_list()
    assert rows[0]["evidence_version_id"] == tricky_id

    writer.delete([(tricky_id, 1)])
    assert writer.table.search().to_list() == []


def test_vector_search_nearest_match(tmp_path: Path) -> None:
    writer = LancePageIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [_record("ev_1", 1, "alpha"), _record("ev_1", 2, "beta"), _record("ev_1", 3, "gamma")],
        embeddings=[_vec(0.0), _vec(5.0), _vec(10.0)],
    )
    results = writer.table.search(_vec(5.0)).limit(1).to_list()
    assert results[0]["page_no"] == 2
