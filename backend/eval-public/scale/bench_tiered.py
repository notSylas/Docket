"""Tiered indexing benchmark (CPU, fake inference): time to first/complete
keyword-searchable vs time to fully indexed, against one-pass ingestion.

    python bench_tiered.py CORPUS_DIR --mode tiered|onepass --data-dir DIR --out RESULT.json

Inference is the project's FakeInferenceGateway (zero-latency embeddings) and
the Docling stand-in is a python-docx text parser, so the numbers measure the
pipeline, SQLite/FTS/Lance writes and queue overhead, NOT model cost. The
report script applies the documented real-model rates separately (clearly
labelled as a projection).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "tests" / "unit" / "ingestion"))

from docket.infra.inference.gateway import FakeInferenceGateway  # noqa: E402
from docket.infra.retrieval.hybrid import fts_search  # noqa: E402
from docket.interfaces.cli.context import AppContext  # noqa: E402
from pool_fakes import FakeParser  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--mode", choices=["tiered", "onepass"], required=True)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--query", default="agreement")
    args = ap.parse_args()

    ctx = AppContext.for_testing(data_dir=args.data_dir, gateway=FakeInferenceGateway(), parser=FakeParser())
    source = ctx.source_manager.register_source(args.corpus)
    res: dict = {"mode": args.mode, "corpus": str(args.corpus)}
    t0 = time.perf_counter()

    def query_ms() -> float:
        t = time.perf_counter()
        hits = fts_search(ctx.engine, args.query, 8)
        return round((time.perf_counter() - t) * 1000, 1), len(hits)

    if args.mode == "onepass":
        r = ctx.pipeline.run_ingestion_for_source(source.id)
        res.update(total_s=round(time.perf_counter() - t0, 2), files=r.files_processed, failed=r.files_failed)
    else:
        indexer = ctx.tiered_indexer()
        added = indexer.enqueue_source(source.id)
        res["enqueue_s"] = round(time.perf_counter() - t0, 2)
        res["jobs"] = added
        marks: dict[str, float] = {}
        counter = {"n": 0}
        total = added["new"] // 2

        def progress(kind, job, detail):
            if kind != "done" or job.tier != 1:
                return
            counter["n"] += 1
            n = counter["n"]
            for frac in (0.01, 0.1, 0.5, 1.0):
                key = f"tier1_{int(frac * 100)}pct_s"
                if key not in marks and n >= max(1, int(total * frac)):
                    marks[key] = round(time.perf_counter() - t0, 2)

        indexer._progress = progress
        s1 = indexer.run(tier2=False)
        res["tier1"] = dict(files=s1.tier1_done, run_s=round(s1.elapsed_s, 2), first_searchable_s=round(s1.first_searchable_s or 0, 3), marks=marks)
        res["after_tier1_query_ms_hits"] = query_ms()
        res["gateway_embed_calls_after_tier1"] = len(ctx.gateway.embed_calls)
        t2 = time.perf_counter()
        s2 = indexer.run()
        res["tier2"] = dict(files=s2.tier2_done, run_s=round(s2.elapsed_s, 2), failed=len(s2.failed_paths), dead=s2.dead)
        res["total_s"] = round(time.perf_counter() - t0, 2)
    res["final_query_ms_hits"] = query_ms()
    from docket.services.ingestion.coverage import version_counts

    res["versions"] = version_counts(ctx.engine)
    args.out.write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
