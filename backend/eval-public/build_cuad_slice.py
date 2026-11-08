"""Build a small contract Q&A eval slice from CUAD (real commercial contracts).

DATA ATTRIBUTION (CC BY 4.0): the Contract Understanding Atticus Dataset (CUAD) v1,
The Atticus Project, Inc. (https://www.atticusprojectai.org/cuad), Hendrycks, Burns,
Chen, Ball, "CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review",
NeurIPS 2021 Datasets and Benchmarks (arXiv:2103.06268). Zenodo record 4595826,
licensed Creative Commons Attribution 4.0 International. gold-cuad.yaml contains only
short clause excerpts (<= 60 chars) and values derived from the CUAD annotations; the
contract PDFs themselves are NOT committed (eval-public/external/ is git-ignored).

Everything is derived from the annotations in master_clauses.csv: expected values come
from the annotated answer/clause text, gold spans are short verbatim substrings of the
annotated clause text that were checked to sit inside ONE indexed chunk of the right
contract (and no other contract), unanswerable questions use categories whose annotation
is empty AND whose keywords do not occur anywhere in the indexed text. Nothing is typed
by hand except the list of 12 contracts to use.

Run with the backend venv (needs docket, pyyaml):

    backend/.venv/bin/python backend/eval-public/build_cuad_slice.py \
        --cuad <dir containing CUAD_v1/> [--chunks <cache.json>]

It copies the 12 PDFs to eval-public/external/cuad/corpus/contracts/, ingests them once
(Docling + embeddings, temp dir; or reuses --chunks) and writes eval-public/gold-cuad.yaml.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

from docket.eval.scoring import normalize_text

HERE = Path(__file__).resolve().parent
SUBDIR = "contracts"

# The only hand-picked input: filename prefixes (unique in master_clauses.csv). Varied
# type; first four are 40+ pages.
SELECTED = [
    "ZogenixInc_20190509_10-Q_EX-10",
    "BloomEnergyCorp_20180321_DRSA",
    "CytodynInc_20200109_10-Q_EX-10",
    "PfHospitalityGroupInc_20150923_10-12G_EX-10.1_9266710_EX-10.1_Franchise Agreement1",
    "QuantumGroupIncFl_20090120_8-K",
    "TomOnlineInc_20060501_20-F_EX-",
    "WestPharmaceuticalServicesInc_",
    "PapaJohnsInternationalInc_2019",
    "ReedsInc_20191113_10-Q_EX-10.4",
    "NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBUTOR AGREEMENT",
    "LinkPlusCorp_20050802_8-K_EX-1",
    "GopageCorp_20140221_10-K_EX-10",
]

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
            "forty-five": 45, "sixty": 60, "ninety": 90, "one hundred twenty": 120, "one hundred eighty": 180}
WORD_OF = {v: k for k, v in NUMWORDS.items()}
ROLE_WORDS = {"company", "licensor", "licensee", "distributor", "customer", "party", "parties", "you", "we", "us",
              "our", "supplier", "client", "seller", "buyer", "vendor", "provider", "affiliate", "network", "agent",
              "franchisor", "franchisee", "sponsor", "talent", "host", "the", "manufacturer", "owner"}
SUFFIX_RE = re.compile(r",?\s+(?:inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|plc|s\.a|n\.a|lp|l\.p)\.?$", re.I)

CATEGORY_KEYWORDS = {  # absent from all indexed text => annotation emptiness is trustworthy
    "Insurance": ["insurance", "insured"],
    "Audit Rights": ["audit", "inspect"],
    "Non-Compete": ["compet"],
    "Exclusivity": ["exclusiv", "sole supplier", "sole and exclusive"],
    "Termination For Convenience": ["convenience", "without cause", "for any reason"],
    "Cap On Liability": ["limitation of liab", "aggregate liab", "in no event", "shall not exceed", "limit of liab"],
    "Anti-Assignment": ["assign"],
}
UNANSWERABLE_Q = {
    "Insurance": "What minimum amount of insurance coverage must a party maintain under the {h}?",
    "Audit Rights": "How often may a party audit the other party's books and records under the {h}?",
    "Non-Compete": "For how long is a party prohibited from competing with the other party under the {h}?",
    "Exclusivity": "In which territory is exclusivity granted under the {h}?",
    "Termination For Convenience": "How many days' notice is needed to terminate the {h} for convenience?",
    "Cap On Liability": "What is the maximum aggregate liability of a party under the {h}?",
}
YESNO_Q = {
    "Exclusivity": "Does the {h} contain an exclusivity or exclusive-dealing commitment? Quote the clause.",
    "Non-Compete": "Does the {h} restrict a party from competing with the other party? Quote the clause.",
    "Anti-Assignment": "Does the {h} restrict a party from assigning the agreement? Quote the clause.",
    "Audit Rights": "Does the {h} give a party a right to audit the other party's records? Quote the clause.",
    "Insurance": "Does the {h} require a party to maintain insurance? Quote the clause.",
    "Cap On Liability": "Does the {h} cap or limit a party's liability? Quote the clause.",
}
CROSS_DESC = {
    "Exclusivity": "an exclusivity or exclusive-dealing clause",
    "Non-Compete": "a non-compete clause",
    "Audit Rights": "an audit-rights clause",
    "Insurance": "an insurance requirement",
    "Cap On Liability": "a cap or limitation on liability",
    "Termination For Convenience": "a right to terminate for convenience",
    "Anti-Assignment": "an anti-assignment clause",
}
# Quotas for single-contract answerable questions, then unanswerable and cross-contract totals.
QUOTA = [("Governing Law", 4), ("Parties", 2), ("Effective Date", 2), ("Agreement Date", 2), ("Expiration Date", 2),
         ("Renewal Term", 2), ("Termination For Convenience", 3), ("Cap On Liability", 3),
         ("Exclusivity", 2), ("Anti-Assignment", 2), ("Non-Compete", 1), ("Audit Rights", 1), ("Insurance", 1)]
N_UNANSWERABLE = 6
N_CROSS = 6


YESNO_KEYWORD = {  # a model that quotes the clause (as asked) instead of saying "yes" must still pass
    "Exclusivity": r"exclusive|exclusivity",
    "Non-Compete": r"compet",
    "Anti-Assignment": r"assign",
    "Audit Rights": r"audit",
    "Insurance": r"insurance",
    "Cap On Liability": r"liabilit",
}
# Per-contract extra phrase from the annotated clause text (the annotation is the ground truth).
YESNO_EXTRA = {
    "PapaJohnsInternationalInc": r"prohibited\s+from\s+granting",  # exclusivity expressed as a prohibition
    "ReedsInc": r"exclusive\s+right",
}


NEG_PATTERN = (r"re:^\W*no\b|\bdoes\s+not\s+(?:contain|restrict|require|give|cap|limit|appear)"
               r"|\bno\s+(?:such\s+)?(?:clause|provision|exclusivity|restriction|requirement)")


def yesno_pattern(cat: str, span: str, fn: str) -> str:
    """Accept yes / applies / quoting the annotated clause (span or topic keyword near a quote)."""
    alts = [r"\byes\b", r"\bapplies\b", r"\bdoes\s+(?:contain|restrict|require|give|cap|limit)\b"]
    alts.append(r"\s+".join(re.escape(w) for w in normalize_text(span).split()))
    alts.append(YESNO_KEYWORD[cat])
    for k, v in YESNO_EXTRA.items():
        if fn.startswith(k):
            alts.append(v)
    return "re:" + "|".join(alts)


def all_party_pattern(row_parties: list[str]) -> str | None:
    """One regex accepting ANY party CUAD lists for the contract."""
    alts = []
    for p in row_parties:
        pt = party_pattern(p)
        if pt:
            alts.append(pt[3:])
    return "re:" + "|".join(dict.fromkeys(alts)) if alts else None


def squeeze(s: str) -> str:
    return " ".join(s.split())


def clauses(row: dict, cat: str) -> list[str]:
    try:
        v = ast.literal_eval(row[cat])
    except (ValueError, SyntaxError):
        return []
    return [squeeze(x) for x in v if squeeze(x)]


def clean_party(raw: str) -> tuple[str, str | None]:
    """'Foo Inc. ("Foo")' -> ('Foo Inc.', 'Foo')."""
    alias = re.search(r"[(“\"]+\s*[\"“]?([^\"”)]{2,30})[\"”]", raw)
    name = re.split(r"\s*[(“\"]", raw, maxsplit=1)[0]
    name = squeeze(name).strip(" ,;")
    return name, (alias.group(1).strip() if alias else None)


def party_pattern(raw: str) -> str | None:
    name, alias = clean_party(raw)
    core = SUFFIX_RE.sub("", name).strip(" ,.")
    forms = [core] if len(core) >= 3 else []
    if alias and len(alias) >= 3 and alias.lower() not in ROLE_WORDS:
        forms.append(alias)
    forms = [f.lower() for f in forms if f.lower() not in ROLE_WORDS]
    if not forms:
        return None
    return "re:" + "|".join(r"\s+".join(re.escape(w) for w in f.split()) for f in dict.fromkeys(forms))


def parties(row: dict) -> list[str]:
    out = []
    for p in row["Parties-Answer"].split(";"):
        p = p.strip()
        name, alias = clean_party(p)
        if name and (alias or " " in name or name.lower() not in ROLE_WORDS) and party_pattern(p):
            out.append(p)
    return out


def title_of(row: dict) -> str:
    t = squeeze(row["Document Name-Answer"])
    t = re.split(r",\s*dated|\s+dated\b|\s*\(", t, maxsplit=1)[0].strip()
    return t.title() if t.isupper() else t


def handle(row: dict) -> str:
    p1 = clean_party(parties(row)[0])[0]
    return f"{title_of(row)} with {p1}"


def windows(text: str):
    tokens = text.split()
    for i in range(len(tokens)):
        w = ""
        for t in tokens[i:]:
            nxt = f"{w} {t}".strip()
            if len(nxt) > 60:
                break
            w = nxt
        if len(w) >= 20:
            yield w


def find_span(clause_list: list[str], doc: str, chunks: dict[str, list[str]]) -> str | None:
    """First short verbatim window of an annotated clause that is inside a single chunk of
    `doc` and inside no chunk of any other contract."""
    own = chunks[doc]
    others = [c for d, cs in chunks.items() if d != doc for c in cs]
    for clause in clause_list:
        for w in windows(clause):
            if not re.search(r"[A-Za-z]{4}", w):
                continue
            n = normalize_text(w)
            if any(n in c for c in own) and not any(n in c for c in others):
                return w
    return None


def date_patterns(answer: str, clause_list: list[str]) -> list[str] | None:
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", answer.strip())
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    mname = "|".join(MONTHS)
    text = " ".join(clause_list)
    found = []
    for rx, order in [
        (rf"\b({mname})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", "mdy"),
        (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?({mname})[a-z]*\.?,?\s+(\d{{4}})", "dmy"),
    ]:
        for mm in re.finditer(rx, text, re.I):
            g = mm.groups()
            if order == "mdy":
                found.append((MONTHS.index(g[0].lower()) + 1, int(g[1]), int(g[2])))
            else:
                found.append((MONTHS.index(g[1].lower()) + 1, int(g[0]), int(g[2])))
    ok = [f for f in found if f[0] == mo and f[1] == d and (f[2] == y or f[2] % 100 == y % 100)]
    if not ok:
        return None
    mo, d, y = ok[0]
    return [rf"(?<!\d){y}(?!\d)", rf"(?<!\d)0?{d}(?:st|nd|rd|th)?(?!\d)",
            rf"{MONTHS[mo - 1]}|{MONTHS[mo - 1][:3]}\b|(?<!\d)0?{mo}[/.-]"]


def number_pattern(n: int, unit: str) -> str:
    alts = [str(n)] + ([WORD_OF[n]] if n in WORD_OF else [])
    return rf"\b(?:{'|'.join(alts)})\b[^.]{{0,25}}?\b{unit}s?\b"


def duration_from(answer: str) -> tuple[int, str] | None:
    m = re.fullmatch(r"(\d+)\s*(year|month|day)s?", answer.strip(), re.I)
    return (int(m.group(1)), m.group(2).lower()) if m else None


def notice_from_clause(clause_list: list[str]) -> tuple[int, str] | None:
    words = "|".join(sorted(NUMWORDS, key=len, reverse=True))
    rx = rf"\b(\d+|{words})\s*(?:\(\d+\)\s*)?(?:calendar\s+|business\s+|consecutive\s+)?(day|month)s?\b"
    found = set()
    for c in clause_list:
        for m in re.finditer(rx, c, re.I):
            g = m.group(1).lower()
            found.add((int(g) if g.isdigit() else NUMWORDS[g], m.group(2).lower()))
    return next(iter(found)) if len(found) == 1 else None


def cap_amount(clause_list: list[str]) -> str | None:
    found = set()
    for c in clause_list:
        for m in re.finditer(r"\$\s?([\d,]+(?:\.\d+)?)(\s*(?:million|billion))?", c, re.I):
            num = m.group(1).replace(",", "").rstrip(".")
            if num and num != "0":
                found.add((num, (m.group(2) or "").strip().lower()))
    if len(found) != 1:
        return None
    num, scale = next(iter(found))
    if scale:
        base = num[:-2] if num.endswith(".0") else num
        return rf"\$?\s?{re.escape(base)}(?:\.0+)?\s*{scale}"
    return rf"\$?\s?{re.escape(num)}(?!\d)"


def q_dict(qid, qtype, question, answerable, must, spans, origin, docs, **extra):
    d = {"id": qid, "type": qtype, "question": question, "answerable": answerable}
    if must:
        d["must_contain"] = must
    if spans:
        d["gold_spans"] = spans
    d.update({"reviewed": True, "origin": origin, "source_documents": docs, "formula_dependent": False})
    d.update(extra)
    return d


def get_chunks(corpus: Path, cache: Path | None) -> list[dict]:
    if cache and cache.exists():
        return json.loads(cache.read_text())
    from docket.core.config import Settings
    from docket.eval.runner import EvalRunner, OllamaMetaGateway

    with EvalRunner(corpus, gateway=OllamaMetaGateway(gen_model=Settings().gen_model)) as runner:
        runner.ingest()
        if runner.ingest_failures:
            print("INGEST FAILURES:", runner.ingest_failures, file=sys.stderr)
        data = [{"source_document": c.source_document, "text": c.text} for c in runner.corpus_chunks()]
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuad", type=Path, required=True, help="dir containing CUAD_v1/")
    ap.add_argument("--chunks", type=Path, default=None, help="chunk cache json (created if missing)")
    ap.add_argument("--corpus-out", type=Path, default=HERE / "external/cuad/corpus")
    ap.add_argument("--gold-out", type=Path, default=HERE / "gold-cuad.yaml")
    args = ap.parse_args()

    root = args.cuad / "CUAD_v1"
    rows = {r["Filename"]: r for r in csv.DictReader(open(root / "master_clauses.csv", encoding="utf-8-sig"))}
    pdfs = {p.name: p for p in (root / "full_contract_pdf").rglob("*.pdf")}
    chosen = []
    for prefix in SELECTED:
        hits = [fn for fn in rows if fn.startswith(prefix) and fn in pdfs]
        assert len(hits) == 1, (prefix, hits)
        chosen.append(hits[0])

    dest = args.corpus_out / SUBDIR
    dest.mkdir(parents=True, exist_ok=True)
    for fn in chosen:
        shutil.copy2(pdfs[fn], dest / fn)

    raw_chunks = get_chunks(args.corpus_out, args.chunks)
    chunks: dict[str, list[str]] = {}
    for fn in chosen:
        chunks[fn] = [normalize_text(c["text"]) for c in raw_chunks if c["source_document"] == f"{SUBDIR}/{fn}"]
        assert chunks[fn], f"no chunks for {fn}"
    fulltext = {fn: " ".join(chunks[fn]) for fn in chosen}

    def doc_of(fn):
        return f"{SUBDIR}/{fn}"

    def slug(fn):
        return re.sub(r"[^a-z0-9]", "", fn.split("_")[0].lower())

    # ---- candidates per category -------------------------------------------------
    cands: dict[str, list[dict]] = {c: [] for c, _ in QUOTA}
    unans: dict[str, list[dict]] = {c: [] for c in UNANSWERABLE_Q}
    for fn in chosen:
        row = rows[fn]
        if not parties(row):
            continue
        h = handle(row)
        d = doc_of(fn)
        origin = f"CUAD v1 (CC BY 4.0) master_clauses.csv {fn}"
        sl = slug(fn)

        def add(cat, qid, qtype, question, must, clause_list, note, must_not=None):
            span = find_span(clause_list, fn, chunks)
            if span:
                cands[cat].append(q_dict(f"cuad-{qid}-{sl}", qtype, question, True, must, [span],
                                         f"{origin}; {note}", [d], _fn=fn,
                                         **({"must_not_contain": [must_not]} if must_not else {})))

        gl = squeeze(row["Governing Law-Answer"])
        gl = re.sub(r"^(?:the\s+)?(?:state|commonwealth)\s+of\s+", "", gl, flags=re.I)
        if gl and len(gl) <= 30 and not re.search(r"[;,/]", gl):
            add("Governing Law", "govlaw", "single_fact",
                f"Under the {h}, which jurisdiction's law governs the agreement?",
                ["re:" + r"\s+".join(re.escape(w) for w in gl.lower().split())],
                clauses(row, "Governing Law"), f"Governing Law = {gl}")
        ps = parties(row)
        if len(ps) >= 2:
            pat2 = party_pattern(ps[1])
            p1 = clean_party(ps[0])[0]
            if pat2 and pat2 != party_pattern(ps[0]):
                span = None
                for item in clauses(row, "Parties"):
                    if len(item) >= 20:
                        span = find_span([item], fn, chunks)
                        if span:
                            break
                if span:
                    cands["Parties"].append(q_dict(
                        f"cuad-parties-{sl}", "single_fact",
                        f"Who is the counterparty to {p1} in the {title_of(row)}?", True, [pat2], [span],
                        f"{origin}; Parties = {row['Parties-Answer'][:80]}", [d], _fn=fn))
        for cat, qid, text in [("Effective Date", "effdate", "What is the effective date of the {h}?"),
                               ("Agreement Date", "agrdate", "On what date was the {h} entered into or dated?"),
                               ("Expiration Date", "expdate", "When does the initial term of the {h} expire?")]:
            cl = clauses(row, cat)
            pats = date_patterns(row[f"{cat}-Answer"], cl) if cl else None
            if pats:
                add(cat, qid, "numeric", text.format(h=h), ["re:" + p for p in pats], cl,
                    f"{cat} = {row[f'{cat}-Answer']}")
        cl = clauses(row, "Renewal Term")
        dur = duration_from(row["Renewal Term-Answer"]) if cl else None
        if dur:
            add("Renewal Term", "renewal", "numeric", f"For how long does the {h} renew each time it renews?",
                ["re:" + number_pattern(*dur)], cl, f"Renewal Term = {row['Renewal Term-Answer']}")
        cl = clauses(row, "Termination For Convenience")
        nt = notice_from_clause(cl) if cl else None
        if nt:
            add("Termination For Convenience", "tfc", "numeric",
                f"How much notice is required to terminate the {h} for convenience?",
                ["re:" + number_pattern(*nt)], cl, f"TfC notice {nt[0]} {nt[1]}")
        cl = clauses(row, "Cap On Liability")
        if cl:
            amt = cap_amount(cl)
            if amt:
                add("Cap On Liability", "cap", "numeric", f"What dollar amount caps liability under the {h}?",
                    ["re:" + amt], cl, "Cap On Liability amount from annotated clause")
        for cat in ("Exclusivity", "Anti-Assignment", "Non-Compete", "Audit Rights", "Insurance"):
            cl = clauses(row, cat)
            if cl:
                span_ = find_span(cl, fn, chunks)
                if span_:
                    add(cat, cat.lower().replace(" ", "").replace("-", ""), "single_fact", YESNO_Q[cat].format(h=h),
                        [yesno_pattern(cat, span_, fn)], cl, f"{cat} annotated present", must_not=NEG_PATTERN)
        for cat, q in UNANSWERABLE_Q.items():
            if not clauses(row, cat) and not any(k in fulltext[fn] for k in CATEGORY_KEYWORDS[cat]):
                unans[cat].append(q_dict(f"cuad-none-{cat.lower().replace(' ', '').replace('-', '')}-{sl}",
                                         "single_fact", q.format(h=h), False, None, None,
                                         f"{origin}; {cat} annotation empty and keywords {CATEGORY_KEYWORDS[cat]} "
                                         "absent from indexed text", [d], _fn=fn))

    # ---- deterministic selection spread across contracts -------------------------
    used: Counter = Counter()
    order = {fn: i for i, fn in enumerate(chosen)}
    final: list[dict] = []

    def take(pool, n):
        pool = sorted(pool, key=lambda q: (used[q["_fn"]], order[q["_fn"]]))
        for q in pool[:n]:
            used[q["_fn"]] += 1
            final.append(q)

    for cat, n in QUOTA:
        take(cands[cat], n)
    ucat = list(UNANSWERABLE_Q)
    picked, i = 0, 0
    while picked < N_UNANSWERABLE and i < 50:
        pool = [q for q in unans[ucat[i % len(ucat)]] if q not in final]
        i += 1
        if pool:
            take(pool, 1)
            picked += 1

    # ---- cross-contract questions ------------------------------------------------
    cross = []
    for cat, desc in CROSS_DESC.items():
        pos, ambiguous = [], False
        for fn in chosen:
            row = rows[fn]
            if clauses(row, cat):
                pos.append(fn)
            elif any(k in fulltext[fn] for k in CATEGORY_KEYWORDS[cat]):
                ambiguous = True
        if ambiguous or not (1 <= len(pos) <= 3):
            continue
        spans, pats = [], []
        for fn in pos:
            sp = find_span(clauses(rows[fn], cat), fn, chunks)
            pt = all_party_pattern(parties(rows[fn]))
            if not (sp and pt):
                break
            spans.append(sp)
            pats.append(pt)
        else:
            cross.append((f"cuad-x-{cat.lower().replace(' ', '').replace('-', '')}",
                          f"Which of the contracts in this library contain {desc}? Name the parties.",
                          pats, spans, pos, f"{cat} annotated in {len(pos)} of {len(chosen)}; empty and keyword-free in the rest"))
    vals: dict[str, list[str]] = {}
    for fn in chosen:
        g = squeeze(rows[fn]["Governing Law-Answer"])
        g = re.sub(r"^(?:the\s+)?(?:state|commonwealth)\s+of\s+", "", g, flags=re.I)
        vals.setdefault(g, []).append(fn)
    if all(v for v in vals):
        for g, pos in sorted(vals.items(), key=lambda kv: (len(kv[1]), kv[0])):
            if not (1 <= len(pos) <= 3) or len(g) > 30 or re.search(r"[;,/]", g):
                continue
            spans, pats = [], []
            for fn in pos:
                sp = find_span(clauses(rows[fn], "Governing Law"), fn, chunks)
                pt = all_party_pattern(parties(rows[fn]))
                if not (sp and pt):
                    break
                spans.append(sp)
                pats.append(pt)
            else:
                cross.append((f"cuad-x-govlaw-{re.sub(r'[^a-z0-9]', '', g.lower())}",
                              f"Which of the contracts in this library are governed by the law of {g}? Name the parties.",
                              pats, spans, pos, f"Governing Law = {g} in {len(pos)} of {len(chosen)}"))
    # Cross-contract questions deliberately carry no source_documents (they span dev/test documents).
    for qid, text, pats, spans, pos, note in cross[:N_CROSS]:
        qtype = "multi_doc" if len(spans) >= 2 else "single_fact"
        final.append(q_dict(qid, qtype, text, True, None, spans,
                            f"CUAD v1 (CC BY 4.0) master_clauses.csv; {note}", [], enumeration=pats, split="dev"))

    for q in final:
        q.pop("_fn", None)
    gold = {"version": 1, "population": "public_real_contracts", "questions": final}
    header = (
        "# CUAD v1 contract Q&A slice (12 real commercial contracts, PDF). GENERATED by "
        "eval-public/build_cuad_slice.py;\n# do not edit by hand. Expected values and gold spans are derived from the CUAD "
        "annotations\n# (master_clauses.csv). Data: The Atticus Project, CUAD v1, CC BY 4.0 "
        "(https://doi.org/10.5281/zenodo.4595826).\n# Only short clause excerpts are quoted here; the contracts live in "
        "eval-public/external/ (git-ignored).\n"
    )
    args.gold_out.write_text(header + yaml.safe_dump(gold, sort_keys=False, allow_unicode=True, width=110))
    n_un = sum(1 for q in final if not q["answerable"])
    print(f"{len(final)} questions: {len(final) - n_un} answerable, {n_un} unanswerable, "
          f"{sum(1 for q in final if not q['source_documents'])} cross-contract")
    print("per-category single candidates:", {c: len(v) for c, v in cands.items()})
    print("unanswerable candidates:", {c: len(v) for c, v in unans.items()})


if __name__ == "__main__":
    main()
