"""Concise ingest output: signature pre-check, one-line failures, dedupe,
and third-party log/progress suppression."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from docket.infra.parsing import docling_wrapper as dw
from docket.infra.parsing.docling_wrapper import ParseError
from docket.infra.parsing.filecheck import InvalidFileError, check_file_signature
from docket.infra.parsing.xlsx_wrapper import XlsxParser
from docket.interfaces.cli.quiet import (
    FailureReporter,
    condense_error,
    quiet_ingest,
)


def test_signature_accepts_real_headers(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7\n...")
    check_file_signature(pdf)
    for ext in (".docx", ".xlsx", ".pptx"):
        f = tmp_path / f"a{ext}"
        f.write_bytes(b"PK\x03\x04rest")
        check_file_signature(f)


@pytest.mark.parametrize(
    "name,label",
    [
        ("x.pdf", "PDF"),
        ("x.docx", "Word (.docx)"),
        ("x.xlsx", "Excel (.xlsx)"),
        ("x.pptx", "PowerPoint (.pptx)"),
    ],
)
def test_signature_rejects_plain_text(tmp_path, name, label):
    f = tmp_path / name
    f.write_text("this is just plain text, not a document")
    with pytest.raises(InvalidFileError) as info:
        check_file_signature(f)
    assert str(info.value) == f"not a valid {label} file"


def test_signature_ignores_other_extensions(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# hi")
    check_file_signature(f)


def test_docling_parser_rejects_fake_pdf_without_invoking_converter(tmp_path):
    def boom(_path):
        raise AssertionError("converter must not be called")

    parser = object.__new__(dw.DoclingParser)
    parser._converter = SimpleNamespace(convert=boom)
    f = tmp_path / "fake.pdf"
    f.write_text("not a pdf")
    with pytest.raises(ParseError) as info:
        parser.parse("src_1", f)
    assert condense_error(info.value) == "not a valid PDF file"


def test_xlsx_parser_rejects_fake_xlsx(tmp_path):
    f = tmp_path / "fake.xlsx"
    f.write_text("not a workbook")
    parser = XlsxParser()
    with pytest.raises(Exception) as info:
        parser.parse("src_1", f)
    assert condense_error(info.value) == "not a valid Excel (.xlsx) file"


def test_condense_error_collapses_multiline_and_truncates():
    multi = "Input document /x/y.pdf is not valid.\n  File format not allowed\n  at File 'a.py', line 3"
    assert condense_error(multi) == "Input document /x/y.pdf is not valid."
    assert condense_error(None) == "unknown error"
    long = "x" * 500
    assert len(condense_error(long)) <= 140
    wrapped = "failed to parse /a b/c.pdf for source src_ab12: boom\nTraceback..."
    assert condense_error(wrapped) == "boom"


def test_failure_reporter_relative_and_dedupes(tmp_path):
    rep = FailureReporter(tmp_path)
    p = tmp_path / "tests" / "fixtures" / "bad.pdf"
    assert rep.line(p, "not a valid PDF file") == "FAILED tests/fixtures/bad.pdf: not a valid PDF file"
    assert rep.line(p, "not a valid PDF file") is None
    # a different path / different message is still reported
    assert rep.line(tmp_path / "other.pdf", "not a valid PDF file") is not None
    assert rep.line(p, "different problem") is not None
    outside = Path("/elsewhere/x.pdf")
    assert rep.line(outside, "e") == "FAILED /elsewhere/x.pdf: e"


def test_quiet_ingest_raises_levels_and_restores(monkeypatch):
    monkeypatch.delenv("DOCKET_DEBUG", raising=False)
    monkeypatch.delenv("TQDM_DISABLE", raising=False)
    monkeypatch.setenv("HF_HUB_DISABLE_PROGRESS_BARS", "0")
    noisy = logging.getLogger("RapidOCR")
    noisy.setLevel(logging.INFO)
    with quiet_ingest():
        assert noisy.level == logging.WARNING
        assert os.environ["TQDM_DISABLE"] == "1"
        # an explicit user setting is not overridden
        assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "0"
    assert noisy.level == logging.INFO
    assert "TQDM_DISABLE" not in os.environ


def test_quiet_ingest_is_noop_with_docket_debug(monkeypatch):
    monkeypatch.setenv("DOCKET_DEBUG", "1")
    monkeypatch.delenv("TQDM_DISABLE", raising=False)
    noisy = logging.getLogger("transformers")
    noisy.setLevel(logging.INFO)
    with quiet_ingest():
        assert noisy.level == logging.INFO
        assert "TQDM_DISABLE" not in os.environ
