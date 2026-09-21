"""Thin wrapper around Docling's ``DocumentConverter`` used by ingestion.

Isolates the rest of the codebase from Docling's API and exception surface:
callers get a plain ``ParsedDocument`` back, or a ``ParseError`` they can
catch to skip a single bad file without crashing a batch ingestion run (a
later checkpoint's concern -- this module just makes that possible).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from attest.parsing.normalize import unescape_markdown


@dataclass
class ParsedDocument:
    text: str  # full markdown, already unescaped via normalize.unescape_markdown
    source_path: Path
    parser_name: str  # e.g. "docling"
    parser_version: str  # docling.__version__


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


class DoclingParser:
    """Parses documents via Docling and returns normalized markdown text.

    ``DocumentConverter`` is constructed once (it loads layout/OCR/table
    models -- expensive, see spike/RESULTS.md's first-call-latency finding)
    and reused across ``parse()`` calls, mirroring the spike's usage.
    """

    def __init__(self) -> None:
        from docling.document_converter import DocumentConverter

        self._converter = DocumentConverter()
        self._parser_name = "docling"
        self._parser_version = self._detect_version()

    @staticmethod
    def _detect_version() -> str:
        import docling

        return getattr(docling, "__version__", "unknown")

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
        except Exception as exc:  # noqa: BLE001 -- intentionally broad, see docstring
            raise ParseError(source_id, path, exc) from exc

        return ParsedDocument(
            text=text,
            source_path=path,
            parser_name=self._parser_name,
            parser_version=self._parser_version,
        )
