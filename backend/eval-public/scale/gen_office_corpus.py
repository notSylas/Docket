"""Deterministic generator for a realistic-ish Office corpus for scale measurements.

    backend/.venv/bin/python gen_office_corpus.py OUTDIR [--docx 3000] [--xlsx 1500] [--pptx 500]
                                                  [--cuad-txt DIR] [--seed 7] [--procs 16]

Produces nested department/year/project folders with varied .docx/.xlsx/.pptx files. 5% of the files are
exact duplicates (byte copies under a different name/folder) and 5% are near-duplicate "version families"
(name_v1 / name_v2 / name_final with a few paragraphs / cells changed). Text is drawn from Faker business
vocabulary and, if --cuad-txt is given, from sentences of the CUAD contract text files (CC BY 4.0) so the
legal-ish documents have realistic term frequencies. Same seed => byte-identical file content.
A manifest.json (path -> kind/idx/variant/dup_of) is written next to the corpus root.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from multiprocessing import Pool
from pathlib import Path

DEPTS = ["finance", "legal", "hr", "engineering", "sales", "operations", "procurement", "marketing", "research", "it"]
DOC_KINDS = ["memo", "report", "agreement", "minutes", "policy", "proposal", "incident-review", "spec"]
SHEET_KINDS = ["ledger", "inventory", "timesheet", "budget", "pipeline", "survey", "orders"]
CATEGORIES = ["Travel", "Software", "Hardware", "Consulting", "Facilities", "Training", "Marketing", "Legal", "Logistics", "Utilities"]

_G: dict = {}


def _init(cuad_txt: str | None, seed: int) -> None:
    from faker import Faker

    fk = Faker("en_US")
    _G["fk"] = fk
    _G["seed"] = seed
    sents: list[str] = []
    if cuad_txt:
        files = sorted(Path(cuad_txt).glob("*.txt"))[:60]
        for f in files:
            t = f.read_text(errors="ignore")
            for s in re.split(r"(?<=[.;])\s+", re.sub(r"\s+", " ", t)):
                if 60 <= len(s) <= 400:
                    sents.append(s)
    _G["sents"] = sents


def _rng(seed: int, idx: int, salt: str) -> random.Random:
    return random.Random(f"{seed}:{salt}:{idx}")


def _para(rng: random.Random, fk, idx: int, legal_bias: float) -> str:
    sents = _G["sents"]
    n = rng.randint(2, 7)
    out = []
    for _ in range(n):
        if sents and rng.random() < legal_bias:
            out.append(rng.choice(sents))
        else:
            out.append(fk.sentence(nb_words=rng.randint(8, 22)))
    if rng.random() < 0.4:
        out.append(f"Reference {fk.bothify('??-####').upper()}; owner {fk.name()}; budget ${rng.randint(1, 900) * 1000:,}.")
    return " ".join(out)


# ---- docx ----------------------------------------------------------------

def make_docx(path: str, idx: int, seed: int, variant: int) -> None:
    from docx import Document

    fk = _G["fk"]
    rng = _rng(seed, idx, "docx")
    fk.seed_instance(seed * 100003 + idx)
    kind = DOC_KINDS[idx % len(DOC_KINDS)]
    company = fk.company()
    d = Document()
    d.add_heading(f"{kind.title()}: {fk.catch_phrase()} ({company})", 0)
    d.add_paragraph(f"Prepared by {fk.name()} on {fk.date_between('-6y', 'today')}. Project {fk.bs()}.")
    n_sections = rng.choice([2, 3, 4, 6, 10, 18, 30])
    legal_bias = 0.7 if kind in ("agreement", "policy") else 0.15
    vrng = _rng(seed, idx, f"var{variant}")
    for s in range(n_sections):
        d.add_heading(f"{s + 1}. {fk.bs().title()}", 1)
        for _ in range(rng.randint(1, 5)):
            txt = _para(rng, fk, idx, legal_bias)
            if variant and vrng.random() < 0.15:  # near-duplicate edit
                txt = txt.replace("the", "the revised", 1) + f" Amended in v{variant}."
            d.add_paragraph(txt)
        if rng.random() < 0.25:
            t = d.add_table(rows=1, cols=4)
            for i, h in enumerate(["Item", "Owner", "Due", "Amount"]):
                t.rows[0].cells[i].text = h
            for _ in range(rng.randint(2, 12)):
                r = t.add_row().cells
                r[0].text, r[1].text, r[2].text, r[3].text = fk.bs(), fk.name(), str(fk.date_between("-1y", "+1y")), f"${rng.randint(100, 99999):,}"
    d.save(path)


# ---- xlsx ----------------------------------------------------------------

def make_xlsx(path: str, idx: int, seed: int, variant: int) -> None:
    from openpyxl import Workbook

    fk = _G["fk"]
    rng = _rng(seed, idx, "xlsx")
    fk.seed_instance(seed * 100003 + idx + 5_000_000)
    kind = SHEET_KINDS[idx % len(SHEET_KINDS)]
    wb = Workbook(write_only=True)
    n_sheets = rng.choice([1, 1, 2, 3, 4, 6, 8])
    heavy = rng.random() < 0.25  # heavy-tailed sizes
    vrng = _rng(seed, idx, f"var{variant}")
    vendors = [fk.company() for _ in range(25)]
    people = [fk.name() for _ in range(15)]
    for s in range(n_sheets):
        ws = wb.create_sheet(f"{kind}_{s + 1}_{fk.word()}"[:31])
        rows = rng.randint(3000, 20000) if heavy else rng.randint(30, 900)
        ws.append(["ID", "Date", "Vendor", "Category", "Owner", "Amount", "Qty", "Notes"])
        base = fk.date_between("-4y", "-1y")
        for r in range(rows):
            amt = round(rng.uniform(5, 50000), 2)
            if variant and vrng.random() < 0.01:
                amt = round(amt * 1.1, 2)
            ws.append([f"{kind[:3].upper()}-{idx}-{s}-{r}", str(base), rng.choice(vendors), rng.choice(CATEGORIES),
                       rng.choice(people), amt, rng.randint(1, 500),
                       fk.sentence(nb_words=rng.randint(4, 16)) if rng.random() < 0.6 else ""])
    wb.save(path)


# ---- pptx ----------------------------------------------------------------

def make_pptx(path: str, idx: int, seed: int, variant: int) -> None:
    from pptx import Presentation

    fk = _G["fk"]
    rng = _rng(seed, idx, "pptx")
    fk.seed_instance(seed * 100003 + idx + 9_000_000)
    prs = Presentation()
    vrng = _rng(seed, idx, f"var{variant}")
    for s in range(rng.choice([5, 8, 12, 20, 40])):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = fk.catch_phrase() if s else f"{fk.company()} - {fk.bs().title()}"
        tf = slide.placeholders[1].text_frame
        tf.text = fk.sentence(nb_words=10)
        for _ in range(rng.randint(2, 6)):
            txt = fk.sentence(nb_words=rng.randint(6, 16))
            if variant and vrng.random() < 0.15:
                txt += f" (updated v{variant})"
            tf.add_paragraph().text = txt
        if rng.random() < 0.5:
            slide.notes_slide.notes_text_frame.text = " ".join(fk.sentence() for _ in range(rng.randint(1, 4)))
    prs.save(path)


MAKERS = {"docx": make_docx, "xlsx": make_xlsx, "pptx": make_pptx}


def _job(args):
    kind, path, idx, seed, variant = args
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    MAKERS[kind](path, idx, seed, variant)
    return path


def plan(out: Path, counts: dict[str, int], seed: int):
    """Return (jobs, dup_copies, manifest). Deterministic."""
    rng = random.Random(seed)
    jobs, dups, manifest = [], [], {}
    for kind, n in counts.items():
        n_base = int(n * 0.90)
        n_family = int(n * 0.05)
        n_dup = n - n_base - n_family
        for i in range(n_base):
            dept = DEPTS[(i * 7 + seed) % len(DEPTS)]
            year = 2019 + (i % 7)
            proj = f"project-{(i // 11) % 40:02d}"
            rel = f"{dept}/{year}/{proj}/{kind}_{i:05d}.{kind}"
            jobs.append((kind, str(out / rel), i, seed, 0))
            manifest[rel] = {"kind": kind, "idx": i, "variant": 0}
        for j in range(n_family):  # version families: v1, final share an idx with base file idx (=> near-dup)
            src = rng.randrange(n_base)
            dept = DEPTS[(src * 7 + seed) % len(DEPTS)]
            year = 2019 + (src % 7)
            proj = f"project-{(src // 11) % 40:02d}"
            tag = rng.choice(["v2", "final", "FINAL_revised", "v3_edits"])
            rel = f"{dept}/{year}/{proj}/{kind}_{src:05d}_{tag}.{kind}"
            jobs.append((kind, str(out / rel), src, seed, 1 + (j % 3)))
            manifest[rel] = {"kind": kind, "idx": src, "variant": 1 + (j % 3), "near_dup_of": f"{kind}_{src:05d}"}
        for j in range(n_dup):
            src = rng.randrange(n_base)
            dept = DEPTS[(src * 7 + seed) % len(DEPTS)]
            year = 2019 + (src % 7)
            proj = f"project-{(src // 11) % 40:02d}"
            srel = f"{dept}/{year}/{proj}/{kind}_{src:05d}.{kind}"
            drel = f"{DEPTS[(src + 3) % len(DEPTS)]}/archive/copy_of_{kind}_{src:05d}.{kind}"
            dups.append((str(out / srel), str(out / drel)))
            manifest[drel] = {"kind": kind, "dup_of": srel}
    return jobs, dups, manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--docx", type=int, default=3000)
    ap.add_argument("--xlsx", type=int, default=1500)
    ap.add_argument("--pptx", type=int, default=500)
    ap.add_argument("--cuad-txt", type=str, default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--procs", type=int, default=16)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    jobs, dups, manifest = plan(a.out, {"docx": a.docx, "xlsx": a.xlsx, "pptx": a.pptx}, a.seed)
    with Pool(a.procs, initializer=_init, initargs=(a.cuad_txt, a.seed)) as pool:
        for i, _ in enumerate(pool.imap_unordered(_job, jobs, chunksize=8)):
            if i % 500 == 0:
                print(f"generated {i}/{len(jobs)}", flush=True)
    import shutil

    for s, d in dups:
        Path(d).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(s, d)
    (a.out.parent / (a.out.name + ".manifest.json")).write_text(json.dumps(manifest, indent=0))
    print(f"done: {len(jobs)} generated + {len(dups)} exact duplicates")


if __name__ == "__main__":
    sys.exit(main())
