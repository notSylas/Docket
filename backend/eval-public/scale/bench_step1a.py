"""Step 1a CPU microbenchmark (no Ollama): legacy vs scalable eligibility vs scalable + ANN.

    PYTHONPATH=backend/src backend/.venv/bin/python bench_step1a.py --work DIR --rows 200000 1000000 --out step1a.json

Per row count N: a LanceDB `chunks` table (1024-d clustered synthetic vectors, 30 chunks per version) and a
migrated SQLite DB with N/30 evidence versions (a fraction superseded but still present in the vector table, to
exercise the eligibility check). Queries call `docket.infra.retrieval.hybrid._search_eligible` directly in each mode
(DOCKET_ELIGIBILITY_MODE=legacy|scalable), top_k=30. Recall@30 is vs the legacy flat result (the exact eligible
top-30). Synthetic vectors, so recall numbers are indicative, not a quality claim. Numbers move with CPU load.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

import numpy as np
import pyarrow as pa

DIM = 1024
PER_VER = 30
N_CLUST = 2000


def _cent():
    return np.random.default_rng(1).standard_normal((N_CLUST, DIM)).astype(np.float32)


def _batch(start: int, n: int, cent, n_versions_stale_every: int) -> pa.RecordBatch:
    rng = np.random.default_rng(start + 17)
    vec = cent[rng.integers(0, N_CLUST, n)] + 0.6 * rng.standard_normal((n, DIM), dtype=np.float32)
    vec /= np.linalg.norm(vec, axis=1, keepdims=True)
    ids = np.arange(start, start + n)
    return pa.RecordBatch.from_arrays(
        [
            pa.array([f"chk_{i:016x}" for i in ids]),
            pa.array(["src_main"] * n),
            pa.array([""] * n),
            pa.FixedSizeListArray.from_arrays(pa.array(vec.ravel()), DIM),
            pa.array([f"ver_{int(i) // PER_VER:08d}" for i in ids]),
        ],
        names=["chunk_id", "source_id", "text", "vector", "evidence_version_id"],
    )


def build_lance(path: Path, n: int, cent):
    import lancedb

    schema = _batch(0, 2, cent, 0).schema

    def gen():
        for s in range(0, n, 50_000):
            yield _batch(s, min(50_000, n - s), cent, 0)

    db = lancedb.connect(str(path))
    t0 = time.perf_counter()
    t = db.create_table("chunks", data=pa.RecordBatchReader.from_batches(schema, gen()), mode="overwrite")
    return t, time.perf_counter() - t0


def build_sqlite(path: Path, n_versions: int, stale_frac: float):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from docket.core.db.engine import get_engine

    root = Path(__import__("docket").__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src/docket/core/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    engine = get_engine(path)
    # one real template version through the ORM, then bulk clone
    import sys

    sys.path.insert(0, str(root / "tests" / "unit"))
    from docket.infra.index.base import ChunkRecord
    from test_hybrid_retrieval import _insert_metadata  # type: ignore

    rec = ChunkRecord(chunk_id="chk_tpl", source_id="src_main", evidence_version_id="tpl", evidence_unit_id="eu_tpl",
                      chunk_recipe_id="rcp", ordinal=0, heading=None, text="t", content_hash="h")
    _insert_metadata(engine, [rec])
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(evidence_versions)"))]
        row = dict(conn.execute(text("SELECT * FROM evidence_versions WHERE id='tpl'")).mappings().one())
        stmt = text(f"INSERT INTO evidence_versions ({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})")
        rng = np.random.default_rng(3)
        stale = set(rng.choice(n_versions, int(n_versions * stale_frac), replace=False).tolist())
        batch = []
        for v in range(n_versions):
            r = dict(row)
            r["id"] = f"ver_{v:08d}"
            r["content_hash"] = f"h_{v}"
            if r.get("file_path") is not None:
                r["file_path"] = f"/tmp/f{v}"
            r["status"] = "SUPERSEDED" if v in stale else "READY"
            batch.append(r)
            if len(batch) == 5000:
                conn.execute(stmt, batch)
                batch = []
        if batch:
            conn.execute(stmt, batch)
    return engine


def du(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def run_queries(table, engine, queries, mode, nprobes=None, refine=None, k=30):
    from docket.infra.retrieval import hybrid

    os.environ["DOCKET_ELIGIBILITY_MODE"] = mode
    if nprobes:
        os.environ["DOCKET_ANN_NPROBES"] = str(nprobes)
        os.environ["DOCKET_ANN_REFINE_FACTOR"] = str(refine)
    lat, res = [], []
    hybrid._search_eligible(table, engine, queries[0].tolist(), k)  # warm
    for q in queries:
        t0 = time.perf_counter()
        rows = hybrid._search_eligible(table, engine, q.tolist(), k)
        lat.append(time.perf_counter() - t0)
        res.append([r["chunk_id"] for r in rows])
    lat.sort()
    return {"p50_s": round(statistics.median(lat), 4), "p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 4)}, res


def recall(res, truth):
    return round(statistics.mean(len(set(a) & set(t)) / max(1, len(t)) for a, t in zip(res, truth)), 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--rows", type=int, nargs="+", default=[200_000])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--stale-frac", type=float, default=0.02)
    ap.add_argument("--grid", action="store_true", help="also sweep (nprobes, refine_factor)")
    a = ap.parse_args()
    import docket
    from docket.infra.index.vector_index import maintain_table, vector_index_stats

    print("docket from", docket.__file__)
    a.work.mkdir(parents=True, exist_ok=True)
    cent = _cent()
    out = json.loads(a.out.read_text()) if a.out.exists() else {}
    for n in a.rows:
        d = a.work / f"n{n}"
        d.mkdir(exist_ok=True)
        table, t_load = build_lance(d / "lancedb", n, cent)
        engine = build_sqlite(d / "docket.sqlite3", (n + PER_VER - 1) // PER_VER, a.stale_frac)
        rng = np.random.default_rng(99)
        queries = cent[rng.integers(0, N_CLUST, a.queries)] + 0.6 * rng.standard_normal((a.queries, DIM), dtype=np.float32)
        queries /= np.linalg.norm(queries, axis=1, keepdims=True)
        R: dict = {"rows": n, "versions": (n + PER_VER - 1) // PER_VER, "stale_frac": a.stale_frac,
                   "table_bytes": du(d / "lancedb")}
        R["legacy_flat"], truth = run_queries(table, engine, queries, "legacy")
        R["scalable_flat"], res = run_queries(table, engine, queries, "scalable")
        R["scalable_flat"]["recall30"] = recall(res, truth)
        before = du(d / "lancedb")
        t0 = time.perf_counter()
        rep = maintain_table(table, min_rows=1, compact=False, keep_hours=1)
        R["ann_build_s"] = round(time.perf_counter() - t0, 1)
        R["ann_index_bytes"] = (vector_index_stats(table) or {}).get("size_bytes") or du(d / "lancedb") - before
        R["ann_params"] = rep["ann"]
        from docket.infra.index import tuning
        R["scalable_ann"], res = run_queries(table, engine, queries, "scalable", tuning.ann_nprobes(), tuning.ann_refine_factor())
        R["scalable_ann"]["recall30"] = recall(res, truth)
        R["scalable_ann"]["nprobes"], R["scalable_ann"]["refine"] = tuning.ann_nprobes(), tuning.ann_refine_factor()
        if a.grid:
            R["grid"] = {}
            for npb, rf in [(10, 5), (20, 10), (40, 10), (50, 20), (64, 30), (100, 30)]:
                lat, res = run_queries(table, engine, queries, "scalable", npb, rf)
                lat["recall30"] = recall(res, truth)
                R["grid"][f"nprobes={npb},refine={rf}"] = lat
        out[str(n)] = R
        a.out.write_text(json.dumps(out, indent=2))
        print(json.dumps(R, indent=2), flush=True)
    base = Path(os.environ.get("STEP0_RESULTS", "/home/pc/Desktop/Rajdeep_Dev/Local_Agent/backend/eval-public/scale/RESULTS.json"))
    if base.exists():
        print("Step 0 baseline file present:", base, "(compare manually)")


if __name__ == "__main__":
    main()
