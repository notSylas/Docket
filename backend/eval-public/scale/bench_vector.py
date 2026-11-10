"""Vector-search microbenchmark WITHOUT Ollama (synthetic vectors). Measurement only.

    backend/.venv/bin/python bench_vector.py --work DIR --rows 100000 500000 1000000 2000000 --out vec.json

Per row count N: builds a LanceDB table with the same schema as docket.infra.index.vector_index
(chunk_id, source_id, text ~2 KB, vector[1024] float32, evidence_version_id; 1 version per 30 chunks),
vectors are CLUSTERED synthetic (2000 gaussian centroids + noise, unit-normalised) -- NOT real embeddings, so
recall numbers are indicative only. Then, exactly as hybrid.py::_search_eligible does:
    table.search(vec).where("evidence_version_id IN (<ids>)", prefilter=True).limit(30).to_list()
with all / 50% / 10% eligible versions (flat scan, no ANN index), plus an unfiltered flat scan, the cost of
building the IN-list string, and the real eligible-ids SQL on a SQLite DB with one row per version
(sources + evidence_versions, same columns/indexes). Then an IVF_PQ index (num_sub_vectors=dim/8) is built and
queried with nprobes/refine_factor variants, with and without the IN filter; recall@30 is vs the exact flat
result with the same filter. Each timed config runs in a spawned subprocess with a hard timeout.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sqlite3
import statistics
import time
import uuid
from pathlib import Path

import numpy as np
import pyarrow as pa

DIM = 1024
CHUNKS_PER_VERSION = 30
TEXT_LEN = 2000
N_QUERIES = 12


def vid(v: int) -> str:
    return "ver_" + uuid.UUID(int=(v * 2654435761) % (1 << 128)).hex  # 36 chars, uuid-like, deterministic


def _centroids(seed=1):
    rng = np.random.default_rng(seed)
    return rng.standard_normal((2000, DIM)).astype(np.float32)


def make_batch(start: int, n: int, cent: np.ndarray) -> pa.RecordBatch:
    rng = np.random.default_rng(start + 17)
    assign = rng.integers(0, len(cent), n)
    vec = cent[assign] + 0.6 * rng.standard_normal((n, DIM), dtype=np.float32)
    vec /= np.linalg.norm(vec, axis=1, keepdims=True)
    letters = np.frombuffer(b"abcdefghijklmnopqrstuvwxyz      ", dtype=np.uint8)
    txt = letters[rng.integers(0, len(letters), (n, TEXT_LEN))]
    texts = pa.array(txt.view(f"S{TEXT_LEN}").ravel(), type=pa.binary()).cast(pa.string())
    ids = np.arange(start, start + n)
    if isinstance(texts, pa.ChunkedArray):
        texts = texts.combine_chunks()
    chunk_ids = pa.array([f"chk_{i:064x}" for i in ids])
    versions = pa.array([vid(int(i) // CHUNKS_PER_VERSION) for i in ids])
    source = pa.array([f"src_{int(i) // 3000:06d}" + "0" * 20 for i in ids])
    vectors = pa.FixedSizeListArray.from_arrays(pa.array(vec.ravel()), DIM)
    return pa.RecordBatch.from_arrays([chunk_ids, source, texts, vectors, versions],
                                      names=["chunk_id", "source_id", "text", "vector", "evidence_version_id"])


def build_table(db_dir: Path, n: int) -> dict:
    import lancedb
    cent = _centroids()
    db = lancedb.connect(str(db_dir))
    schema = make_batch(0, 2, cent).schema
    t0 = time.perf_counter()

    def gen():
        step = 50_000
        for s in range(0, n, step):
            yield make_batch(s, min(step, n - s), cent)

    reader = pa.RecordBatchReader.from_batches(schema, gen())
    db.create_table("chunks", data=reader, mode="overwrite")
    return {"build_seconds": round(time.perf_counter() - t0, 1)}


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def sql_in(vals) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in vals)


def make_queries(n_rows: int):
    cent = _centroids()
    rng = np.random.default_rng(99)
    q = cent[rng.integers(0, len(cent), N_QUERIES)] + 0.6 * rng.standard_normal((N_QUERIES, DIM), dtype=np.float32)
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    return q


def eligible_subset(n_rows: int, frac: float):
    n_ver = (n_rows + CHUNKS_PER_VERSION - 1) // CHUNKS_PER_VERSION
    rng = np.random.default_rng(5)
    k = max(1, int(n_ver * frac))
    chosen = np.sort(rng.choice(n_ver, k, replace=False)) if frac < 1 else np.arange(n_ver)
    return [vid(int(v)) for v in chosen]


def _worker(q, db_dir, n_rows, mode, frac, nprobes, refine, truth):
    import lancedb
    table = lancedb.connect(db_dir).open_table("chunks")
    queries = make_queries(n_rows)
    ids = eligible_subset(n_rows, frac) if frac is not None else None
    t0 = time.perf_counter()
    where = f"evidence_version_id IN ({sql_in(ids)})" if ids is not None else None
    t_build_in = time.perf_counter() - t0
    lat, res = [], []
    for i, qv in enumerate(queries):
        s = table.search(qv.tolist())
        if mode == "ann":
            s = s.nprobes(nprobes)
            if refine:
                s = s.refine_factor(refine)
        else:
            s = s.bypass_vector_index() if hasattr(s, "bypass_vector_index") else s
        if where:
            s = s.where(where, prefilter=True)
        t1 = time.perf_counter()
        rows = s.limit(30).to_list()
        lat.append(time.perf_counter() - t1)
        res.append([r["chunk_id"] for r in rows])
        if i == 0 and lat[0] > 120:  # too slow to repeat
            break
    rec = None
    if truth is not None:
        rec = statistics.mean(len(set(a) & set(truth[i])) / max(1, len(truth[i])) for i, a in enumerate(res))
    q.put({"lat": lat, "t_build_in_list_s": t_build_in, "in_list_chars": len(where) if where else 0,
           "recall30": rec, "results": res})


def timed_run(db_dir, n_rows, mode, frac, nprobes=None, refine=None, truth=None, timeout=900):
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_worker, args=(q, str(db_dir), n_rows, mode, frac, nprobes, refine, truth))
    p.start()
    try:
        out = q.get(timeout=timeout)
    except Exception:
        p.kill()
        p.join()
        return {"timeout_s": timeout}
    p.join()
    lat = sorted(out["lat"])
    r = {"n_queries": len(lat), "p50_s": round(statistics.median(lat), 4),
         "p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 4), "max_s": round(lat[-1], 4),
         "in_list_chars": out["in_list_chars"], "build_in_list_s": round(out["t_build_in_list_s"], 4),
         "recall30": None if out["recall30"] is None else round(out["recall30"], 4)}
    return r, out["results"]


def sqlite_eligible_bench(path: Path, n_versions: int) -> dict:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    con.execute("CREATE TABLE evidence_versions (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, status TEXT NOT NULL, "
                "file_path TEXT, content_hash TEXT)")
    con.execute("CREATE INDEX ix_ev_source ON evidence_versions(source_id)")
    nsrc = max(1, n_versions // 100)
    con.executemany("INSERT INTO sources VALUES (?,?)", [(f"src_{i:06d}" + "0" * 20, "ACTIVE") for i in range(nsrc)])
    con.executemany("INSERT INTO evidence_versions VALUES (?,?,?,?,?)",
                    ((vid(v), f"src_{v // 100:06d}" + "0" * 20, "READY", f"/lib/x/{v}.pdf", "h" * 64)
                     for v in range(n_versions)))
    con.commit()
    sql = ("SELECT evidence_versions.id FROM evidence_versions JOIN sources ON sources.id = evidence_versions.source_id "
           "WHERE sources.status = 'ACTIVE' AND evidence_versions.status = 'READY'")
    ts, tb = [], []
    for _ in range(5):
        t0 = time.perf_counter()
        ids = [r[0] for r in con.execute(sql)]
        ts.append(time.perf_counter() - t0)
        t1 = time.perf_counter()
        s = sql_in(ids)
        tb.append(time.perf_counter() - t1)
    con.close()
    return {"n_versions": n_versions, "sqlite_load_ids_p50_s": round(statistics.median(ts), 4),
            "build_in_list_string_p50_s": round(statistics.median(tb), 4), "in_list_chars": len(s)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--rows", type=int, nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    a.work.mkdir(parents=True, exist_ok=True)
    allres = json.loads(a.out.read_text()) if a.out.exists() else {}
    for n in a.rows:
        key = str(n)
        R = allres.setdefault(key, {})
        db_dir = a.work / f"lance_{n}"
        print(f"=== {n} rows", flush=True)
        if not (db_dir / "chunks.lance").exists():
            R["table_build"] = build_table(db_dir, n)
        R["table_bytes_flat"] = dir_size(db_dir)
        R["bytes_per_row_flat"] = round(R["table_bytes_flat"] / n)
        n_ver = (n + CHUNKS_PER_VERSION - 1) // CHUNKS_PER_VERSION
        R["sqlite_eligible_ids"] = sqlite_eligible_bench(a.work / f"el_{n}.db", n_ver)
        (a.work / f"el_{n}.db").unlink()
        flat = R.setdefault("flat_scan", {})
        truth = {}
        for label, frac in [("no_filter", None), ("in_all", 1.0), ("in_50pct", 0.5), ("in_10pct", 0.1)]:
            out = timed_run(db_dir, n, "flat", frac, timeout=1200)
            if isinstance(out, dict):
                flat[label] = out
                continue
            flat[label], truth[label] = out
            print(n, "flat", label, flat[label], flush=True)
            a.out.write_text(json.dumps(allres, indent=1))
        if not a.skip_index:
            import lancedb
            tbl = lancedb.connect(str(db_dir)).open_table("chunks")
            t0 = time.perf_counter()
            try:
                tbl.create_index(vector_column_name="vector", index_type="IVF_PQ",
                                 num_sub_vectors=DIM // 8, replace=True)
                R["ivf_pq"] = {"build_seconds": round(time.perf_counter() - t0, 1)}
            except Exception as e:  # noqa: BLE001
                R["ivf_pq"] = {"error": repr(e), "after_s": round(time.perf_counter() - t0, 1)}
            R["table_bytes_with_index"] = dir_size(db_dir)
            R["index_bytes"] = R["table_bytes_with_index"] - R["table_bytes_flat"]
            ann = R["ivf_pq"].setdefault("queries", {})
            if "error" not in R["ivf_pq"]:
                for label, frac in [("no_filter", None), ("in_all", 1.0), ("in_10pct", 0.1)]:
                    for nprobes, refine in [(1, None), (10, None), (20, 10), (50, 10), (100, 30)]:
                        out = timed_run(db_dir, n, "ann", frac, nprobes, refine, truth.get(label), timeout=900)
                        k = f"{label}|nprobes={nprobes}|refine={refine}"
                        ann[k] = out if isinstance(out, dict) else out[0]
                        print(n, "ivf_pq", k, ann[k], flush=True)
                        a.out.write_text(json.dumps(allres, indent=1))
        a.out.write_text(json.dumps(allres, indent=1))
        if not a.keep:
            import shutil
            shutil.rmtree(db_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
