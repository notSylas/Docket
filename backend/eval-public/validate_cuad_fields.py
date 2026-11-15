"""CPU-only validation of the deterministic contract-field extractors against
the CUAD v1 annotations (master_clauses.csv, CC BY 4.0, The Atticus Project).

    backend/.venv/bin/python backend/eval-public/validate_cuad_fields.py \
        --cuad <dir containing CUAD_v1/> [--limit N] [--min-conf 0.8] [--json out.json]

Contracts are the CUAD full-text files, chunked with the project's own chunker
(same recipe as ingestion) so clause-boundary effects are realistic. No model.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from docket.core.config import settings
from docket.infra.parsing.chunker import chunk_document
from docket.infra.parsing.recipes import docling_recipe
from docket.services.documents import classify
from docket.services.documents.fields import ChunkText, extract_all, parse_duration

PRESENCE = {
    "exclusivity": "Exclusivity-Answer", "non_compete": "Non-Compete-Answer",
    "anti_assignment": "Anti-Assignment-Answer", "audit_rights": "Audit Rights-Answer",
    "insurance": "Insurance-Answer", "liability_cap": "Cap On Liability-Answer",
}


def gold_date(s: str) -> str | None:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        y = int(m.group(3))
        y += 2000 if y < 50 else 1900 if y < 100 else 0
        try:
            return date(y, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    ds = classify.find_dates(s)
    return ds[0][0] if ds else None


def norm_law(s: str) -> str:
    s = re.sub(r"\b(state|commonwealth|province|of|the|laws?)\b", " ", s.lower())
    return re.sub(r"[^a-z]+", " ", s).strip()


def name_tokens(s: str) -> set[str]:
    stop = {"inc", "llc", "ltd", "corp", "corporation", "company", "co", "the", "of", "and", "limited", "incorporated", "licensor", "licensee"}
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in stop}


def ratio(n: int, d: int) -> str:
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuad", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--start", type=int, default=0, help="first contract index (use disjoint ranges for tune/held-out)")
    ap.add_argument("--min-conf", type=float, default=0.8)
    ap.add_argument("--json")
    a = ap.parse_args()
    root = Path(a.cuad) / "CUAD_v1"
    rows = list(csv.DictReader(open(root / "master_clauses.csv", encoding="utf-8")))
    rows = rows[a.start:]
    if a.limit:
        rows = rows[: a.limit]
    recipe = docling_recipe(settings, "docling", "1")
    st: dict[str, defaultdict] = defaultdict(lambda: defaultdict(int))
    wrong: dict[str, list] = defaultdict(list)
    for r in rows:
        stem = Path(r["Filename"]).stem
        txt_path = root / "full_contract_txt" / f"{stem}.txt"
        if not txt_path.exists():
            continue
        text = txt_path.read_text(encoding="utf-8", errors="replace")
        _, drafts = chunk_document(text, recipe)
        chunks = [ChunkText(f"c{i}", d.text, i, getattr(d, "heading", None)) for i, d in enumerate(drafts)]
        parties = classify.extract_parties("\n".join(c.text for c in chunks[:3])[:3500])
        got = {f.field: f for f in extract_all(chunks, parties)}
        ok = lambda f: f in got and got[f].confidence >= a.min_conf  # noqa: E731

        # governing law
        g = (r.get("Governing Law-Answer") or "").strip()
        if g:
            st["governing_law"]["gold"] += 1
        if g and not ok("governing_law"):
            wrong["governing_law_missed"].append((stem, g, got["governing_law"].normalized if "governing_law" in got else None))
        if ok("governing_law"):
            st["governing_law"]["extracted"] += 1
            ours = set(norm_law(got["governing_law"].normalized).split()) - {"and"}
            theirs = set(norm_law(g).split()) - {"and"}
            good = bool(g) and bool(ours) and (ours <= theirs or theirs <= ours)
            st["governing_law"]["correct"] += good
            if not good:
                wrong["governing_law"].append((stem, got["governing_law"].normalized, g))
        # effective date (gold: Effective Date, else Agreement Date)
        ge = gold_date(r.get("Effective Date-Answer", "")) or gold_date(r.get("Agreement Date-Answer", ""))
        if ge:
            st["effective_date"]["gold"] += 1
        if ok("effective_date"):
            st["effective_date"]["extracted"] += 1
            good = got["effective_date"].normalized == ge
            st["effective_date"]["correct"] += good
            if not good:
                wrong["effective_date"].append((stem, got["effective_date"].normalized, ge))
        # expiration (comparable only when both are dates)
        gx = gold_date(r.get("Expiration Date-Answer", ""))
        if gx:
            st["expiration_date"]["gold"] += 1
        if ok("expiration_or_term") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", got["expiration_or_term"].normalized or ""):
            st["expiration_date"]["extracted"] += 1
            st["expiration_date"]["correct"] += got["expiration_or_term"].normalized == gx
        # renewal term
        gr = parse_duration(r.get("Renewal Term-Answer", "") or "")
        if gr:
            st["renewal_term"]["gold"] += 1
        if ok("renewal_term"):
            st["renewal_term"]["extracted"] += 1
            good = bool(gr) and got["renewal_term"].normalized == gr[0]
            st["renewal_term"]["correct"] += good
            if not good:
                wrong["renewal_term"].append((stem, got["renewal_term"].normalized, r.get("Renewal Term-Answer")))
        # termination notice vs the annotated termination-for-convenience clause text
        tfc_clause = r.get("Termination For Convenience") or ""
        tfc_dur = parse_duration(tfc_clause)
        if (r.get("Termination For Convenience-Answer") or "").strip() == "Yes" and tfc_dur:
            st["notice_period"]["gold"] += 1
        if ok("termination_notice_period"):
            st["notice_period"]["extracted"] += 1
            n = got["termination_notice_period"].normalized
            good = bool(tfc_dur) and n == tfc_dur[0] or (n or "") in re.sub(r"\s+", " ", tfc_clause) or bool(
                n and re.search(rf"\b{n.split()[0]}\b", tfc_clause) and tfc_clause != "")
            st["notice_period"]["correct"] += good
            if not good:
                wrong["notice_period"].append((stem, n, tfc_clause[:120]))
        # parties
        gp = [p for p in re.split(r";\s*", r.get("Parties-Answer") or "") if p.strip()]
        gtok = [name_tokens(p) for p in gp]
        otok = [name_tokens(p) for p in parties]
        st["parties"]["gold"] += len(gtok)
        st["parties"]["extracted"] += len(otok)
        st["parties"]["correct"] += sum(1 for o in otok if o and any(o & g_ and len(o & g_) / len(o | g_) >= 0.5 for g_ in gtok))
        st["parties"]["recalled"] += sum(1 for g_ in gtok if g_ and any(o & g_ and len(o & g_) / len(o | g_) >= 0.5 for o in otok))
        # presence-type fields
        for field, col in PRESENCE.items():
            gy = (r.get(col) or "").strip() == "Yes"
            pred = ok(field) and got[field].normalized not in ("absent",)
            if field == "liability_cap":
                pred = ok(field) and (got[field].normalized or "").startswith("$")
                gy = gy and "$" in (r.get("Cap On Liability") or "")
            s = st[field]
            s["gold"] += gy
            s["extracted"] += pred
            s["correct"] += gy and pred
            if pred and not gy:
                wrong[field].append((stem, got[field].value[:100]))
            if gy and not pred:
                wrong[field + "_missed"].append((stem, (r.get(field.title().replace("_", "-") if False else "") or "")[:0]))
    out = {}
    print(f"contracts: {len(rows)}  min_conf: {a.min_conf}")
    print(f"{'field':20} {'precision':22} {'recall':22}")
    for field in ["governing_law", "effective_date", "expiration_date", "renewal_term", "notice_period", "parties",
                  "liability_cap", "exclusivity", "non_compete", "anti_assignment", "audit_rights", "insurance"]:
        s = st[field]
        rec = s["recalled"] if field == "parties" else s["correct"]
        print(f"{field:20} {ratio(s['correct'], s['extracted']):22} {ratio(rec, s['gold']):22}")
        out[field] = dict(s)
    if a.json:
        Path(a.json).write_text(json.dumps({"stats": out, "wrong": {k: v[:25] for k, v in wrong.items()}}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
