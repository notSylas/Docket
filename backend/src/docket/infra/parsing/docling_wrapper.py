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

from docket.infra.parsing.filecheck import check_file_signature
from docket.infra.parsing.normalize import unescape_markdown

# Inline, invisible page-boundary sentinel inserted into markdown at points
# where Docling's page number changes (see `_insert_page_markers`).
# `docket.infra.parsing.chunker` recognizes and strips these; they must never
# reach `ParsedDocument.text` or anything downstream of it.
_PAGE_MARKER_RE = re.compile(r"<!--PAGE:(\d+)-->")

# Scale factor each page is rendered at when Docling produces
# `document.pages[n].image` (visual retrieval checkpoint 2) -- 2x the PDF's
# point-based page_width/page_height. Passed straight into `PdfPipelineOptions`
# below (see `DoclingParser.__init__`'s comment on why 2.0, not Docling's
# default of 1.0). Exposed as a module constant, not just inlined there, so
# any code converting a formula region's point-based bbox into this same
# page image's pixel coordinates (`docket.infra.parsing.formula_crop`) uses the
# exact value actually used to render -- never a second hardcoded "2.0"
# that could silently drift out of sync with the real render call.
PAGE_IMAGES_SCALE = 2.0


def _page_marker(page_no: int) -> str:
    return f"<!--PAGE:{page_no}-->"


@dataclass
class ParsedDocument:
    text: str  # full markdown, already unescaped via normalize.unescape_markdown, marker-free
    source_path: Path
    parser_name: str  # e.g. "docling"
    parser_version: str  # docling.__version__
    formula_regions: list[dict] = field(default_factory=list)  # source coordinates, not recognized text
    # page_no -> raw PNG bytes of that page's rendered image (visual
    # retrieval checkpoint 2). Only used as a VLM-description input
    # (`docket.infra.index.visual_index`) -- never citable evidence, never shown to
    # a user. A page missing from this dict means Docling couldn't produce an
    # image for it (see `_page_images`); callers must skip it, not error.
    page_images: dict[int, bytes] = field(default_factory=dict)
    # Same content as `text`, but with `<!--PAGE:N-->` markers left in at
    # each genuine page transition -- consumed only by
    # `docket.infra.parsing.chunker.chunk_document` to derive per-chunk
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


def _page_images(document: object) -> dict[int, bytes]:
    """Extract each page's rendered image as PNG bytes, for the VLM
    description pass (visual retrieval checkpoint 2).

    Reads `document.pages[page_no].image`, a Pydantic `ImageRef | None` (see
    `docling_core.types.doc.document`). Rendered via `.pil_image` (a PIL
    Image, re-encoded to PNG here) rather than parsing `.uri`'s data-URI by
    hand. A page whose `image` is `None` (e.g. generation failed for that
    page) is skipped, not an error -- `generate_page_images` is best-effort
    per Docling's own semantics.
    """
    from io import BytesIO

    images: dict[int, bytes] = {}
    pages = getattr(document, "pages", {}) or {}
    items = pages.items() if isinstance(pages, dict) else []
    for page_no, page in items:
        image_ref = getattr(page, "image", None)
        if image_ref is None:
            continue
        pil_image = getattr(image_ref, "pil_image", None)
        if pil_image is None:
            continue
        buffer = BytesIO()
        pil_image.save(buffer, format="PNG")
        images[page_no] = buffer.getvalue()
    return images


#: Minimum length (in whitespace-collapsed characters) a `document.texts`
#: item's text must have before it's trusted as a search anchor for marker
#: insertion. Below this length, an item is skipped entirely -- neither
#: used to place a marker nor allowed to advance the forward search cursor.
#:
#: Root-caused against two real 26/29-page NCERT physics chapter PDFs
#: (see docling_wrapper fix history): short, generic, or per-page-repeating
#: strings -- single-letter formula variable labels docling splits into
#: their own text item ("A", "E", "l", ...), running headers/footers
#: ("Physics", "Reprint 2026-27", "EXAMPLE 3.1"), and even a full repeated
#: chapter-title running header ("Moving Charges and Magnetism", 28 chars)
#: -- recur dozens of times across a chapter. Forward-searching for one of
#: these can land on a wildly wrong (much-too-far-ahead) occurrence instead
#: of the intended one, since "first match forward of the cursor" is a
#: near-arbitrary occurrence when the text repeats constantly. Once that
#: happens the shared search cursor overshoots real content that never gets
#: revisited (search is forward-only), and every subsequent item silently
#: stops matching -- observed in practice as markers correctly appearing for
#: the first few pages and then permanently stopping for the rest of the
#: document. 32 was chosen empirically: it comfortably exceeds every
#: generic/repeating string observed in either fixture (max 28 chars) while
#: still being well under typical prose sentence length, so real transition
#: text on ordinary pages is essentially never excluded by it.
_MIN_ANCHOR_LEN = 32


def _anchor_pattern(normalized: str) -> re.Pattern[str]:
    """Build a regex that finds ``normalized`` in raw markdown tolerant of
    exactly how much whitespace separates each word.

    ``normalized`` has already collapsed every run of whitespace in the
    source ``item.text`` to a single space. Docling's raw markdown export
    does not reliably mirror that whitespace one-for-one -- confirmed
    against the same two real PDFs noted on ``_MIN_ANCHOR_LEN``: ordinary
    prose is routinely exported with runs of two or more literal spaces
    between words (hundreds of occurrences per document) where the
    whitespace-collapsed item text has only one. A plain ``str.find`` of
    the collapsed needle against the raw haystack silently fails on any
    such item -- not a rare edge case here, but the majority of prose items
    past the first page or two. Escaping the needle for regex safety and
    then turning each of *those* single spaces into ``\\s+`` matches either
    representation without needing to build (and index-map back from) a
    whitespace-collapsed copy of the whole markdown string.
    """
    return re.compile(re.escape(normalized).replace(r"\ ", r"\s+"))


def _insert_page_markers(markdown: str, document: object) -> str:
    """Insert ``<!--PAGE:N-->`` right before the first (forward) occurrence
    of each text item's content in ``markdown``, at every point where the
    page number genuinely changes.

    Walks ``document.texts`` in order (the same field/shape
    ``_formula_regions`` reads for ``.prov[0].page_no``) and, for each
    item's whitespace-collapsed ``.text`` long enough to trust as an anchor
    (see ``_MIN_ANCHOR_LEN``), searches for it in ``markdown`` strictly
    forward of the previous match, whitespace-tolerantly (see
    ``_anchor_pattern``) -- documents can repeat text (e.g. running
    headers), so searching forward avoids re-matching an earlier
    occurrence. Tables/formulas/images are transformed by the markdown
    exporter and typically won't be found verbatim; those items are simply
    skipped (this only needs to catch transitions on ordinary prose/heading
    text, which is the overwhelming majority of content) -- as are items
    below the minimum anchor length, which skip searching *and* leave the
    cursor untouched, so they can never corrupt it (see ``_MIN_ANCHOR_LEN``).

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
        if len(normalized) < _MIN_ANCHOR_LEN:
            # Too short/generic to trust as a search anchor -- skip without
            # touching the cursor (see _MIN_ANCHOR_LEN).
            continue

        match = _anchor_pattern(normalized).search(result, search_from)
        if match is None:
            continue

        idx = match.start()
        search_from = match.end()

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
        # generate_page_images=True: captures each page's rendered image so a
        # VLM can describe it as a retrieval-ranking signal (visual retrieval
        # checkpoint 2) -- never citable evidence, see ParsedDocument.page_images.
        # images_scale=2.0 (Docling's default is 1.0): a real, considered choice,
        # not an arbitrary bump -- a VLM reading a small/blurry render will
        # describe it worse (missed headings, misread numbers), so legibility
        # is worth the extra memory/time per page during ingestion.
        options = PdfPipelineOptions(
            do_formula_enrichment=False,
            generate_page_images=True,
            images_scale=PAGE_IMAGES_SCALE,
        )
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
            # Cheap magic-byte check first: a text file named *.pdf is
            # reported as "not a valid PDF file" without invoking Docling.
            check_file_signature(path)
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
            page_images = _page_images(result.document)
        except Exception as exc:  # noqa: BLE001 -- intentionally broad, see docstring
            raise ParseError(source_id, path, exc) from exc

        return ParsedDocument(
            text=text,
            source_path=path,
            parser_name=self._parser_name,
            parser_version=self._parser_version,
            formula_regions=formula_regions,
            text_with_page_markers=text_with_page_markers,
            page_images=page_images,
        )
