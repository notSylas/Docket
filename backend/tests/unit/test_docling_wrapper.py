"""Tests for attest.parsing.docling_wrapper.DoclingParser.

No Ollama/GPU dependency here -- everything runs locally through Docling's
own models, so nothing in this file needs the ``integration`` marker. The
real-file smoke test is slower (Docling loads layout/OCR/table models on
first use, ~seconds) but still fully local and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from attest.parsing.docling_wrapper import DoclingParser, ParseError, ParsedDocument

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "Docs"
SAMPLE_DOCX = DOCS_DIR / "01_Work_Intelligence_PRD_v1.0.docx"


@pytest.fixture(scope="module")
def parser() -> DoclingParser:
    # Constructed once per test module: Docling loads its models on
    # DocumentConverter() construction, which is expensive the first time
    # (see spike/RESULTS.md's first-call-latency finding).
    return DoclingParser()


def test_parse_directory_raises_parse_error_not_raw_docling_exception(
    parser: DoclingParser, tmp_path: Path
) -> None:
    fake_docx_dir = tmp_path / "not_really_a_file.docx"
    fake_docx_dir.mkdir()

    with pytest.raises(ParseError) as exc_info:
        parser.parse("src-directory", fake_docx_dir)

    err = exc_info.value
    assert err.source_id == "src-directory"
    assert err.path == fake_docx_dir
    assert isinstance(err.cause, Exception)
    assert not isinstance(err.cause, ParseError)
    assert "src-directory" in str(err)
    assert str(fake_docx_dir) in str(err)


def test_parse_corrupt_file_with_misleading_extension_raises_parse_error(
    parser: DoclingParser, tmp_path: Path
) -> None:
    fake_docx = tmp_path / "fake.docx"
    fake_docx.write_text("this is plain text, not a real .docx file")

    with pytest.raises(ParseError) as exc_info:
        parser.parse("src-corrupt", fake_docx)

    err = exc_info.value
    assert err.source_id == "src-corrupt"
    assert err.path == fake_docx
    assert isinstance(err.cause, Exception)


def test_parse_nonexistent_file_raises_parse_error(
    parser: DoclingParser, tmp_path: Path
) -> None:
    missing = tmp_path / "does_not_exist.docx"

    with pytest.raises(ParseError) as exc_info:
        parser.parse("src-missing", missing)

    assert exc_info.value.source_id == "src-missing"
    assert exc_info.value.path == missing


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="sample docx not present in Docs/")
def test_parse_real_docx_smoke_test(parser: DoclingParser) -> None:
    result = parser.parse("src-real", SAMPLE_DOCX)

    assert isinstance(result, ParsedDocument)
    assert result.source_path == SAMPLE_DOCX
    assert result.parser_name == "docling"
    assert result.parser_version  # non-empty, introspected from installed docling
    assert len(result.text) > 0
    # Proves unescape_markdown actually ran: a real design doc full of
    # snake_case identifiers should contain no leftover CommonMark escapes.
    assert "\\_" not in result.text
