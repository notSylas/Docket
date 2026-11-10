"""Step 1b-D experiment: Docling with vs without OCR on PDFs that have a text layer.

    PYTHONPATH=backend/src python bench_ocr_skip.py --pdf-dir DIR --n 20 --out OCR_SKIP_RESULTS.json

For N PDFs (seeded sample, <= --max-pages pages each): detect the text layer, then parse with the default
converter (OCR on) and, for PDFs that have a text layer, with OCR disabled; report seconds, pages/s and text
equivalence (exact match, difflib ratio, character counts). Results are appended per file so an interruption
keeps what was measured. CPU only.
"""

from __future__ import annotations

import argparse
import difflib
import json
import random
import time
from pathlib import Path

import pypdfium2 as pdfium

from docket.infra.parsing.docling_wrapper import DoclingParser, pdf_has_text_layer


def pages_of(p: Path) -> int:
    d = pdfium.PdfDocument(str(p))
    try:
        return len(d)
    finally:
        d.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    pdfs = sorted(Path(a.pdf_dir).rglob("*.pdf"))
    random.Random(a.seed).shuffle(pdfs)
    picked = []
    for p in pdfs:
        try:
            if pages_of(p) <= a.max_pages:
                picked.append(p)
        except Exception:
            pass
        if len(picked) >= a.n:
            break
    on = DoclingParser(generate_page_images=False)
    off = DoclingParser(generate_page_images=False, skip_ocr_with_text_layer=True)
    on.parse("s", picked[0])  # warm model load
    off.parse("s", picked[0])
    out = Path(a.out)
    for p in picked:
        row = {"file": p.name, "pages": pages_of(p), "text_layer": pdf_has_text_layer(p)}
        t = time.perf_counter(); r_on = on.parse("s", p); row["sec_ocr_on"] = round(time.perf_counter() - t, 2)
        if row["text_layer"]:
            t = time.perf_counter(); r_off = off.parse("s", p); row["sec_ocr_off"] = round(time.perf_counter() - t, 2)
            row["identical_text"] = r_on.text == r_off.text
            row["similarity"] = round(difflib.SequenceMatcher(None, r_on.text, r_off.text, autojunk=False).ratio(), 4) if len(r_on.text) < 200_000 else None
            row["chars_on"], row["chars_off"] = len(r_on.text), len(r_off.text)
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
