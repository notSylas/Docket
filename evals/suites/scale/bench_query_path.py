"""Time the retrieval legs on a real ingested library (read-only; needs one Ollama embed per query).

    backend/.venv/bin/python bench_query_path.py DATA_DIR out.json "query 1" "query 2" ...

Reports: library size (chunks, READY versions, lance fragments/versions), _eligible_version_ids (SQLite),
IN-list string size, vector_search body (LanceDB flat scan, prefilter) with a pre-computed vector, fts_search,
the query embedding call, and whole hybrid_search, each as p50 over 5 repeats."""
import json, statistics, sys, time
from pathlib import Path

from docket.infra.retrieval import hybrid
from docket.interfaces.cli.context import AppContext

data, out, queries = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:] or ["governing law", "payment terms"]
assert "/.local/share/docket" not in str(data.resolve())
ctx = AppContext.for_testing(data_dir=data)
table = ctx.vector_writer.table


def p50(fn, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter(); r = fn(); ts.append(time.perf_counter() - t0)
    return round(statistics.median(ts), 4), round(max(ts), 4), r


res = {"data_dir": str(data), "rows": table.count_rows(), "lance_versions": len(table.list_versions()),
       "lance_fragments": table.stats()["fragment_stats"]["num_fragments"]}
m, mx, ids = p50(lambda: hybrid._eligible_version_ids(ctx.engine))
res["eligible_version_ids"] = {"n": len(ids), "p50_s": m, "max_s": mx}
m, mx, s = p50(lambda: hybrid._sql_in_list(ids))
res["in_list_string"] = {"chars": len(s), "p50_s": m}
res["queries"] = {}
for q in queries:
    t0 = time.perf_counter(); vec = ctx.gateway.embed(q); t_emb = time.perf_counter() - t0
    m_v, mx_v, _ = p50(lambda: hybrid._search_eligible(table, ctx.engine, vec, 30))
    m_f, mx_f, _ = p50(lambda: hybrid.fts_search(ctx.engine, q, 30))
    m_h, mx_h, _ = p50(lambda: hybrid.hybrid_search(engine=ctx.engine, table=table, gateway=ctx.gateway, query=q, top_k=18))
    res["queries"][q] = {"embed_query_s": round(t_emb, 3), "vector_search_incl_eligible_sql_p50_s": m_v, "vector_max_s": mx_v,
                         "fts_search_p50_s": m_f, "fts_max_s": mx_f, "hybrid_search_total_p50_s": m_h, "hybrid_max_s": mx_h}
out.write_text(json.dumps(res, indent=1))
print(json.dumps(res, indent=1))
