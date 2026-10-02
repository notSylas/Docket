"""End-to-end `.pptx` ingestion through `IngestionPipeline` -- the native
python-pptx counterpart to `test_xlsx_ingestion.py`'s coverage. Proves the
format dispatch added for the presentation adapter (`pipeline.py`'s
`_ingest_one_pptx_file`) goes through the same `EvidenceManager`/
`ChunkWriter`/`IndexManager` seam as PDF/DOCX/XLSX, not a parallel path.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from conftest import _chunk_ids_for_file, _job_status, _write_docx, _write_pptx
from docket.core.db.models import Chunk, EvidenceUnit, EvidenceVersion, VersionStatus


def test_pptx_file_ingests_text_table_chart_and_notes_as_distinct_units(
    env: SimpleNamespace,
) -> None:
    path = env.folder / "deck.pptx"
    _write_pptx(
        path,
        slide_text="Quarterly review",
        table_headers=["Region", "Revenue"],
        table_rows=[["East", "100"], ["West", "200"]],
        chart_categories=["East", "West"],
        chart_series={"Revenue": [100.0, 200.0]},
        notes_text="Remember to mention the West region slowdown",
    )

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.status == "succeeded"
    assert result.files_processed == 1
    assert result.files_failed == 0
    [file_result] = result.file_results
    assert file_result.status == "ingested"
    # 1 slide_text + 2 table_row + 1 chart_data + 1 notes = 5 units/chunks.
    assert file_result.chunks_written == 5

    with env.session_factory() as session:
        version = session.execute(
            select(EvidenceVersion).where(EvidenceVersion.file_path == str(path))
        ).scalar_one()
        assert version.status == VersionStatus.READY
        assert version.parser_name == "python-pptx"

        units = session.execute(
            select(EvidenceUnit).where(EvidenceUnit.evidence_version_id == version.id)
        ).scalars().all()
        kinds = sorted(u.unit_kind for u in units)
        assert kinds == ["chart_data", "notes", "slide_text", "table_row", "table_row"]
        unit_kind_by_id = {u.id: u.unit_kind for u in units}

        chunks = session.execute(
            select(Chunk).where(Chunk.evidence_version_id == version.id)
        ).scalars().all()
        assert len(chunks) == 5
        assert all(c.provenance == "extracted" for c in chunks)

        notes_text = next(
            c.text for c in chunks if unit_kind_by_id[c.evidence_unit_id] == "notes"
        )
        slide_text = next(
            c.text for c in chunks if unit_kind_by_id[c.evidence_unit_id] == "slide_text"
        )
        assert "Remember to mention" in notes_text
        assert "Remember to mention" not in slide_text
        assert "Quarterly review" in slide_text
        assert "Quarterly review" not in notes_text

    chunk_ids = _chunk_ids_for_file(env, path)
    assert chunk_ids
    assert env.vector.existing_chunk_ids(chunk_ids) == chunk_ids
    assert env.fts.existing_chunk_ids(chunk_ids) == chunk_ids
    assert _job_status(env, result.job_id).value == "succeeded"


def test_second_run_unchanged_pptx_is_skipped_and_not_reembedded(env: SimpleNamespace) -> None:
    path = env.folder / "deck.pptx"
    _write_pptx(path, slide_text="Stable content")

    first = env.pipeline.run_ingestion_for_source(env.source.id)
    assert first.file_results[0].status == "ingested"
    embed_calls_after_first = len(env.gateway.embed_calls)
    assert embed_calls_after_first > 0

    second = env.pipeline.run_ingestion_for_source(env.source.id)
    assert second.file_results[0].status == "unchanged"
    assert second.file_results[0].chunks_written == 0
    assert len(env.gateway.embed_calls) == embed_calls_after_first


def test_slide_with_no_extractable_content_ingests_without_crashing(
    env: SimpleNamespace,
) -> None:
    path = env.folder / "empty_slide.pptx"
    _write_pptx(path, slide_text=None)  # blank-layout slide, nothing added to it

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    # No ingestable content anywhere in the (one-file) source: treated as a
    # failed job (see pipeline.py's "no ingestable files found" handling is
    # NOT this case -- the file itself is discovered and processed, it just
    # yields zero chunks), but the file-level result must still be a clean
    # "ingested, zero chunks" rather than a crash/failure.
    [file_result] = result.file_results
    assert file_result.status == "ingested"
    assert file_result.chunks_written == 0

    with env.session_factory() as session:
        version = session.execute(
            select(EvidenceVersion).where(EvidenceVersion.file_path == str(path))
        ).scalar_one()
        assert version.status == VersionStatus.READY
        units = session.execute(
            select(EvidenceUnit).where(EvidenceUnit.evidence_version_id == version.id)
        ).scalars().all()
        assert units == []


def test_ppt_file_fails_with_explicit_unsupported_format_error(env: SimpleNamespace) -> None:
    path = env.folder / "legacy.ppt"
    path.write_bytes(b"not a real legacy ppt file")

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.files_processed == 1
    assert result.files_failed == 1
    [file_result] = result.file_results
    assert file_result.status == "failed"
    assert "unsupported" in file_result.error.lower()
    assert "ppt" in file_result.error.lower()

    # No EvidenceVersion row at all for a format refused before ingest_file.
    with env.session_factory() as session:
        versions = session.execute(select(EvidenceVersion)).scalars().all()
        assert versions == []


def test_docx_and_pptx_in_same_source_both_ingest_through_same_pipeline(
    env: SimpleNamespace,
) -> None:
    docx_path = env.folder / "notes.docx"
    pptx_path = env.folder / "deck.pptx"
    _write_docx(docx_path, "Notes", "Some prose content about glaciers.")
    _write_pptx(pptx_path, slide_text="Deck content about glaciers.")

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.status == "succeeded"
    assert result.files_processed == 2
    assert result.files_failed == 0
    assert {r.status for r in result.file_results} == {"ingested"}

    with env.session_factory() as session:
        versions = {
            v.file_path: v.parser_name
            for v in session.execute(select(EvidenceVersion)).scalars().all()
        }
    assert versions[str(docx_path)] == "docling"
    assert versions[str(pptx_path)] == "python-pptx"
