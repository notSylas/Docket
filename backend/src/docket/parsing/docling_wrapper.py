"""Thin wrapper around Docling's ``DocumentConverter`` used by ingestion.

Isolates the rest of the codebase from Docling's API and exception surface:
callers get a plain ``ParsedDocument`` back, or a ``ParseError`` they can
catch to skip a single bad file without crashing a batch ingestion run (a
later checkpoint's concern -- this module just makes that possible).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from importlib.metadata import version

from docket.parsing.normalize import unescape_markdown

# Inline, invisible page-boundary sentinel inserted into markdown at points
# where Docling's page number changes (see `_insert_page_markers`).
# `docket.parsing.chunker` recognizes and strips these; they must never
# reach `ParsedDocument.text` or anything downstream of it.
_PAGE_MARKER_RE = re.compile(r"<!--PAGE:(\d+)-->")


def _page_marker(page_no: int) -> str:
    return f"<!--PAGE:{page_no}-->"


@dataclass
class ParsedDocument:
    text: str  # full markdown, already unescaped via normalize.unescape_markdown, marker-free
    source_path: Path
    parser_name: str  # e.g. "docling"
    parser_version: str  # docling.__version__
    formula_regions: list[dict] = field(default_factory=list)  # source coordinates, not recognized text
    # Same content as `text`, but with `<!--PAGE:N-->` markers left in at
    # each genuine page transition -- consumed only by
    # `docket.parsing.chunker.chunk_document` to derive per-chunk
    # page_start/page_end. `None` when no page-boundary information could
    # be recovered (e.g. a fixture/fake `ParsedDocument` built without it);
    # callers should fall back to `text` in that case, which chunks exactly
    # as before with page_start/page_end left `None` throughout.
    text_with_page_markers: str | None = None


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


def _insert_page_markers(markdown: str, document: object) -> str:
    """Insert ``<!--PAGE:N-->`` right before the first (forward) occurrence
    of each text item's content in ``markdown``, at every point where the
    page number genuinely changes.

    Walks ``document.texts`` in order (the same field/shape
    ``_formula_regions`` reads for ``.prov[0].page_no``) and, for each
    item's whitespace-collapsed ``.text``, searches for it in ``markdown``
    strictly forward of the previous match -- documents can repeat text
    (e.g. running headers), so searching forward avoids re-matching an
    earlier occurrence. Tables/formulas/images are transformed by the
    markdown exporter and typically won't be found verbatim; those items
    are simply skipped (this only needs to catch transitions on ordinary
    prose/heading text, which is the overwhelming majority of content).

    Runs against the *raw* (still CommonMark-escaped) markdown, before
    ``unescape_markdown`` -- Docling's own exported text is un-escaped, so
    matching it against the escaped export would silently fail on any
    prose containing escapable characters (e.g. underscores); comparing
    against the raw export is the more accurate match target for the
    overwhelming majority of items.

    Never inserts a marker out of order: once a marker for page N has been
    inserted, a later match whose page number is < N is skipped (no
    marker inserted) though the search position still advances past it.
    A marker is inserted only when the page number genuinely differs from
    the last one inserted (including the very first match, which always
    differs from the initial "no page seen yet" state) -- this also
    prevents duplicate markers for a page with several matched items.
    """
    search_from = 0
    current_page: int | None = None
    result = markdown

    for item in getattr(document, "texts", []) or []:
        prov_list = getattr(item, "prov", []) or []
        if not prov_list:
            continue
        page_no = getattr(prov_list[0], "page_no", None)
        if page_no is None:
            continue

        item_text = getattr(item, "text", None)
        if not item_text:
            continue
        normalized = " ".join(item_text.split())
        if not normalized:
            continue

        idx = result.find(normalized, search_from)
        if idx == -1:
            continue

        search_from = idx + len(normalized)

        if current_page is not None and page_no <= current_page:
            # Same page (no-op) or a backwards/out-of-order page number --
            # never insert a marker that would go backwards.
            continue

        marker = _page_marker(page_no)
        result = result[:idx] + marker + result[idx:]
        search_from += len(marker)
        current_page = page_no

    return result


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
            # Marker insertion runs on the raw (still-escaped) markdown --
            # see `_insert_page_markers`'s docstring for why -- then
            # unescape_markdown runs on both the marker-annotated and
            # marker-free variants. `text` itself is computed exactly as
            # before (unaffected by marker insertion): no regression for
            # any existing caller.
            text = unescape_markdown(markdown)
            annotated_markdown = _insert_page_markers(markdown, result.document)
            text_with_page_markers = unescape_markdown(annotated_markdown)
            formula_regions = _formula_regions(result.document)
        except Exception as exc:  # noqa: BLE001 -- intentionally broad, see docstring
            raise ParseError(source_id, path, exc) from exc

        return ParsedDocument(
            text=text,
            source_path=path,
            parser_name=self._parser_name,
            parser_version=self._parser_version,
            formula_regions=formula_regions,
            text_with_page_markers=text_with_page_markers,
        )
