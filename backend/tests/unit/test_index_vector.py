"""Unit tests for `LanceIndexWriter` (LanceDB-backed vector index)."""

from __future__ import annotations

from pathlib import Path

from attest.index.base import ChunkRecord
from attest.index.vector_index import LanceIndexWriter


def _record(chunk_id: str, text: str, source_id: str = "src_1") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id=source_id,
        evidence_version_id="ev_1",
        evidence_unit_id="eu_1",
        chunk_recipe_id="rcp_1",
        ordinal=0,
        heading=None,
        text=text,
        content_hash="hash_" + chunk_id,
    )


def _vec(seed: float) -> list[float]:
    return [seed, seed + 0.1, seed + 0.2]


def test_upsert_creates_table_and_is_queryable(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb", table_name="chunks")
    writer.upsert([_record("chk_a", "alpha text")], embeddings=[_vec(0.1)])

    assert writer.existing_chunk_ids(["chk_a"]) == {"chk_a"}


def test_upsert_same_id_twice_no_duplicates(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert([_record("chk_a", "alpha text")], embeddings=[_vec(0.1)])
    writer.upsert([_record("chk_a", "alpha text updated")], embeddings=[_vec(0.9)])

    table = writer._open_table()
    rows = table.search().to_list()
    assert len(rows) == 1
    assert rows[0]["text"] == "alpha text updated"


def test_upsert_requires_matching_embeddings_length(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    import pytest

    with pytest.raises(ValueError):
        writer.upsert([_record("chk_a", "alpha"), _record("chk_b", "beta")], embeddings=[_vec(0.1)])


def test_delete_removes_rows(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [_record("chk_a", "alpha"), _record("chk_b", "beta")],
        embeddings=[_vec(0.1), _vec(0.2)],
    )
    writer.delete(["chk_a"])
    assert writer.existing_chunk_ids(["chk_a", "chk_b"]) == {"chk_b"}


def test_delete_on_nonexistent_table_is_a_noop(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.delete(["chk_a"])  # table never created -- must not raise


def test_delete_by_source_only_removes_that_source(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [_record("chk_a", "alpha", source_id="src_1"), _record("chk_b", "beta", source_id="src_2")],
        embeddings=[_vec(0.1), _vec(0.2)],
    )
    writer.delete_by_source("src_1")

    assert writer.existing_chunk_ids(["chk_a", "chk_b"]) == {"chk_b"}


def test_chunk_ids_for_source(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [
            _record("chk_a", "alpha", source_id="src_1"),
            _record("chk_b", "beta", source_id="src_1"),
            _record("chk_c", "gamma", source_id="src_2"),
        ],
        embeddings=[_vec(0.1), _vec(0.2), _vec(0.3)],
    )
    assert writer.chunk_ids_for_source("src_1") == {"chk_a", "chk_b"}
    assert writer.chunk_ids_for_source("src_2") == {"chk_c"}


def test_chunk_ids_for_source_on_nonexistent_table_returns_empty(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    assert writer.chunk_ids_for_source("src_1") == set()


def test_existing_chunk_ids_empty_input(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    assert writer.existing_chunk_ids([]) == set()


def test_existing_chunk_ids_on_nonexistent_table_returns_empty(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    assert writer.existing_chunk_ids(["chk_a"]) == set()


def test_ids_with_sql_special_characters_do_not_break_predicates(tmp_path: Path) -> None:
    """chunk_id/source_id containing a single quote must not corrupt the
    LanceDB delete/search predicate string (proper escaping, not naive
    f-string interpolation)."""
    writer = LanceIndexWriter(tmp_path / "lancedb")
    tricky_id = "chk_o'brien"
    writer.upsert(
        [_record(tricky_id, "alpha", source_id="src_o'brien")], embeddings=[_vec(0.1)]
    )

    assert writer.existing_chunk_ids([tricky_id]) == {tricky_id}
    assert writer.chunk_ids_for_source("src_o'brien") == {tricky_id}

    writer.delete([tricky_id])
    assert writer.existing_chunk_ids([tricky_id]) == set()


def test_vector_search_nearest_match(tmp_path: Path) -> None:
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(
        [_record("chk_a", "alpha"), _record("chk_b", "beta"), _record("chk_c", "gamma")],
        embeddings=[_vec(0.0), _vec(5.0), _vec(10.0)],
    )
    table = writer._open_table()
    results = table.search(_vec(5.0)).limit(1).to_list()
    assert results[0]["chunk_id"] == "chk_b"
