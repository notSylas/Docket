"""Download ~N public arXiv PDFs (mixed categories) for the scale pilot. Polite: 3 s between requests.
Usage: python fetch_arxiv.py OUTDIR [N]. Downloaded PDFs are third-party data: never commit them."""
import re, sys, time, urllib.request, urllib.parse
from pathlib import Path

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
n_total = int(sys.argv[2]) if len(sys.argv) > 2 else 300
cats = ["cs.CL", "cs.LG", "math.PR", "physics.optics", "q-bio.NC", "econ.EM", "stat.ME", "cs.DB", "astro-ph.GA", "cond-mat.mes-hall"]
per = n_total // len(cats) + 3
UA = "docket-scale-bench/0.1 (local research benchmark)"

def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read()

ids = []
for c in cats:
    q = urllib.parse.urlencode({"search_query": f"cat:{c}", "start": 200, "max_results": per, "sortBy": "submittedDate"})
    xml = get(f"http://export.arxiv.org/api/query?{q}").decode()
    ids += re.findall(r"<id>http://arxiv.org/abs/([^<]+?)(?:v\d+)?</id>", xml)
    time.sleep(3)
ids = list(dict.fromkeys(ids))
print(len(ids), "ids", flush=True)
got = 0
for i in ids:
    if got >= n_total: break
    dest = out / (i.replace("/", "_") + ".pdf")
    if dest.exists(): got += 1; continue
    try:
        data = get(f"https://arxiv.org/pdf/{i}", 120)
        if data[:4] == b"%PDF" and len(data) < 25_000_000:
            dest.write_bytes(data); got += 1
    except Exception as e:
        print("fail", i, e, flush=True)
    time.sleep(3)
    if got % 25 == 0: print(got, flush=True)
print("done", got)
