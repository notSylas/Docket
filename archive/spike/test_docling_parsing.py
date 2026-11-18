"""Tier 2 spike: probe Docling's parsing robustness on messy inputs —
a table-heavy PDF, a mixed-formatting PDF, and an image-only ('scanned') PDF
that has no embedded text layer and must go through OCR.

Usage: python test_docling_parsing.py
"""
import time
from pathlib import Path

from docling.document_converter import DocumentConverter

TEST_DIR = Path(__file__).resolve().parent / "test_pdfs"

EXPECTATIONS = {
    "messy_table.pdf": [
        "FTS5 lexical search",
        "LanceDB vector search",
        "Reranker",
        "Qwen3-14B",
    ],
    "mixed_format.pdf": [
        "watchfiles",
        "authorized_sources",
        "evidence_version",
        "SR-07",
        "FR-RET-04",
    ],
    "scanned_style.pdf": [
        "abstention",
        "hallucinated",
        "chunk_recipes",
        "Phase 1 exit gate",
    ],
}


def main() -> None:
    converter = DocumentConverter()

    for pdf_name, expected_terms in EXPECTATIONS.items():
        path = TEST_DIR / pdf_name
        print(f"\n{'=' * 60}\n{pdf_name}\n{'=' * 60}")

        start = time.time()
        result = converter.convert(str(path))
        elapsed = time.time() - start
        text = result.document.export_to_markdown()

        print(f"Parse time: {elapsed:.2f}s | extracted chars: {len(text)}")

        found = [t for t in expected_terms if t.lower() in text.lower()]
        missing = [t for t in expected_terms if t.lower() not in text.lower()]

        print(f"Found {len(found)}/{len(expected_terms)} expected terms: {found}")
        if missing:
            print(f"MISSING: {missing}")

        n_tables = len(result.document.tables) if hasattr(result.document, "tables") else "?"
        print(f"Tables detected: {n_tables}")

        preview = text[:500].replace("\n", " ")
        print(f"Preview: {preview}")


if __name__ == "__main__":
    main()
