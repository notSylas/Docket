"""Thin wrapper around Docling's ``DocumentConverter`` used by ingestion.

Isolates the rest of the codebase from Docling's API and exception surface:
callers get a plain ``ParsedDocument`` back, or a ``ParseError`` they can
catch to skip a single bad file without crashing a batch ingestion run (a
later checkpoint's concern -- this module just makes that possible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from importlib.metadata import version

from docket.parsing.normalize import unescape_markdown


@dataclass
class ParsedDocument:
    text: str  # full markdown, already unescaped via normalize.unescape_markdown
    source_path: Path
    parser_name: str  # e.g. "docling"
    parser_version: str  # docling.__version__
    formula_regions: list[dict] = field(default_factory=list)  # source coordinates, not recognized text


class ParseError(Exception):
    """Raised for any failure parsing ``path`` for ``source_id``.

    Wraps the underlying Docling (or filesystem) exception as ``cause`` so
    callers never need to catch Docling's own exception types directly.
    """

    def __init__(self, source_id: str, path: Path, cause: Exception):
        self.source_id = source_id
        self.path = path
        self.cause = cause
        super().__init__(f"failed to parse {path} for source {source_id}: {cause}")


def _formula_regions(document: object) -> list[dict]:
    """Keep source coordinates for detected formulas without trusting OCR text."""
    regions: list[dict] = []
    for item in getattr(document, "texts", []) or []:
        if "formula" not in str(getattr(item, "label", "")).lower():
            continue
        for prov in getattr(item, "prov", []) or []:
            bbox = getattr(prov, "bbox", None)
            page_no = getattr(prov, "page_no", None)
            pages = getattr(document, "pages", {})
            page = pages.get(page_no) if isinstance(pages, dict) else None
            size = getattr(page, "size", None)
            origin = getattr(bbox, "coord_origin", None)
            regions.append({
                "item_ref": getattr(item, "self_ref", None),
                "page_no": page_no,
                "coordinate_origin": getattr(origin, "value", str(origin)) if origin is not None else None,
                "page_width": getattr(size, "width", None),
                "page_height": getattr(size, "height", None),
                "bbox": {axis: getattr(bbox, axis, None) for axis in ("l", "t", "r", "b")}
                if bbox is not None else None,
            })
    return regions


class DoclingParser:
    """Parses documents via Docling and returns normalized markdown text.

    ``DocumentConverter`` is constructed once (it loads layout/OCR/table
    models -- expensive, see spike/RESULTS.md's first-call-latency finding)
    and reused across ``parse()`` calls, mirroring the spike's usage.
    """

    def __init__(self) -> None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions

        # Formula OCR is generative and must not silently become authoritative evidence.
        options = PdfPipelineOptions(do_formula_enrichment=False)
        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        self._parser_name = "docling"
        self._parser_version = self._detect_version()

    @staticmethod
    def _detect_version() -> str:
        return version("docling")

    @property
    def parser_name(self) -> str:
        """The parser identity to record on `EvidenceVersion` rows -- exposed
        so callers (e.g. the ingestion pipeline) can pass it to
        `EvidenceManager.ingest_file` before parsing happens (bytes are
        stored, and their EvidenceVersion recorded, independently of whether
        parsing subsequently succeeds)."""
        return self._parser_name

    @property
    def parser_version(self) -> str:
        return self._parser_version

    def parse(self, source_id: str, path: Path) -> ParsedDocument:
        """Run Docling's ``DocumentConverter`` on ``path`` and export markdown.

        Any exception raised by Docling (or by the filesystem, e.g. a
        directory or unreadable/corrupt file) is caught and re-raised as
        ``ParseError(source_id, path, cause)`` -- never propagated raw, so a
        single bad file in a batch ingestion run can be caught and skipped.
        """
        try:
            result = self._converter.convert(str(path))
            markdown = result.document.export_to_markdown()
            text = unescape_markdown(markdown)
            formula_regions = _formula_regions(result.document)
        except Exception as exc:  # noqa: BLE001 -- intentionally broad, see docstring
            raise ParseError(source_id, path, exc) from exc

        return ParsedDocument(
            text=text,
            source_path=path,
            parser_name=self._parser_name,
            parser_version=self._parser_version,
            formula_regions=formula_regions,
        )
