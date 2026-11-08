"""Tier-1 "fast text" readers: text-only extraction with no layout models.

Used by tiered indexing to make a file keyword-searchable within seconds
instead of waiting for the full Docling path (layout, tables, OCR; roughly 0.6
pages/s/worker on CPU). Two formats have a fast path here:

- born-digital PDF: the text layer via pypdfium2 (a Docling dependency that is
  already installed; it is NOT declared in pyproject.toml, so it is imported
  lazily and a missing import simply means "no fast path" rather than an
  error). A PDF without a usable text layer (a scan, mostly images) returns
  `None`: such files go straight to Tier 2 and are never made provisional.
- .docx: paragraphs and tables via python-docx (already a dependency of the
  test suite and of Docling). Headings become markdown headings so the shared
  chunker splits units on them; tables become pipe tables.

Everything else (txt/md/csv/html/eml, xlsx, pptx) already has a lightweight
native reader + chunker that is the *same* code as the full path; the pipeline
reuses it for Tier 1 and the Tier-2 upgrade only adds embeddings.

The output is a `ParsedDocument` whose `text_with_page_markers` feeds
`chunk_document`, exactly like the Docling path, with its own parser identity
(`TIER1_PARSER_NAME`) so provisional chunk ids and recipe rows never collide
with the full-path chunks.
"""

from __future__ import annotations

import logging
from pathlib import Path

from docket.infra.parsing.docling_wrapper import ParsedDocument, _page_marker

logger = logging.getLogger(__name__)

TIER1_PARSER_NAME = "tier1-text"
TIER1_PARSER_VERSION = "1"
FAST_EXTENSIONS = {".pdf", ".docx"}

# A page counts as "has text" at this many characters; a PDF is treated as
# born-digital when at least this share of its pages do.
_MIN_PAGE_CHARS = 40
_MIN_TEXT_PAGE_SHARE = 0.5


class NoTextLayer(Exception):
    """The file has no usable embedded text (scan/image-only); go to Tier 2."""


def has_fast_path(path: Path) -> bool:
    return path.suffix.lower() in FAST_EXTENSIONS


def parse_pdf_text(path: Path, max_pages: int = 0) -> ParsedDocument:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise NoTextLayer("pypdfium2 is not installed") from exc
    pdf = pdfium.PdfDocument(str(path))
    try:
        n = len(pdf)
        if max_pages and max_pages > 0:
            n = min(n, max_pages)
        if n == 0:
            raise NoTextLayer("no pages")
        pages: list[str] = []
        for i in range(n):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    pages.append(tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
                finally:
                    tp.close()
            finally:
                page.close()
    finally:
        pdf.close()
    with_text = sum(1 for p in pages if len(p.strip()) >= _MIN_PAGE_CHARS)
    if with_text / len(pages) < _MIN_TEXT_PAGE_SHARE:
        raise NoTextLayer(f"only {with_text} of {len(pages)} pages carry a text layer")
    marked = "\n\n".join(f"{_page_marker(i + 1)}\n{p.strip()}" for i, p in enumerate(pages))
    plain = "\n\n".join(p.strip() for p in pages)
    return ParsedDocument(
        text=plain,
        source_path=path,
        parser_name=TIER1_PARSER_NAME,
        parser_version=TIER1_PARSER_VERSION,
        text_with_page_markers=marked,
    )


def _table_markdown(table) -> str:
    rows = []
    for row in table.rows:
        cells = []
        seen = set()
        for cell in row.cells:
            if id(cell._tc) in seen:  # merged cells repeat
                continue
            seen.add(id(cell._tc))
            cells.append(" ".join(cell.text.split()).replace("|", "/"))
        rows.append(cells)
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def parse_docx_text(path: Path) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    blocks: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            if style.lower().startswith("heading") or style == "Title":
                digits = "".join(ch for ch in style if ch.isdigit())
                level = min(max(int(digits) if digits else 1, 1), 6)
                blocks.append("#" * level + " " + text)
            else:
                blocks.append(text)
        elif tag == "tbl":
            md = _table_markdown(Table(child, document))
            if md:
                blocks.append(md)
    if not blocks:
        raise NoTextLayer("document has no text")
    text = "\n\n".join(blocks)
    return ParsedDocument(
        text=text,
        source_path=path,
        parser_name=TIER1_PARSER_NAME,
        parser_version=TIER1_PARSER_VERSION,
        text_with_page_markers=text,
    )


def parse_fast(path: Path, max_pages: int = 0) -> ParsedDocument:
    """Fast text for a PDF/docx; raises `NoTextLayer` when there is none."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf_text(path, max_pages)
    if suffix == ".docx":
        return parse_docx_text(path)
    raise NoTextLayer(f"no fast path for {suffix}")
