"""Tests for docket.parsing.docling_wrapper.DoclingParser.

No Ollama/GPU dependency here -- everything runs locally through Docling's
own models, so nothing in this file needs the ``integration`` marker. The
real-file smoke test is slower (Docling loads layout/OCR/table models on
first use, ~seconds) but still fully local and deterministic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from docket.parsing.docling_wrapper import DoclingParser, ParseError, ParsedDocument

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


def test_formula_provenance_preserves_coordinates_without_ocr_text():
    from types import SimpleNamespace as NS
    from docket.parsing.docling_wrapper import _formula_regions
    document = NS(texts=[NS(label="formula", self_ref="#/texts/2", text="invented equation",
                           prov=[NS(page_no=3, bbox=NS(l=1, t=2, r=10, b=20, coord_origin="TOPLEFT"))])],
                  pages={3: NS(size=NS(width=600, height=800))})
    (region,) = _formula_regions(document)
    assert region["page_no"] == 3 and region["item_ref"] == "#/texts/2"
    assert region["bbox"] == {"l": 1, "t": 2, "r": 10, "b": 20}
    assert region["coordinate_origin"] == "TOPLEFT"
    assert region["page_width"] == 600
    assert "invented" not in str(region)


# ---------------------------------------------------------------------------
# Page markers (<!--PAGE:N-->) -- provenance for the visual retrieval
# checkpoint. Same fake `document.texts`/`.prov` mocking style as the
# formula-region test above, since `_insert_page_markers` reads the exact
# same field (`item.prov[0].page_no`) that `_formula_regions` reads.
# ---------------------------------------------------------------------------


def _text_item(text: str, page_no: int):
    from types import SimpleNamespace as NS
    return NS(text=text, prov=[NS(page_no=page_no)])


def test_insert_page_markers_at_genuine_transitions_only():
    from docket.parsing.docling_wrapper import _insert_page_markers
    from types import SimpleNamespace as NS

    markdown = (
        "Intro paragraph on page one.\n\n"
        "Still page one, second paragraph.\n\n"
        "First paragraph on page two.\n\n"
        "Still page two.\n\n"
        "First paragraph on page three.\n"
    )
    document = NS(texts=[
        _text_item("Intro paragraph on page one.", 1),
        _text_item("Still page one, second paragraph.", 1),
        _text_item("First paragraph on page two.", 2),
        _text_item("Still page two.", 2),
        _text_item("First paragraph on page three.", 3),
    ])

    annotated = _insert_page_markers(markdown, document)

    assert annotated.count("<!--PAGE:1-->") == 1
    assert annotated.count("<!--PAGE:2-->") == 1
    assert annotated.count("<!--PAGE:3-->") == 1
    # Marker order in the string matches document/page order.
    assert (
        annotated.index("<!--PAGE:1-->")
        < annotated.index("<!--PAGE:2-->")
        < annotated.index("<!--PAGE:3-->")
    )
    # Markers sit immediately before the matched text, not scattered.
    assert "<!--PAGE:2-->First paragraph on page two." in annotated
    assert "<!--PAGE:3-->First paragraph on page three." in annotated
    # Stripping markers reproduces the original markdown exactly.
    assert re.sub(r"<!--PAGE:\d+-->", "", annotated) == markdown


def test_insert_page_markers_skips_unmatched_items_without_error():
    from docket.parsing.docling_wrapper import _insert_page_markers
    from types import SimpleNamespace as NS

    markdown = "Only this sentence survived the exporter.\n"
    document = NS(texts=[
        # A table/formula/image item whose text never appears verbatim in
        # the exported markdown -- must be silently skipped, not raise.
        _text_item("| a | b |\n| - | - |", 1),
        _text_item("Only this sentence survived the exporter.", 2),
    ])

    annotated = _insert_page_markers(markdown, document)

    # The unmatched item is skipped; the matched one still gets its marker
    # (this is the very first successful match, so it always gets one).
    assert annotated == "<!--PAGE:2-->Only this sentence survived the exporter.\n"


def test_insert_page_markers_never_goes_backwards():
    from docket.parsing.docling_wrapper import _insert_page_markers
    from types import SimpleNamespace as NS

    markdown = "First on page five.\n\nThen an out-of-order item.\n"
    document = NS(texts=[
        _text_item("First on page five.", 5),
        # Docling/layout quirk: a later item claims an earlier page number.
        _text_item("Then an out-of-order item.", 2),
    ])

    annotated = _insert_page_markers(markdown, document)

    assert annotated.count("<!--PAGE:") == 1
    assert "<!--PAGE:5-->First on page five." in annotated
    assert "<!--PAGE:2-->" not in annotated


def test_insert_page_markers_no_duplicate_marker_within_same_page():
    from docket.parsing.docling_wrapper import _insert_page_markers
    from types import SimpleNamespace as NS

    markdown = "Sentence A.\n\nSentence B.\n\nSentence C.\n"
    document = NS(texts=[
        _text_item("Sentence A.", 1),
        _text_item("Sentence B.", 1),
        _text_item("Sentence C.", 1),
    ])

    annotated = _insert_page_markers(markdown, document)

    assert annotated.count("<!--PAGE:1-->") == 1
    assert annotated.startswith("<!--PAGE:1-->Sentence A.")


def test_insert_page_markers_repeated_text_matches_forward_only():
    from docket.parsing.docling_wrapper import _insert_page_markers
    from types import SimpleNamespace as NS

    # "Running Header" appears twice -- once per page -- and must not both
    # collapse onto the first occurrence.
    markdown = "Running Header\n\nBody on page one.\n\nRunning Header\n\nBody on page two.\n"
    document = NS(texts=[
        _text_item("Running Header", 1),
        _text_item("Body on page one.", 1),
        _text_item("Running Header", 2),
        _text_item("Body on page two.", 2),
    ])

    annotated = _insert_page_markers(markdown, document)

    assert annotated.count("<!--PAGE:1-->") == 1
    assert annotated.count("<!--PAGE:2-->") == 1
    first_header_idx = annotated.index("Running Header")
    marker2_idx = annotated.index("<!--PAGE:2-->")
    second_header_idx = annotated.index("Running Header", first_header_idx + 1)
    # The page-2 marker precedes the *second* "Running Header", not the first.
    assert marker2_idx < second_header_idx
    assert marker2_idx > first_header_idx


# ---------------------------------------------------------------------------
# Page images (ParsedDocument.page_images) -- visual retrieval checkpoint 2.
# ---------------------------------------------------------------------------


def test_page_images_extracts_png_bytes_and_skips_pages_without_image():
    from types import SimpleNamespace as NS
    from PIL import Image
    from docket.parsing.docling_wrapper import _page_images

    tiny_image = Image.new("RGB", (4, 4))
    document = NS(
        pages={
            1: NS(image=NS(pil_image=tiny_image)),
            2: NS(image=None),  # generation failed for this page -- must be skipped
        }
    )

    images = _page_images(document)

    assert set(images.keys()) == {1}
    assert isinstance(images[1], bytes)
    assert len(images[1]) > 0
    # A real PNG, not just arbitrary bytes.
    assert images[1].startswith(b"\x89PNG\r\n\x1a\n")


def test_page_images_empty_when_no_pages_have_images():
    from types import SimpleNamespace as NS
    from docket.parsing.docling_wrapper import _page_images

    document = NS(pages={1: NS(image=None), 2: NS(image=None)})
    assert _page_images(document) == {}


def test_page_images_missing_pages_attribute_returns_empty():
    from types import SimpleNamespace as NS
    from docket.parsing.docling_wrapper import _page_images

    document = NS()
    assert _page_images(document) == {}


def test_parsed_document_page_images_end_to_end_via_fake_converter(tmp_path):
    """Exercises `DoclingParser.parse()` end to end (same fake-converter
    technique as `test_parsed_document_text_has_no_markers_fake_docling_result`
    below), confirming `ParsedDocument.page_images` is populated from
    `document.pages[n].image.pil_image` and pages with `image=None` are
    skipped, not errored."""
    from types import SimpleNamespace as NS
    from PIL import Image
    from docket.parsing import docling_wrapper as dw

    tiny_image = Image.new("RGB", (4, 4))
    document = NS(
        texts=[],
        pages={
            1: NS(image=NS(pil_image=tiny_image)),
            2: NS(image=None),
        },
    )
    document.export_to_markdown = lambda: "Some page text.\n"
    fake_result = NS(document=document)

    parser = object.__new__(dw.DoclingParser)
    parser._converter = NS(convert=lambda path: fake_result)
    parser._parser_name = "docling"
    parser._parser_version = "test-version"

    fake_path = tmp_path / "fake.pdf"
    fake_path.write_text("irrelevant")
    result = parser.parse("src-fake", fake_path)

    assert set(result.page_images.keys()) == {1}
    assert result.page_images[1].startswith(b"\x89PNG\r\n\x1a\n")


def test_parsed_document_text_has_no_markers_fake_docling_result(monkeypatch, tmp_path):
    """Exercises `DoclingParser.parse()` end to end with a faked converter
    result (no real Docling model load), confirming `ParsedDocument.text`
    stays marker-free while `text_with_page_markers` carries them."""
    from types import SimpleNamespace as NS
    from docket.parsing import docling_wrapper as dw

    document = NS(texts=[
        _text_item("Page one sentence.", 1),
        _text_item("Page two sentence.", 2),
    ], pages={})

    def fake_export_to_markdown():
        return "Page one sentence.\n\nPage two sentence.\n"

    document.export_to_markdown = fake_export_to_markdown
    fake_result = NS(document=document)

    parser = object.__new__(dw.DoclingParser)
    parser._converter = NS(convert=lambda path: fake_result)
    parser._parser_name = "docling"
    parser._parser_version = "test-version"

    fake_path = tmp_path / "fake.pdf"
    fake_path.write_text("irrelevant")
    result = parser.parse("src-fake", fake_path)

    assert "<!--PAGE:" not in result.text
    assert result.text_with_page_markers is not None
    assert "<!--PAGE:2-->" in result.text_with_page_markers
    assert "<!--PAGE:" not in result.text_with_page_markers.replace(
        "<!--PAGE:2-->", ""
    ).replace("<!--PAGE:1-->", "")
