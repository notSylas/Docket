"""E4 (Upgrade/08 section 8): wider candidate pool + Ollama pointwise reranker, RETRIEVAL ONLY.

Usage (ALWAYS with DOCKET_DATA_DIR pointed at a scratch dir; never the real one):
  DOCKET_DATA_DIR=<scratch>/home python e4_pool_rerank.py --scratch <scratch> --out <scratch>/e4.json \
      [--sets g1,ext,orig] [--models qwen3:8b,qwen3:14b]
Then `--report <e4.json> --md e4_results.md` renders the markdown tables.

Product code is not touched: ingestion reuses docket.eval.runner.EvalRunner into a scratch data dir,
retrieval calls docket.infra.retrieval.hybrid.hybrid_search directly, matching uses
docket.eval.scoring.span_hits. follow_up questions are skipped (they need the LLM rewrite).
"""
from __future__ import annotations

import argparse, json, math, os, sys, time
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent.parent
SETS = {
    "g1": ("gold-g1.yaml", EVAL / "corpus-synthetic-g1"),
    "ext": ("gold-extended.yaml", EVAL / "corpus-synthetic"),
    "orig": ("gold.yaml", EVAL.parent.parent / "Docs"),
}
POOLS = (8, 30)
KS = (8, 12, 20)
OLLAMA = "http://localhost:11434"
SYSTEM = (
    "You judge search results. Given a question and a passage from a document, answer 'yes' if the passage "
    "contains the specific information (the exact figure, cell, row, name or fact) needed to answer the "
    "question, otherwise answer 'no'. Reply with exactly one word: yes or no."
)


def rerank_text(c) -> str:
    parts = [c.location or "", *(c.context or []), c.text]
    return "\n".join(p for p in parts if p)


def logsumexp(xs):
    if not xs:
        return -50.0
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def judge(client: httpx.Client, model: str, question: str, passage: str) -> float:
    body = {
        "model": model, "stream": False, "think": False, "logprobs": True, "top_logprobs": 20,
        "options": {"temperature": 0, "num_predict": 1, "num_ctx": 8192},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Question: {question}\n\nPassage:\n{passage}\n\nDoes the passage contain what is needed to answer the question? yes or no:"},
        ],
    }
    r = client.post(f"{OLLAMA}/api/chat", json=body, timeout=300)
    r.raise_for_status()
    tops = r.json()["logprobs"][0]["top_logprobs"]
    yes = [t["logprob"] for t in tops if t["token"].strip().lower() == "yes"]
    no = [t["logprob"] for t in tops if t["token"].strip().lower() == "no"]
    return logsumexp(yes) - logsumexp(no)


def collect(set_name: str, scratch: Path, models: list[str]) -> dict:
    from docket.eval.runner import EvalRunner
    from docket.eval.schema import load_gold_set, QuestionType
    from docket.infra.inference.gateway import OllamaGateway
    from docket.infra.retrieval.hybrid import hybrid_search

    gold_file, corpus = SETS[set_name]
    gold = load_gold_set(EVAL / gold_file)
    qs = [q for q in gold.questions if q.answerable and q.gold_spans and q.type is not QuestionType.FOLLOW_UP]
    data_dir = scratch / f"data-{set_name}"
    assert str(data_dir).startswith(str(scratch)) and ".local/share/docket" not in str(data_dir)
    out = {"questions": [], "skipped_follow_up": sum(1 for q in gold.questions if q.type is QuestionType.FOLLOW_UP)}
    with EvalRunner(corpus, gateway=OllamaGateway(), data_dir=data_dir) as runner:
        t0 = time.time(); runner.ingest(); print(f"[{set_name}] ingested in {time.time()-t0:.0f}s, failures={runner.ingest_failures}", flush=True)
        ctx = runner._context
        table = ctx.vector_writer.table
        gw = OllamaGateway()
        client = httpx.Client()
        for qi, q in enumerate(qs):
            rec = {"id": q.id, "type": q.type.value, "n_spans": len(q.gold_spans), "pools": {}, "rerank": {}}
            fused30 = None
            for pool in POOLS:
                t = time.perf_counter()
                ranked = hybrid_search(engine=ctx.engine, table=table, gateway=gw, query=q.question, top_k=pool)
                rec["pools"][str(pool)] = {"retrieval_s": time.perf_counter() - t}
                chunks = ctx.resolver.resolve_many([r.chunk_id for r in ranked])
                rec["pools"][str(pool)]["texts"] = [c.text for c in chunks]
                if pool == 30:
                    fused30 = chunks
            rec["spans"] = list(q.gold_spans)
            rec["pool30_n"] = len(fused30)
            for model in models:
                scores = []
                t = time.perf_counter()
                for c in fused30:
                    scores.append(judge(client, model, q.question, rerank_text(c)))
                rec["rerank"][model] = {"latency_s": time.perf_counter() - t, "scores": scores}
            out["questions"].append(rec)
            print(f"[{set_name}] {qi+1}/{len(qs)} {q.id} " + " ".join(f"{m}={rec['rerank'][m]['latency_s']:.1f}s" for m in models), flush=True)
    return out


# ---------------------------------------------------------------- metrics

def norm_hits(spans, texts):
    from docket.eval.scoring import span_hits
    return span_hits(spans, texts)


def metrics_for(rec, texts):
    from docket.eval.scoring import retrieval_hit
    hits = norm_hits(rec["spans"], texts)
    first = 0.0
    for i, t in enumerate(texts):
        if retrieval_hit(rec["spans"], [t]):
            first = 1.0 / (i + 1)
            break
    return any(hits), all(hits), first


def configs(rec, models):
    """name -> ordered texts."""
    cfg = {}
    for pool in POOLS:
        texts = rec["pools"][str(pool)]["texts"]
        for k in KS:
            cfg[f"fuse pool{pool} k{k}"] = texts[:k]
    t30 = rec["pools"]["30"]["texts"]
    for m in models:
        sc = rec["rerank"][m]["scores"]
        order = sorted(range(len(t30)), key=lambda i: -sc[i])  # stable: ties keep fused order
        for k in (8, 12):
            cfg[f"rerank {m} pool30 k{k}"] = [t30[i] for i in order[:k]]
    return cfg


def agg(rows):
    n = len(rows)
    return (sum(r[0] for r in rows), sum(r[1] for r in rows), sum(r[2] for r in rows) / n if n else 0, n)


def pct(a, n):
    return f"{100*a/n:.1f}% ({a}/{n})" if n else "-"


def report(data: dict, models: list[str]) -> str:
    L = []
    names = {"g1": "G1 (hard numeric)", "ext": "Extended", "orig": "Original gold.yaml (../Docs)"}
    for s, d in data.items():
        qs = d["questions"]
        if not qs:
            continue
        L.append(f"## {names[s]}: {len(qs)} questions (follow_up skipped: {d['skipped_follow_up']})\n")
        per = {}
        for rec in qs:
            for name, texts in configs(rec, models).items():
                per.setdefault(name, {})[rec["id"]] = metrics_for(rec, texts)
        types = sorted({r["type"] for r in qs})
        L.append("### Overall\n\n| config | any-span | all-spans | MRR |\n|---|---|---|---|")
        for name, m in per.items():
            a, al, mrr, n = agg(list(m.values()))
            L.append(f"| {name} | {pct(a,n)} | {pct(al,n)} | {mrr:.3f} |")
        base = per["fuse pool8 k8"]
        L.append("\n### Net all-spans change vs baseline (pool8 k8, production default), questions\n\n| config | gained | lost | net | any-span gained | any-span lost |\n|---|---|---|---|---|---|")
        for name, m in per.items():
            g = sum(1 for i in m if m[i][1] and not base[i][1]); l = sum(1 for i in m if base[i][1] and not m[i][1])
            ag = sum(1 for i in m if m[i][0] and not base[i][0]); al_ = sum(1 for i in m if base[i][0] and not m[i][0])
            L.append(f"| {name} | {g} | {l} | {g-l:+d} | {ag} | {al_} |")
        for t in types:
            ids = [r["id"] for r in qs if r["type"] == t]
            L.append(f"\n### Type `{t}` ({len(ids)} questions)\n\n| config | any-span | all-spans | MRR |\n|---|---|---|---|")
            for name, m in per.items():
                a, al, mrr, n = agg([m[i] for i in ids])
                L.append(f"| {name} | {pct(a,n)} | {pct(al,n)} | {mrr:.3f} |")
        L.append("\n### Latency (seconds per question)\n\n| step | p50 | p90 | mean |\n|---|---|---|---|")
        def stats(xs):
            xs = sorted(xs); n = len(xs)
            return f"{xs[n//2]:.2f} | {xs[min(n-1,int(n*0.9))]:.2f} | {sum(xs)/n:.2f}"
        for pool in POOLS:
            L.append(f"| hybrid_search pool{pool} (embed+FTS+vector+fuse) | {stats([r['pools'][str(pool)]['retrieval_s'] for r in qs])} |")
        for m in models:
            L.append(f"| rerank {m}, {sum(r['pool30_n'] for r in qs)//len(qs)} chunks (sequential) | {stats([r['rerank'][m]['latency_s'] for r in qs])} |")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch"); ap.add_argument("--out"); ap.add_argument("--sets", default="g1,ext")
    ap.add_argument("--models", default="qwen3:8b,qwen3:14b")
    ap.add_argument("--report"); ap.add_argument("--md")
    a = ap.parse_args()
    models = a.models.split(",")
    if a.report:
        data = json.load(open(a.report))
        md = report(data, models)
        (open(a.md, "w") if a.md else sys.stdout).write(md)
        return
    scratch = Path(a.scratch).resolve()
    dd = os.environ.get("DOCKET_DATA_DIR", "")
    if not dd or not dd.startswith(str(scratch)) and "/tmp/claude-1000" not in dd:
        sys.exit("refusing: DOCKET_DATA_DIR must be a scratch dir")
    results = {}
    for s in a.sets.split(","):
        results[s] = collect(s, scratch, models)
        json.dump(results, open(a.out, "w"))


if __name__ == "__main__":
    main()
