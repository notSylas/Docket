"""Cheap pre-parse signature check: a file whose extension says PDF/Office
but whose first bytes say otherwise (e.g. a 95-byte text file named
`x.pdf`) is reported as "not a valid <type> file" without invoking
Docling/openpyxl/python-pptx and their multi-line tracebacks."""

from __future__ import annotations

from pathlib import Path

_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

_LABELS = {
    ".pdf": "PDF",
    ".docx": "Word (.docx)",
    ".xlsx": "Excel (.xlsx)",
    ".pptx": "PowerPoint (.pptx)",
}


class InvalidFileError(Exception):
    """The file's bytes do not match its extension's format."""

    def __init__(self, path: Path, label: str):
        self.path = path
        self.label = label
        super().__init__(f"not a valid {label} file")


def check_file_signature(path: Path) -> None:
    """Raise `InvalidFileError` when `path`'s magic bytes contradict its
    extension. Extensions without a known signature are never rejected."""
    suffix = Path(path).suffix.lower()
    label = _LABELS.get(suffix)
    if label is None:
        return
    with open(path, "rb") as fh:
        head = fh.read(1024)
    if suffix == ".pdf":
        ok = b"%PDF-" in head  # the spec allows junk before the header
    else:
        ok = head.startswith(_ZIP_SIGNATURES)
    if not ok:
        raise InvalidFileError(Path(path), label)
