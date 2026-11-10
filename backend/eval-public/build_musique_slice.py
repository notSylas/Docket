"""Deterministically build the MuSiQue multi-hop slice: shared paragraph library + gold-musique.yaml.

Source: MuSiQue-Full v1.0 dev (Trivedi et al., TACL 2022), CC BY 4.0
(https://github.com/StonyBrookNLP/musique). Full pairs each answerable question
with an unanswerable twin that has one supporting paragraph removed. The raw
jsonl is NOT committed: put musique_full_v1.0_dev.jsonl at
eval-public/external/musique/raw/full_dev.jsonl (or set MUSIQUE_RAW). Mirror used:
huggingface.co/datasets/bdsaglam/musique.

Library: one .txt file per distinct paragraph ("<title>" heading line + text),
hashed into six sub-folders (sources). Each selected question contributes its
supporting paragraphs plus DISTRACTORS of its own distractors; all questions
share one library, so retrieval faces every other question's paragraphs as
noise. Paragraphs that are the *removed* supporting paragraph of a selected
unanswerable twin are never written (asserted), so those questions have no
evidence in the library.

Gold: answerable questions get must_contain = the dataset answer + its aliases as
regex alternatives (word-bounded, after docket's text normalisation) and one
verbatim 20-60 char gold span per supporting paragraph, verified to sit inside a
single indexed chunk (backend venv ingest with fake embeddings). Unanswerable
questions come from the dataset's own unanswerable variant (answerable: false).

    <venv>/bin/python eval-public/build_musique_slice.py     (needs pyyaml)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_finqa_slice import dump_chunks, norm  # noqa: E402

EXT = HERE / "external" / "musique"
RAW = Path(os.environ.get("MUSIQUE_RAW", EXT / "raw" / "full_dev.jsonl"))
CORPUS = EXT / "corpus"
GOLD = HERE / "gold-musique.yaml"
N_FOLDERS = 6
DISTRACTORS = 8
ANS_QUOTA = {"2hop": 12, "3hop": 8, "4hop": 4}
UNANS_QUOTA = {"2hop": 3, "3hop": 2, "4hop": 1}


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hop(r: dict) -> str:
    return r["id"].split("hop")[0] + "hop"


def slug(t: str) -> str:
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()[:50] or "untitled"


def key(p: dict) -> tuple[str, str]:
    return p["title"], p["paragraph_text"]


def fname(p: dict) -> str:
    return f"{slug(p['title'])}-{h(p['title'] + '|' + p['paragraph_text'])[:8]}.txt"


def folder(p: dict) -> str:
    return f"lib-{int(h(p['title'] + p['paragraph_text']), 16) % N_FOLDERS}"


def pattern(answer: str, aliases: list[str]) -> str:
    seen, alts = set(), []
    for a in [answer] + list(aliases):
        n = unicodedata.normalize("NFKC", a)
        n = re.sub(r"(?<=\d),(?=\d{3}\b)", "", n)
        n = re.sub(r"\s+", " ", n).strip().casefold()
        if n and n not in seen:
            seen.add(n)
            alts.append(rf"(?<![\w]){re.escape(n)}(?![\w])")
    return "re:" + "|".join(alts)


def span_for(p: dict, needle: str | None, chunks: list[str]) -> str | None:
    text = re.sub(r"\s+", " ", p["paragraph_text"]).strip()
    anchors = []
    if needle:
        i = text.casefold().find(needle.casefold())
        if i >= 0:
            anchors.append((i, i + len(needle)))
    anchors.append((0, min(len(text), 40)))
    for s, e in anchors:
        for width in (60, 54, 46, 38, 30, 24):
            w = max(width, e - s) if e - s <= 60 else 60
            lo = max(0, min(s - (w - (e - s)) // 2, len(text) - w))
            seg = text[lo : lo + w].strip()
            if 20 <= len(seg) <= 60 and any(norm(seg) in c for c in chunks):
                return seg
    return None


def main() -> None:
    if not RAW.exists():
        raise SystemExit(f"missing {RAW}: see module docstring")
    rows = [json.loads(line) for line in RAW.read_text().splitlines()]
    twins = defaultdict(dict)
    for r in rows:
        twins[r["id"]][r["answerable"]] = r
    ans_pick, quota = [], dict(ANS_QUOTA)
    for r in sorted((r for r in rows if r["answerable"] and len(r["answer"]) <= 60), key=lambda r: h(r["id"])):
        if quota.get(hop(r), 0) > 0:
            ans_pick.append(r)
            quota[hop(r)] -= 1
    un_pick, quota = [], dict(UNANS_QUOTA)
    chosen_ids = {r["id"] for r in ans_pick}
    for r in sorted((r for r in rows if not r["answerable"] and r["id"] not in chosen_ids), key=lambda r: h(r["id"])):
        if quota.get(hop(r), 0) > 0:
            un_pick.append(r)
            quota[hop(r)] -= 1

    lib: dict[tuple[str, str], dict] = {}
    removed: set[tuple[str, str]] = set()
    per_q: dict[str, dict] = {}
    for r in ans_pick + un_pick:
        twin = twins[r["id"]][True]
        sup_twin = [p for p in twin["paragraphs"] if p["is_supporting"]]
        have = {key(p) for p in r["paragraphs"]}
        if not r["answerable"]:
            removed |= {key(p) for p in sup_twin if key(p) not in have}
            support = [p for p in r["paragraphs"] if key(p) in {key(q) for q in sup_twin}]
        else:
            support = [p for p in r["paragraphs"] if p["is_supporting"]]
        sup_keys = {key(p) for p in sup_twin}
        dist = sorted((p for p in r["paragraphs"] if key(p) not in sup_keys), key=lambda p: h(p["title"] + p["paragraph_text"]))
        dist = dist[:DISTRACTORS]
        for p in support + dist:
            lib[key(p)] = p
        per_q[r["id"] + ("" if r["answerable"] else "-unans")] = {"row": r, "support": support}
    for k in removed:
        lib.pop(k, None)  # never write the removed evidence, even if another question pooled it
    assert not (removed & set(lib))

    for d in CORPUS.glob("lib-*"):
        for f in d.glob("*.txt"):
            f.unlink()
    for k, p in sorted(lib.items(), key=lambda kv: fname(kv[1])):
        d = CORPUS / folder(p)
        d.mkdir(parents=True, exist_ok=True)
        (d / fname(p)).write_text(f"{p['title']}\n\n{p['paragraph_text']}\n", encoding="utf-8")
    chunks_raw = dump_chunks(CORPUS)
    chunks = [norm(c["text"]) for c in chunks_raw]
    Path(os.environ.get("MUSIQUE_DEBUG_CHUNKS", os.devnull)).write_text(json.dumps(chunks_raw))

    questions, dropped = [], []
    for qid, info in per_q.items():
        r, support = info["row"], info["support"]
        decomp = r["question_decomposition"]
        if r["answerable"]:
            spans, docs = [], []
            for p in support:
                idx = p["idx"]
                needle = next((d["answer"] for d in decomp if d.get("paragraph_support_idx") == idx), None)
                s = span_for(p, needle, chunks)
                if s and s not in spans:
                    spans.append(s)
                    docs.append(f"{folder(p)}/{fname(p)}")
            if len(spans) < 2:
                dropped.append(qid)
                continue
            questions.append({
                "id": "musique-" + qid, "type": "multi_doc", "question": r["question"], "answerable": True,
                "must_contain": [pattern(r["answer"], r["answer_aliases"])], "gold_spans": spans,
                "reviewed": True,
                "origin": f"MuSiQue-Full dev {r['id']} ({hop(r)}); answer {r['answer']!r}; eval-public/build_musique_slice.py",
                "source_documents": docs, "formula_dependent": False, "split": "dev",
            })
        else:
            questions.append({
                "id": "musique-" + qid, "type": "out_of_corpus", "question": r["question"], "answerable": False,
                "reviewed": True,
                "origin": f"MuSiQue-Full dev {r['id']} unanswerable variant ({hop(r)}; one supporting paragraph removed, "
                          f"dataset answer {r['answer']!r} not derivable); eval-public/build_musique_slice.py",
                "source_documents": [], "formula_dependent": False, "split": "dev",
            })
    header = (
        "# MuSiQue multi-hop slice over eval-public/external/musique/corpus (not committed; rebuild with\n"
        "# eval-public/build_musique_slice.py). Source: MuSiQue-Full v1.0 dev, CC BY 4.0.\n"
        "# GENERATED: answers+aliases from the dataset, spans verified single-chunk. PUBLIC DATA, not a milestone\n"
        "# population. Do not edit by hand.\n"
    )
    GOLD.write_text(header + yaml.safe_dump({"version": 1, "population": "public_musique", "questions": questions},
                                            sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    n = sum(q["answerable"] for q in questions)
    print(f"{GOLD.name}: {n} answerable + {len(questions) - n} unanswerable; library {len(lib)} paragraphs; "
          f"dropped {len(dropped)} {dropped}")


if __name__ == "__main__":
    main()
