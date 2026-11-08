"""MuSiQue distractor-scaling corpora. Leaves gold-musique.yaml and the existing library untouched.
For factor F in (1, 3, 10): out/musF = the existing library (298 paragraph files in lib-0..5) plus (F-1)*298 EXTRA
paragraphs taken from OTHER questions of the MuSiQue-Full dev file (deterministic sha256 order, skipping any
paragraph whose title already occurs in the base library) in a new source folder `extra`.
Usage: make_musique_scaled.py OUT_DIR   (needs eval-public/external/musique/{corpus,raw/full_dev.jsonl})"""
import hashlib, json, re, shutil, sys, unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
base = HERE / "external/musique/corpus"
raw = HERE / "external/musique/raw/full_dev.jsonl"
out = Path(sys.argv[1])
files = sorted(p for p in base.rglob("*.txt"))
titles = {p.read_text().split("\n", 1)[0].strip().lower() for p in files}
n_base = len(files)
h = lambda s: hashlib.sha256(s.encode()).hexdigest()
cand = {}
for line in raw.open():
    r = json.loads(line)
    for p in r["paragraphs"]:
        if p["title"].strip().lower() in titles:
            continue
        cand[h(p["title"] + "|" + p["paragraph_text"])] = p
order = sorted(cand)
print(n_base, "base paragraphs;", len(order), "candidate extra paragraphs")
slug = lambda t: re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()).strip("-")[:50] or "x"
for F in (1, 3, 10):
    d = out / f"mus{F}x"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(base, d)
    if F > 1:
        (d / "extra").mkdir()
        for k in order[: (F - 1) * n_base]:
            p = cand[k]
            (d / "extra" / f"{slug(p['title'])}-{k[:8]}.txt").write_text(f"{p['title']}\n\n{p['paragraph_text']}\n", encoding="utf-8")
    print(F, sum(1 for _ in d.rglob("*.txt")), "files")
