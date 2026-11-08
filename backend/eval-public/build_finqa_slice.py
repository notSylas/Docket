"""Deterministically build the FinQA numeric-reasoning slice: corpus + gold-finqa.yaml.

Source: FinQA (Chen et al., EMNLP 2021), test split, MIT licence
(https://github.com/czyssrs/FinQA). Each example is one page of an S&P 500
company's annual report (pre-text, a table, post-text) with a numeric question.
The raw JSON is NOT committed: put test.json in eval-public/external/finqa/raw/
(or set FINQA_RAW). See eval-public/external/README.

For each selected page this writes eval-public/external/finqa/corpus/<id>/ with
  page.docx   pre-text paragraphs, a real Word table (table_ori), post-text paragraphs
  table.xlsx  sheet "Table" (numbers as numeric cells) + sheet "Notes" with the
              source page and any unit/period phrase found in the table header
Question text is the dataset's. The expected number comes from the dataset's
gold answer / executed program (`exe_ans`), never typed. Gold spans are
verbatim substrings of the TEXT THE INDEXER STORES: after building, the corpus
is ingested (fake embeddings, temp data dir) with the backend venv and every
span must occur inside a single chunk, else the question is dropped. Unanswerable
questions ask for a table row's metric in a fiscal year that appears nowhere in
that page.

Needs python-docx, openpyxl, pyyaml (scratch venv; not backend/.venv). The
chunk verification step shells out to backend/.venv/bin/python (DOCKET_PY).

    <venv>/bin/python eval-public/build_finqa_slice.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml
from docx import Document
from openpyxl import Workbook
from openpyxl.styles import Font

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_gold_g2 import assign_splits  # noqa: E402
from build_synthetic_corpus_g1 import num_re, rnd  # noqa: E402

EXT = HERE / "external" / "finqa"
RAW = Path(os.environ.get("FINQA_RAW", EXT / "raw" / "test.json"))
CORPUS = EXT / "corpus"
GOLD = HERE / "gold-finqa.yaml"
DOCKET_PY = Path(os.environ.get("DOCKET_PY", HERE.parent / ".venv" / "bin" / "python"))
N_FILINGS = 22
MAX_PER_FILING = 2
MAX_QUESTIONS = 30
N_UNANSWERABLE = 4
FIXED_YEAR_GAP = 4  # unanswerable: latest year in the page + this

DUMP_SNIPPET = r'''
import sys, json, hashlib
from pathlib import Path
from docket.eval.runner import EvalRunner
class Fake:
    def embed(self, text):
        return [b / 255 for b in hashlib.sha256(text.encode()).digest()[:16]]
    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]
    def generate(self, **k):
        raise RuntimeError("no generation in chunk dump")
with EvalRunner(Path(sys.argv[1]), gateway=Fake()) as r:
    r.ingest()
    ch = r.corpus_chunks()
    json.dump({"failures": r.ingest_failures, "chunks": [
        {"source": c.source_name, "file": c.source_document, "text": c.text} for c in ch]}, open(sys.argv[2], "w"))
'''


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def parse_num(s: str) -> float | None:
    t = s.strip().replace(",", "").replace("$", "").replace("%", "").strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    if t.startswith("-"):
        neg, t = True, t[1:].strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def eligible(x: dict) -> bool:
    qa = x["qa"]
    if len(qa["steps"]) > 2 or len(qa["question"]) < 30:
        return False
    if "const_" in qa["program"] or qa.get("exe_ans") in (None, "") or isinstance(qa["exe_ans"], str):
        return False
    ans = str(qa["answer"])
    if parse_num(ans) is None or abs(parse_num(ans)) > 1e5:
        return False
    # scale-conversion questions need outside knowledge of the unit
    if re.search(r"\b(in|per) (thousands?|millions?|billions?)\b|\bwhat is that in\b", qa["question"].lower()):
        return False
    if len(x["table_ori"]) < 4 or len(x["pre_text"]) + len(x["post_text"]) < 2:
        return False
    return True


def select(data: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for x in data:
        by[x["id"].rsplit("-", 1)[0]].append(x)
    chosen: dict[str, list[dict]] = {}
    total = 0
    for fn in sorted(by, key=lambda f: hashlib.sha256(f.encode()).hexdigest()):
        qs = [x for x in by[fn] if eligible(x)]
        if not qs or fn.split("/")[0] in {k.split("/")[0] for k in chosen}:  # one filing per company
            continue
        qs = sorted(qs, key=lambda x: x["id"])[:MAX_PER_FILING]
        chosen[fn] = qs
        total += len(qs)
        if len(chosen) >= N_FILINGS or total >= MAX_QUESTIONS + N_UNANSWERABLE:
            break
    return chosen


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------

def slug(fn: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", fn.replace(".pdf", "")).strip("-")


def unit_note(table: list[list[str]]) -> str:
    hdr = " ".join(table[0]) + " " + " ".join(r[0] for r in table[1:3])
    m = re.findall(r"\(\s*(?:in|\$ in|dollars in|amounts in)[^)]*\)", hdr, flags=re.I)
    return "; ".join(sorted(set(m)))


def build_docs(fn: str, x: dict) -> None:
    ticker, year, page = fn.replace(".pdf", "").split("/")
    out = CORPUS / slug(fn)
    out.mkdir(parents=True, exist_ok=True)
    title = f"{ticker} annual report {year}, {page.replace('_', ' ')} (excerpt)"
    doc = Document()
    doc.core_properties.author = "FinQA excerpt"
    doc.add_heading(title, level=1)
    for p in x["pre_text"]:
        doc.add_paragraph(p)
    rows = x["table_ori"]
    width = max(len(r) for r in rows)
    t = doc.add_table(rows=len(rows), cols=width)
    t.style = "Table Grid"
    for i, r in enumerate(rows):
        for j in range(width):
            t.cell(i, j).text = r[j] if j < len(r) else ""
    for p in x["post_text"]:
        doc.add_paragraph(p)
    doc.save(out / "page.docx")

    wb = Workbook()
    ws = wb.active
    ws.title = "Table"
    for r in rows:
        ws.append([parse_num(c) if parse_num(c) is not None and re.fullmatch(r"[\s$(),.\-\d]+", c) else c for c in r])
    for c in ws[1]:
        c.font = Font(bold=True)
    notes = wb.create_sheet("Notes")
    notes.append(["Item", "Value"])
    notes.append(["Source", f"{ticker} annual report {year}, {page.replace('_', ' ')}"])
    un = unit_note(rows)
    notes.append(["Units and period", un or "as stated in the row and column labels"])
    wb.save(out / "table.xlsx")


def dump_chunks(corpus: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        outp = Path(td) / "chunks.json"
        res = subprocess.run([str(DOCKET_PY), "-c", DUMP_SNIPPET, str(corpus), str(outp)],
                             capture_output=True, text=True)
        if res.returncode:
            raise SystemExit(res.stderr[-2000:])
        d = json.loads(outp.read_text())
    if d["failures"]:
        raise SystemExit(f"ingest failures: {d['failures']}")
    return d["chunks"]


# --------------------------------------------------------------------------
# Gold
# --------------------------------------------------------------------------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def answer_pattern(qa: dict) -> list[str]:
    ans = str(qa["answer"]).strip()
    v = abs(float(qa["exe_ans"]))
    is_pct = ans.endswith("%")
    dec = len(ans.rstrip("%").split(".")[1]) if "." in ans else 0
    shown = abs(parse_num(ans))
    alts: set[str] = set()
    if is_pct:  # exe_ans is a fraction; the answer string is the percentage
        pv = shown
        for d in {dec, dec + 1, dec + 2}:
            alts.add(num_re(rnd(pv if d == dec else v * 100, d), d))
        alts.add(num_re(int(abs(v * 100) * 10**dec) / 10**dec, dec))  # truncated
        for d in {dec + 2, dec + 3, 4}:
            alts.add(num_re(rnd(v, d), d))  # fraction form 0.479
    else:
        alts.add(num_re(shown, dec))
        for d in {dec + 1, dec + 2}:
            alts.add(num_re(rnd(v, d), d))
        alts.add(num_re(int(v * 10**dec) / 10**dec, dec))
    return ["re:" + "|".join(sorted(alts))]


def candidate_spans(x: dict) -> list[tuple[str, str]]:
    """(number-string, label) pairs the question depends on, from the program arguments
    and the annotated rows. Spans are later cut from the chunk text around the number."""
    qa = x["qa"]
    return [(s["arg1"], "") for s in qa["steps"]] + [(s["arg2"], "") for s in qa["steps"]]


def spans_for(x: dict, chunks: list[str]) -> list[str] | None:
    """Pick verbatim 20-60 char spans from single chunks that contain each literal
    program argument next to its row label / sentence. Returns None if unverifiable."""
    qa = x["qa"]
    args: list[str] = []
    for s in qa["steps"]:
        for a in (s["arg1"], s["arg2"]):
            if not a.startswith("#") and not a.startswith("const_") and a not in args:
                args.append(a)
    if not args:
        return None
    spans: list[str] = []
    sentences = x["pre_text"] + x["post_text"]
    table = x["table_ori"]
    inds = list(qa.get("gold_inds", {}))
    for a in args:
        want = float(a.rstrip("%").replace(",", ""))
        span = None
        order = inds + [k for k in [f"table_{i}" for i in range(len(table))] + [f"text_{i}" for i in range(len(sentences))]
                        if k not in inds]
        for key in order:  # annotated evidence first, then any row / sentence
            kind, _, idx = key.partition("_")
            if not idx.isdigit():
                continue
            i = int(idx)
            if kind == "table" and i < len(table):
                cells = [c.strip() for c in table[i]]
                if any(re.fullmatch(r"[\s$(),.\-\d%]+", c) and parse_num(c) is not None and abs(parse_num(c) - want) < 1e-9
                       for c in cells[1:]):
                    span = row_span(cells, a, chunks)
            elif kind == "text" and i < len(sentences):
                m = re.search(rf"(?<![\d.]){re.escape(a)}(?![\d])", sentences[i])
                if m:
                    span = text_span(sentences[i], m.start(), m.end(), chunks)
            if span:
                break
        if not span:
            return None  # every program argument must be evidenced
        if span not in spans:
            spans.append(span)
    return spans[:3] or None


def in_one_chunk(span: str, chunks: list[str]) -> bool:
    n = norm(span)
    return any(n in c for c in chunks)


def text_span(p: str, start: int, end: int, chunks: list[str]) -> str | None:
    for width in (60, 50, 40, 30, 24):
        lo = max(0, min(start - (width - (end - start)) // 2, len(p) - width))
        seg = p[lo : lo + width].strip()
        if len(seg) >= 20 and p[start:end] in seg and in_one_chunk(seg, chunks):
            return seg
    return None


def row_span(cells: list[str], a: str, chunks: list[str]) -> str | None:
    """Find, inside a chunk, the 20-60 char window around the row label and the value."""
    label = cells[0]
    for c in chunks:
        i = c.find(norm(label)) if label else -1
        if i < 0:
            continue
        j = c.find(norm(a.replace(",", "")), i) if a else -1
        if j < 0:
            j = c.find(norm(a), i)
        if j < 0:
            continue
        # window from label start to just after the value
        end = j + len(a)
        seg = c[i:end]
        if 20 <= len(seg) <= 60:
            return seg
        if len(seg) > 60:
            seg = c[max(i, end - 60) : end]
            if len(seg) >= 20:
                return seg
    return None


def main() -> None:
    if not RAW.exists():
        raise SystemExit(f"missing {RAW}: download test.json from github.com/czyssrs/FinQA/dataset")
    data = json.loads(RAW.read_text())
    chosen = select(data)
    CORPUS.mkdir(parents=True, exist_ok=True)
    for fn, qs in chosen.items():
        build_docs(fn, qs[0])
    chunks_raw = dump_chunks(CORPUS)
    by_file: dict[str, list[str]] = defaultdict(list)
    for c in chunks_raw:
        by_file[c["source"]].append(norm(c["text"]))
    Path(os.environ.get("FINQA_DEBUG_CHUNKS", os.devnull)).write_text(json.dumps(chunks_raw))
    questions: list[dict] = []
    dropped: list[str] = []
    for fn, qs in chosen.items():
        src = slug(fn)
        for x in qs:
            qa = x["qa"]
            spans = spans_for(x, by_file[src])
            if not spans:
                dropped.append(x["id"])
                continue
            questions.append({
                "id": "finqa-" + re.sub(r"[^A-Za-z0-9]+", "-", x["id"]).strip("-"),
                "type": "numeric",
                "question": qa["question"].strip(),
                "answerable": True,
                "must_contain": answer_pattern(qa),
                "gold_spans": spans,
                "reviewed": True,
                "origin": f"FinQA test {x['id']}; program {qa['program']}; dataset answer {qa['answer']}; "
                          f"eval-public/build_finqa_slice.py",
                "source_documents": [f"{src}/page.docx", f"{src}/table.xlsx"],
                "formula_dependent": False,
            })
    questions = questions[:MAX_QUESTIONS]
    # unanswerable: a table row's metric in a year absent from the page
    used = {q["source_documents"][0].split("/")[0] for q in questions}
    unans = 0
    for fn, qs in chosen.items():
        if unans >= N_UNANSWERABLE:
            break
        src = slug(fn)
        if src not in used:
            continue
        x = qs[0]
        alltext = " ".join(x["pre_text"] + x["post_text"] + [" ".join(r) for r in x["table_ori"]])
        years = [int(y) for y in re.findall(r"\b(19\d\d|20[0-3]\d)\b", alltext)]
        if not years:
            continue
        y = max(years) + FIXED_YEAR_GAP
        labels = [r[0] for r in x["table_ori"][1:] if re.fullmatch(r"[A-Za-z][A-Za-z&,\-()]+( [A-Za-z&,\-()]+){1,5}", r[0] or "")]
        if not labels or str(y) in alltext:
            continue
        ticker = fn.split("/")[0]
        questions.append({
            "id": f"finqa-unans-{src}",
            "type": "out_of_corpus",
            "question": f"What was {ticker}'s {labels[0].lower()} for fiscal {y}?",
            "answerable": False,
            "reviewed": True,
            "origin": f"constructed: row label '{labels[0]}' of {fn} asked for fiscal {y}, a year absent from the page; "
                      f"eval-public/build_finqa_slice.py",
            "source_documents": [],
            "formula_dependent": False,
        })
        unans += 1
    for q in questions:
        q["split"] = "dev"
    header = (
        "# FinQA numeric-reasoning slice over eval-public/external/finqa/corpus (not committed; rebuild with\n"
        "# eval-public/build_finqa_slice.py). Source: FinQA test split, MIT licence, github.com/czyssrs/FinQA.\n"
        "# GENERATED: expected numbers come from the dataset's executed program, spans are verified single-chunk.\n"
        "# PUBLIC DATA, not a milestone population. Do not edit by hand.\n"
    )
    GOLD.write_text(header + yaml.safe_dump({"version": 1, "population": "public_finqa", "questions": questions},
                                            sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    n_ans = sum(q["answerable"] for q in questions)
    print(f"{GOLD.name}: {n_ans} answerable + {len(questions) - n_ans} unanswerable; "
          f"{len(chosen)} filings; dropped (unverifiable spans): {len(dropped)} {dropped}")


if __name__ == "__main__":
    main()
