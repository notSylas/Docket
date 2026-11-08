"""Step 1b CPU microbenchmark (no Ollama): per-file index WRITE cost vs library size.

    PYTHONPATH=<code>/backend/src python bench_step1b_write.py --label after --work DIR \
        --sizes 1000 5000 10000 20000 35000 50000 --out STEP1B_RESULTS.txt

Run once with PYTHONPATH pointing at the frozen baseline source ("before") and once at the
new code ("after"). Per library size N (files): the library is bulk-pre-filled to N files x
--chunks-per-file synthetic chunks (FTS5 rows written directly, Lance in 50k-row fragments,
then one `maintain()` as a finished earlier run would leave it), then --measure new files are
written through the real writers exactly as `IndexManager.upsert_chunks` does: FTS and vector
existence checks, FTS upsert, vector upsert. Also times the re-ingest path (delete of one
file's chunks from both indexes). Reported: mean seconds per file for each step.

Caveats: vectors are --dim wide (default 64, not 1024) to keep disk sane; pre-fill is bulk so
Lance fragment/version growth from per-file writes is NOT reproduced here (see the separate
--incremental mode, which writes every file individually and reports fragment/version counts).
Numbers move with CPU load. Results are appended to --out as they are produced.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path

os.environ.setdefault("DOCKET_ANN_MIN_ROWS", "100000000")  # no ANN build: vector search is not measured

import numpy as np
import pyarrow as pa
from sqlalchemy import text

import docket
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.vector_index import LanceIndexWriter

VOCAB = [f"w{i}" for i in range(6000)]


def make_text(rng: random.Random) -> str:
    return " ".join(rng.choice(VOCAB) for _ in range(40))


def make_records(file_no: int, n: int, rng: random.Random) -> list[ChunkRecord]:
    return [
        ChunkRecord(
            chunk_id=f"chk_{file_no:07d}_{i:03d}",
            source_id="src_main",
            evidence_version_id=f"ver_{file_no:07d}",
            evidence_unit_id="eu",
            chunk_recipe_id="rcp",
            ordinal=i,
            heading=None,
            text=make_text(rng),
            content_hash=f"h{file_no}_{i}",
        )
        for i in range(n)
    ]


def make_engine(work: Path):
    from alembic import command
    from alembic.config import Config

    from docket.core.db.engine import get_engine

    import docket.core.db as dbpkg

    sqlite_path = work / "docket.sqlite3"
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(dbpkg.__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    command.upgrade(cfg, "head")
    return get_engine(sqlite_path)


def prefill(engine, lance_dir: Path, first: int, last: int, cpf: int, dim: int, lw: LanceIndexWriter):
    """Bulk-add files [first, last)."""
    import lancedb

    rng = random.Random(first)
    with engine.begin() as conn:
        batch = []
        for f in range(first, last):
            for r in make_records(f, cpf, rng):
                batch.append(
                    {"chunk_id": r.chunk_id, "text": r.text, "evidence_version_id": r.evidence_version_id,
                     "source_id": r.source_id}
                )
            if len(batch) >= 20000:
                conn.execute(
                    text("INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) "
                         "VALUES (:chunk_id, :text, :evidence_version_id, :source_id)"), batch)
                batch = []
        if batch:
            conn.execute(
                text("INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) "
                     "VALUES (:chunk_id, :text, :evidence_version_id, :source_id)"), batch)
        try:  # new code only: keep the rowid map in step with the bulk insert
            from docket.infra.index.fts_index import rebuild_fts_chunk_map

            rebuild_fts_chunk_map(conn)
        except ImportError:
            pass

    nprng = np.random.default_rng(first)
    db = lancedb.connect(str(lance_dir))

    def gen():
        for s in range(first, last, 2000):
            e = min(last, s + 2000)
            n = (e - s) * cpf
            ids = [f"chk_{f:07d}_{i:03d}" for f in range(s, e) for i in range(cpf)]
            vers = [f"ver_{f:07d}" for f in range(s, e) for _ in range(cpf)]
            vec = nprng.standard_normal((n, dim), dtype=np.float32)
            yield pa.RecordBatch.from_arrays(
                [pa.array(ids), pa.array(["src_main"] * n), pa.array([""] * n),
                 pa.FixedSizeListArray.from_arrays(pa.array(vec.ravel()), dim), pa.array(vers)],
                names=["chunk_id", "source_id", "text", "vector", "evidence_version_id"])

    schema = next(gen()).schema
    if "chunks" in db.table_names():
        db.open_table("chunks").add(gen())
    else:
        db.create_table("chunks", data=gen(), schema=schema)
    lw.maintain(compact=True)


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return time.perf_counter() - t, out


def measure(fw, lw, first_file: int, nfiles: int, cpf: int, dim: int):
    rng = random.Random(first_file * 7)
    nprng = np.random.default_rng(first_file)
    acc = {"fts_exist": 0.0, "vec_exist": 0.0, "fts_write": 0.0, "vec_write": 0.0, "delete": 0.0}
    for f in range(first_file, first_file + nfiles):
        recs = make_records(f, cpf, rng)
        ids = [r.chunk_id for r in recs]
        emb = nprng.standard_normal((cpf, dim), dtype=np.float32).tolist()
        acc["fts_exist"] += timed(lambda: fw.existing_chunk_ids(ids))[0]
        acc["vec_exist"] += timed(lambda: lw.existing_chunk_ids(ids))[0]
        acc["fts_write"] += timed(lambda: fw.upsert(recs, emb))[0]
        acc["vec_write"] += timed(lambda: lw.upsert(recs, emb))[0]
    for f in range(first_file, first_file + min(nfiles, 10)):  # re-ingest path
        ids = [f"chk_{f:07d}_{i:03d}" for i in range(cpf)]
        acc["delete"] += timed(lambda: (fw.delete(ids), lw.delete(ids)))[0] * nfiles / min(nfiles, 10)
    per = {k: v / nfiles for k, v in acc.items()}
    per["total_write"] = per["fts_exist"] + per["vec_exist"] + per["fts_write"] + per["vec_write"]
    return per


def lance_stats(lance_dir: Path) -> dict:
    import lancedb

    t = lancedb.connect(str(lance_dir)).open_table("chunks")
    vdir = lance_dir / "chunks.lance" / "_versions"
    return {"fragments": len(t.to_lance().get_fragments()), "versions": len(list(vdir.glob("*"))) if vdir.exists() else None,
            "manifest_mb": round(sum(p.stat().st_size for p in vdir.glob("*")) / 1e6, 1) if vdir.exists() else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000, 10000, 20000, 35000, 50000])
    ap.add_argument("--chunks-per-file", type=int, default=26)
    ap.add_argument("--measure", type=int, default=30)
    ap.add_argument("--dim", type=int, default=64)
    ap.add_argument("--out", default="STEP1B_RESULTS.txt")
    ap.add_argument("--incremental", type=int, default=0, help="instead: write this many files one by one, report windows")
    a = ap.parse_args()
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    engine = make_engine(work)
    lance_dir = work / "lance"
    fw = FtsIndexWriter(engine)
    lw = LanceIndexWriter(lance_dir, engine=engine)

    def emit(row):
        row.update(label=a.label, code=str(Path(docket.__file__).parent))
        with open(a.out, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    if a.incremental:
        rng = random.Random(1)
        nprng = np.random.default_rng(1)
        window = max(1, a.incremental // 10)
        t_win = time.perf_counter()
        for f in range(a.incremental):
            recs = make_records(f, a.chunks_per_file, rng)
            ids = [r.chunk_id for r in recs]
            emb = nprng.standard_normal((len(recs), a.dim), dtype=np.float32).tolist()
            fw.existing_chunk_ids(ids); lw.existing_chunk_ids(ids)
            fw.upsert(recs, emb); lw.upsert(recs, emb)
            if hasattr(lw, "maintain") and hasattr(__import__("docket.infra.index.tuning", fromlist=["x"]), "compact_every_files"):
                from docket.infra.index import tuning
                every = tuning.compact_every_files()
                if every and (f + 1) % every == 0:
                    lw.maintain(compact=True)
            if (f + 1) % window == 0:
                emit({"mode": "incremental", "files": f + 1, "sec_per_file": round((time.perf_counter() - t_win) / window, 5),
                      **lance_stats(lance_dir)})
                t_win = time.perf_counter()
        return

    done = 0
    for n in a.sizes:
        prefill(engine, lance_dir, done, n, a.chunks_per_file, a.dim, lw)
        done = n
        per = measure(fw, lw, n, a.measure, a.chunks_per_file, a.dim)
        done = n + a.measure
        emit({"mode": "size", "files": n, **{k: round(v, 5) for k, v in per.items()}, **lance_stats(lance_dir)})


if __name__ == "__main__":
    main()
