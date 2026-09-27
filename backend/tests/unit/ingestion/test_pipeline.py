"""Orchestration-level tests for `docket.ingestion.pipeline.IngestionPipeline`
-- the end-to-end incremental ingestion wiring (real Docling parsing, a fake
inference gateway, and a real migrated SQLite DB + real LanceDB dir; see
`conftest.py` for the shared `env`/`parser` fixtures).

This is the regression suite proving the whole pipeline -- not just
`IndexManager` in isolation (see `test_index_manager.py`) -- correctly skips
re-embedding unchanged files end to end, correctly reconciles stale chunks
when a file's content changes, and correctly isolates one bad file's failure
from the rest of the batch. Formula-transcription and visual-index
behavior have their own files (`test_formulas.py`, `test_visual.py`).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import _chunk_ids_for_file, _job_status, _write_docx
from docket.db.engine import get_session_factory
from docket.db.models import Chunk, EvidenceVersion, IngestionJob, IngestionJobStatus
from docket.ingestion.chunk_writer import ChunkWriter
from docket.parsing.chunker import ChunkDraft, EvidenceUnitDraft
from docket.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.sources.manager import SourceManager

# ---------------------------------------------------------------------------
# First run: everything is new.
# ---------------------------------------------------------------------------


def test_first_run_ingests_all_files_and_indexes(env: SimpleNamespace) -> None:
    doc1 = env.folder / "doc1.docx"
    doc2 = env.folder / "doc2.docx"
    _write_docx(doc1, "Section One", "Alpha content about penguins and glaciers.")
    _write_docx(doc2, "Section Two", "Beta content about volcanoes and coffee.")

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.status == "succeeded"
    assert result.files_processed == 2
    assert result.files_failed == 0
    assert {r.status for r in result.file_results} == {"ingested"}
    assert all(r.chunks_written > 0 for r in result.file_results)

    assert _job_status(env, result.job_id) == IngestionJobStatus.SUCCEEDED

    with env.session_factory() as session:
        chunk_count = len(
            session.execute(
                select(Chunk.id).where(Chunk.source_id == env.source.id)
            ).scalars().all()
        )
    assert chunk_count == sum(r.chunks_written for r in result.file_results)
    assert chunk_count > 0

    # Chunks actually landed in both indexes (not just SQLite).
    assert len(env.gateway.embed_calls) > 0
    for file_path in (doc1, doc2):
        chunk_ids = _chunk_ids_for_file(env, file_path)
        assert chunk_ids
        assert env.vector.existing_chunk_ids(chunk_ids) == chunk_ids
        assert env.fts.existing_chunk_ids(chunk_ids) == chunk_ids


# ---------------------------------------------------------------------------
# Second run, unchanged: the critical no-re-embed regression assertion.
# ---------------------------------------------------------------------------


def test_second_run_unchanged_files_are_skipped_and_not_reembedded(
    env: SimpleNamespace,
) -> None:
    doc1 = env.folder / "doc1.docx"
    doc2 = env.folder / "doc2.docx"
    _write_docx(doc1, "Section One", "Alpha content about penguins and glaciers.")
    _write_docx(doc2, "Section Two", "Beta content about volcanoes and coffee.")

    first = env.pipeline.run_ingestion_for_source(env.source.id)
    assert {r.status for r in first.file_results} == {"ingested"}
    embed_calls_after_first_run = len(env.gateway.embed_calls)
    assert embed_calls_after_first_run > 0

    second = env.pipeline.run_ingestion_for_source(env.source.id)

    assert second.status == "succeeded"
    assert second.files_processed == 2
    assert second.files_failed == 0
    assert {r.status for r in second.file_results} == {"unchanged"}
    assert all(r.chunks_written == 0 for r in second.file_results)

    # The whole-pipeline regression assertion: re-running over unchanged
    # files must not trigger a single additional embed call anywhere in the
    # stack (evidence layer -> parser -> chunker -> index manager).
    assert len(env.gateway.embed_calls) == embed_calls_after_first_run


# ---------------------------------------------------------------------------
# Content change: re-ingest + reconcile stale chunks.
# ---------------------------------------------------------------------------


def test_modified_file_is_reingested_and_stale_chunks_are_reconciled(
    env: SimpleNamespace,
) -> None:
    doc1 = env.folder / "doc1.docx"
    doc2 = env.folder / "doc2.docx"
    _write_docx(doc1, "Section One", "Alpha content about penguins and glaciers.")
    _write_docx(doc2, "Section Two", "Beta content about volcanoes and coffee.")

    env.pipeline.run_ingestion_for_source(env.source.id)
    old_doc1_chunk_ids = _chunk_ids_for_file(env, doc1)
    old_doc2_chunk_ids = _chunk_ids_for_file(env, doc2)
    assert old_doc1_chunk_ids and old_doc2_chunk_ids

    _write_docx(doc1, "Section One Revised", "Completely different words: orchestras and tides.")
    result = env.pipeline.run_ingestion_for_source(env.source.id)

    results_by_path = {r.path: r for r in result.file_results}
    assert results_by_path[doc1].status == "ingested"
    assert results_by_path[doc2].status == "unchanged"

    new_doc1_chunk_ids = _chunk_ids_for_file(env, doc1)
    assert new_doc1_chunk_ids
    assert new_doc1_chunk_ids.isdisjoint(old_doc1_chunk_ids)

    # Old doc1 chunks are gone from both indexes...
    assert env.vector.existing_chunk_ids(old_doc1_chunk_ids) == set()
    assert env.fts.existing_chunk_ids(old_doc1_chunk_ids) == set()
    # ...new doc1 chunks are indexed...
    assert env.vector.existing_chunk_ids(new_doc1_chunk_ids) == new_doc1_chunk_ids
    assert env.fts.existing_chunk_ids(new_doc1_chunk_ids) == new_doc1_chunk_ids
    # ...and doc2's untouched chunks were NOT swept up in the reconcile.
    assert env.vector.existing_chunk_ids(old_doc2_chunk_ids) == old_doc2_chunk_ids
    assert env.fts.existing_chunk_ids(old_doc2_chunk_ids) == old_doc2_chunk_ids


# ---------------------------------------------------------------------------
# One bad file must not abort the batch.
# ---------------------------------------------------------------------------


def test_one_corrupt_file_fails_without_aborting_the_batch(env: SimpleNamespace) -> None:
    doc1 = env.folder / "doc1.docx"
    doc2 = env.folder / "doc2.docx"
    corrupt = env.folder / "corrupt.docx"
    _write_docx(doc1, "Section One", "Alpha content about penguins and glaciers.")
    _write_docx(doc2, "Section Two", "Beta content about volcanoes and coffee.")
    corrupt.write_text("this is plain text, not a real .docx file")

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.files_processed == 3
    assert result.files_failed == 1
    assert result.status == "partial"
    assert _job_status(env, result.job_id) == IngestionJobStatus.PARTIAL

    results_by_path = {r.path: r for r in result.file_results}
    assert results_by_path[doc1].status == "ingested"
    assert results_by_path[doc2].status == "ingested"
    assert results_by_path[corrupt].status == "failed"
    assert results_by_path[corrupt].error is not None


# ---------------------------------------------------------------------------
# Progress callback + interruption.
# ---------------------------------------------------------------------------


def test_progress_events_order_and_counts(env: SimpleNamespace) -> None:
    _write_docx(env.folder / "a.docx", "A", "Alpha content about penguins.")
    _write_docx(env.folder / "b.docx", "B", "Beta content about volcanoes.")
    events = []

    result = env.pipeline.run_ingestion_for_source(env.source.id, progress=events.append)

    assert [(e.kind, e.index, e.total) for e in events] == [
        ("start", 1, 2),
        ("done", 1, 2),
        ("start", 2, 2),
        ("done", 2, 2),
    ]
    assert events[0].result is None
    assert events[1].result.status == "ingested"
    assert [e.path.name for e in events[::2]] == ["a.docx", "b.docx"]
    assert result.files_processed == 2


def test_progress_callback_exception_is_swallowed(env: SimpleNamespace) -> None:
    _write_docx(env.folder / "a.docx", "A", "Alpha content about penguins.")

    def boom(event):
        raise RuntimeError("callback bug")

    result = env.pipeline.run_ingestion_for_source(env.source.id, progress=boom)
    assert result.status == "succeeded"
    assert result.files_processed == 1


def test_interrupted_run_finalizes_job_as_failed_and_reraises(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_docx(env.folder / "a.docx", "A", "Alpha content about penguins.")

    def interrupt(source_id, path):
        raise KeyboardInterrupt

    monkeypatch.setattr(env.pipeline, "_ingest_one_file", interrupt)
    with pytest.raises(KeyboardInterrupt):
        env.pipeline.run_ingestion_for_source(env.source.id)

    with env.session_factory() as session:
        job = session.execute(select(IngestionJob)).scalars().one()
        assert job.status == IngestionJobStatus.FAILED
        assert job.error == "interrupted"
        assert job.finished_at is not None


# ---------------------------------------------------------------------------
# Standalone `ChunkWriter` test -- constructed directly, no full
# `IngestionPipeline`, no real Docling parser, no `EvidenceManager`: proves
# this collaborator is independently testable, not just file-size relief
# from the split.
# ---------------------------------------------------------------------------


def test_chunk_writer_persists_units_and_chunks_and_tracks_current_version(
    migrated_sqlite_engine, tmp_path
) -> None:
    """`ChunkWriter`, exercised entirely on its own (hand-built
    `EvidenceUnitDraft`/`ChunkDraft` objects, a real migrated SQLite engine,
    no `IngestionPipeline`/`EvidenceManager`/parser anywhere in the test):
    `ensure_recipe_row` creates the `ChunkRecipe` row exactly once,
    `persist_units_and_chunks` writes real `EvidenceUnit`/`Chunk` rows and
    returns matching `ChunkRecord`s, and `current_version_id`/
    `current_chunk_ids_for_source` read back exactly what was written."""
    session_factory = get_session_factory(migrated_sqlite_engine)
    chunk_recipe = ChunkRecipe(
        chunk_size=200,
        overlap=40,
        splitter=DEFAULT_SPLITTER,
        parser_name="fixture",
        parser_version="1",
    )
    writer = ChunkWriter(session_factory=session_factory, chunk_recipe=chunk_recipe)

    # ensure_recipe_row is idempotent -- calling it twice must not raise a
    # duplicate-primary-key error.
    writer.ensure_recipe_row()
    writer.ensure_recipe_row()

    source_manager = SourceManager(session_factory)
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)
    file_path = str(folder / "doc.docx")

    assert writer.current_version_id(source.id, file_path) is None

    with session_factory() as session:
        version = EvidenceVersion(
            source_id=source.id,
            file_path=file_path,
            content_hash="deadbeef",
            byte_size=0,
            is_current=True,
            parser_name="fixture",
            parser_version="1",
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        version_id = version.id

    assert writer.current_version_id(source.id, file_path) == version_id
    assert writer.current_chunk_ids_for_source(source.id) == set()

    units = [
        EvidenceUnitDraft(unit_index=0, heading="Intro", text="Intro text", content_hash="h0"),
    ]
    chunks = [
        ChunkDraft(
            evidence_unit_index=0, ordinal=0, heading="Intro", text="Intro text", content_hash="c0"
        ),
    ]

    records = writer.persist_units_and_chunks(
        source_id=source.id, evidence_version_id=version_id, units=units, chunks=chunks
    )

    assert len(records) == 1
    assert records[0].source_id == source.id
    assert records[0].evidence_version_id == version_id
    assert records[0].text == "Intro text"

    assert writer.current_chunk_ids_for_source(source.id) == {records[0].chunk_id}
