"""End-to-end `.xlsx` ingestion through `IngestionPipeline` -- the native
openpyxl counterpart to `test_pipeline.py`'s Docling-based coverage. Proves
the format dispatch added for the spreadsheet adapter (`pipeline.py`'s
`_ingest_one_xlsx_file`) goes through the same `EvidenceManager`/`ChunkWriter`/
`IndexManager` seam as PDF/DOCX, not a parallel path, and that the new
`unit_kind`/`locator_json`/`provenance` columns land correctly end to end.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from conftest import _chunk_ids_for_file, _job_status, _write_docx, _write_xlsx
from docket.core.db.models import Chunk, EvidenceUnit, EvidenceVersion, VersionStatus


def test_xlsx_file_ingests_and_indexes_with_row_grouped_units(env: SimpleNamespace) -> None:
    path = env.folder / "revenue.xlsx"
    _write_xlsx(
        path,
        "Revenue",
        ["Month", "Amount"],
        [["January", 120.5], ["February", 130.0]],
    )

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.status == "succeeded"
    assert result.files_processed == 1
    assert result.files_failed == 0
    [file_result] = result.file_results
    assert file_result.status == "ingested"
    assert file_result.chunks_written == 2  # one EvidenceUnit/Chunk per data row

    with env.session_factory() as session:
        version = session.execute(
            select(EvidenceVersion).where(EvidenceVersion.file_path == str(path))
        ).scalar_one()
        assert version.status == VersionStatus.READY
        assert version.parser_name == "openpyxl"

        units = session.execute(
            select(EvidenceUnit).where(EvidenceUnit.evidence_version_id == version.id)
        ).scalars().all()
        assert len(units) == 2
        assert all(u.unit_kind == "range" for u in units)
        assert all(u.locator_json is not None and "Revenue" in u.locator_json for u in units)

        chunks = session.execute(
            select(Chunk).where(Chunk.evidence_version_id == version.id)
        ).scalars().all()
        assert len(chunks) == 2
        assert all(c.provenance == "extracted" for c in chunks)

    chunk_ids = _chunk_ids_for_file(env, path)
    assert chunk_ids
    assert env.vector.existing_chunk_ids(chunk_ids) == chunk_ids
    assert env.fts.existing_chunk_ids(chunk_ids) == chunk_ids
    assert _job_status(env, result.job_id).value == "succeeded"


def test_second_run_unchanged_xlsx_is_skipped_and_not_reembedded(env: SimpleNamespace) -> None:
    path = env.folder / "revenue.xlsx"
    _write_xlsx(path, "Revenue", ["Month", "Amount"], [["January", 120.5]])

    first = env.pipeline.run_ingestion_for_source(env.source.id)
    assert first.file_results[0].status == "ingested"
    embed_calls_after_first = len(env.gateway.embed_calls)
    assert embed_calls_after_first > 0

    second = env.pipeline.run_ingestion_for_source(env.source.id)
    assert second.file_results[0].status == "unchanged"
    assert second.file_results[0].chunks_written == 0
    assert len(env.gateway.embed_calls) == embed_calls_after_first


def test_xlsm_file_fails_with_explicit_unsupported_format_error(env: SimpleNamespace) -> None:
    path = env.folder / "macro.xlsm"
    _write_xlsx(path, "Sheet1", ["A"], [[1]])  # content is irrelevant; extension decides

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.files_processed == 1
    assert result.files_failed == 1
    [file_result] = result.file_results
    assert file_result.status == "failed"
    assert "unsupported" in file_result.error.lower()
    assert ".xlsm" in file_result.error or "xlsm" in file_result.error.lower()

    # No EvidenceVersion row at all for a format refused before ingest_file.
    with env.session_factory() as session:
        versions = session.execute(select(EvidenceVersion)).scalars().all()
        assert versions == []


def test_xls_file_fails_with_explicit_unsupported_format_error(env: SimpleNamespace) -> None:
    path = env.folder / "legacy.xls"
    path.write_bytes(b"not a real legacy xls file")

    result = env.pipeline.run_ingestion_for_source(env.source.id)

    assert result.files_failed == 1
    [file_result] = result.file_results
    assert file_result.status == "failed"
    assert "unsupported" in file_result.error.lower()


def test_docx_and_xlsx_in_same_source_both_ingest_through_same_pipeline(
    env: SimpleNamespace,
) -> None:
    docx_path = env.folder / "notes.docx"
    xlsx_path = env.folder / "data.xlsx"
    _write_docx(docx_path, "Notes", "Some prose content about glaciers.")
    _write_xlsx(xlsx_path, "Data", ["Key", "Value"], [["Pi", 3.14]])

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
    assert versions[str(xlsx_path)] == "openpyxl"
