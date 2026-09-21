"""Tier 2 spike: generate synthetic 'messy' PDFs to probe Docling's parsing
robustness beyond clean .docx (tables, multi-column layout, scanned/image-only text).

Usage: python gen_test_pdfs.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet

OUT_DIR = Path(__file__).resolve().parent / "test_pdfs"
OUT_DIR.mkdir(exist_ok=True)


def gen_messy_table_pdf():
    path = OUT_DIR / "messy_table.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Quarterly Resource Benchmark Results", styles["Title"]), Spacer(1, 12)]

    data = [
        ["Component", "P50 Latency (ms)", "P99 Latency (ms)", "RAM (GB)", "Notes"],
        ["FTS5 lexical search", "8", "42", "0.3", "baseline, no rerank"],
        ["LanceDB vector search", "15", "88", "1.2", "IVF_PQ index"],
        ["RRF fusion", "1", "3", "0.01", "in-process"],
        ["Reranker (0.6B)", "", "210", "0.9", "optional stage; blank = not measured"],
        ["Qwen3-14B generation", "1900", "4200", "11.4", "GPU, ROCm"],
    ]
    table = Table(data, colWidths=[1.6 * inch, 1.1 * inch, 1.1 * inch, 0.8 * inch, 1.8 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("SPAN", (3, 4), (3, 4)),
            ]
        )
    )
    elements.append(table)
    elements.append(Spacer(1, 20))
    elements.append(
        Paragraph(
            "Note: the reranker's P50 latency was not captured in this run due to a "
            "logging gap; treat the blank cell above as missing data, not zero.",
            styles["Normal"],
        )
    )
    doc.build(elements)
    print(f"Wrote {path}")


def gen_mixed_format_pdf():
    path = OUT_DIR / "mixed_format.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=letter, topMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("Appendix C — Example Lifecycle Transactions", styles["Heading1"]),
        Paragraph(
            "This appendix walks through three example transactions against the "
            "evidence store: (1) a new PDF is discovered by the file watcher, (2) a "
            "source is revoked by the user, and (3) a historical question is asked "
            "about a document that has since changed.",
            styles["BodyText"],
        ),
        Spacer(1, 10),
        Paragraph("C.1 New PDF Discovered", styles["Heading2"]),
        Paragraph(
            "1. watchfiles emits a create event for the new path.\n"
            "2. Source Manager checks the path against authorized_sources scopes.\n"
            "3. If in scope, an ingestion_job row is created with status=pending.\n"
            "4. Parser Manager invokes Docling; on success an evidence_version row "
            "is written with a content hash and the raw bytes are stored "
            "content-addressed under objects/.",
            styles["Code"],
        ),
        Spacer(1, 10),
        Paragraph("C.2 Source Revoked", styles["Heading2"]),
        Paragraph(
            "Revocation must be immediate and must block the source from all "
            "future retrieval within one reconciliation cycle, per SR-07.",
            styles["Italic"],
        ),
        Spacer(1, 10),
        Paragraph("C.3 Historical Question", styles["Heading2"]),
        Paragraph(
            "\"What did the retry policy look like before commit a1b2c3d?\" — this "
            "requires resolving evidence_version_id at a prior point in time, not "
            "just the latest version, per FR-RET-04.",
            styles["Normal"],
        ),
    ]
    doc.build(elements)
    print(f"Wrote {path}")


def gen_scanned_style_pdf():
    """Render a page of text purely as a raster image (no embedded text layer),
    to simulate a scanned document and force Docling's OCR path."""
    img = Image.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        font_bold = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34
        )
    except OSError:
        font = ImageFont.load_default()
        font_bold = font

    lines = [
        ("Work Intelligence — Scanned Field Note (simulated)", font_bold),
        ("", font),
        ("Observed during pilot review, 14 Sept 2026:", font),
        ("The abstention behavior correctly triggered on three", font),
        ("out of three out-of-corpus questions during manual", font),
        ("testing. No hallucinated citations were produced.", font),
        ("", font),
        ("Open item: chunking strategy still uses naive fixed", font),
        ("word-count splitting rather than semantic boundaries,", font),
        ("which caused one retrieval recall miss in testing.", font),
        ("", font),
        ("Action: revisit chunk_recipes before Phase 1 exit gate.", font),
    ]
    y = 100
    for text, f in lines:
        draw.text((100, y), text, fill="black", font=f)
        y += 60

    img_path = OUT_DIR / "_scanned_page.png"
    img.save(img_path)

    pdf_path = OUT_DIR / "scanned_style.pdf"
    img.convert("RGB").save(pdf_path, "PDF", resolution=200.0)
    img_path.unlink()
    print(f"Wrote {pdf_path}")


if __name__ == "__main__":
    gen_messy_table_pdf()
    gen_mixed_format_pdf()
    gen_scanned_style_pdf()
