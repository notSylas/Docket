"""Ingestion + storage benchmark. Measurement only: nothing under backend/src is changed.

    backend/.venv/bin/python bench_ingest.py FOLDER --data-dir FRESH_DIR --out result.json
        [--no-page-images] [--ask "question" --ask-after-files N] [--limit N]

Ingests FOLDER (as one local-folder source) into FRESH_DIR (must not exist or be empty; NEVER the real
~/.local/share/docket) through the real services (AppContext.for_testing -> IngestionPipeline with the real
DoclingParser and OllamaGateway) with per-stage timers installed by monkeypatching the service classes in
this process only. Reports per-stage seconds by file format, per-file rows, and bytes on disk per store.

Stages: discovery, evidence_copy_hash (EvidenceManager.ingest_file), parse (Docling / openpyxl / python-pptx /
text), formula_regions, page_images_store, chunk, sqlite_chunks_write, index_existing_check, embed (Ollama),
fts_write, lance_write; `other` = per-file wall minus the sum of the above.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_BACKEND = HERE.parent.parent

STAGES = ["discovery", "evidence_copy_hash", "parse", "formula_regions", "page_images_store", "chunk",
          "sqlite_chunks_write", "index_existing_check", "embed", "fts_write", "lance_write"]

tot: dict[tuple[str, str], float] = defaultdict(float)
cnt: dict[tuple[str, str], int] = defaultdict(int)
cur = {"ext": "-", "pages": 0}
file_stage: dict[str, float] = defaultdict(float)  # per current file


def timed(stage: str, fn, count_fn=None):
    def w(*a, **k):
        t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            dt = time.perf_counter() - t0
            key = (stage, cur["ext"])
            tot[key] += dt
            cnt[key] += count_fn(a, k) if count_fn else 1
            file_stage[stage] += dt
    return w


def install_patches(ctx_cls_modules: dict) -> None:
    from docket.infra.evidence import manager as ev_mgr
    from docket.infra.index import fts_index, manager as idx_mgr, vector_index
    from docket.infra.parsing import docling_wrapper, pptx_wrapper, xlsx_wrapper, text_family
    from docket.services.ingestion import chunk_writer, formula_transcriber, pipeline, visual_indexer

    pipeline.discover_files = timed("discovery", pipeline.discover_files)
    ev_mgr.EvidenceManager.ingest_file = timed("evidence_copy_hash", ev_mgr.EvidenceManager.ingest_file)

    def parse_wrap(cls):
        orig = cls.parse

        def parse(self, *a, **k):
            t0 = time.perf_counter()
            r = orig(self, *a, **k)
            dt = time.perf_counter() - t0
            key = ("parse", cur["ext"])
            tot[key] += dt
            cnt[key] += 1
            file_stage["parse"] += dt
            tw = getattr(r, "text_with_page_markers", None)
            if tw:
                pages = [int(x) for x in re.findall(r"<!--PAGE:(\d+)-->", tw)]
                cur["pages"] = max(pages) if pages else 0
            return r
        cls.parse = parse

    for cls in (docling_wrapper.DoclingParser, xlsx_wrapper.XlsxParser, pptx_wrapper.PptxParser,
                text_family.TextFamilyParser):
        parse_wrap(cls)
    formula_transcriber.FormulaTranscriber.save_regions = timed(
        "formula_regions", formula_transcriber.FormulaTranscriber.save_regions)
    visual_indexer.VisualIndexer.save_page_images = timed(
        "page_images_store", visual_indexer.VisualIndexer.save_page_images)
    for name in ("chunk_document", "chunk_workbook", "chunk_presentation", "chunk_text", "chunk_mail"):
        setattr(pipeline, name, timed("chunk", getattr(pipeline, name)))
    chunk_writer.ChunkWriter.persist_units_and_chunks = timed(
        "sqlite_chunks_write", chunk_writer.ChunkWriter.persist_units_and_chunks)
    idx_mgr.embed_texts = timed("embed", idx_mgr.embed_texts, lambda a, k: len(a[1]) if len(a) > 1 else len(k["texts"]))
    fts_index.FtsIndexWriter.upsert = timed("fts_write", fts_index.FtsIndexWriter.upsert,
                                            lambda a, k: len(a[1]))
    vector_index.LanceIndexWriter.upsert = timed("lance_write", vector_index.LanceIndexWriter.upsert,
                                                 lambda a, k: len(a[1]))
    for cls in (fts_index.FtsIndexWriter, vector_index.LanceIndexWriter):
        cls.existing_chunk_ids = timed("index_existing_check", cls.existing_chunk_ids)


def du(path: Path) -> int:
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def disk_report(data_dir: Path, raw_bytes: int) -> dict:
    sqlite_path = data_dir / "docket.sqlite3" if (data_dir / "docket.sqlite3").exists() else None
    out: dict = {"data_dir_total_bytes": du(data_dir), "raw_corpus_bytes": raw_bytes, "top_level": {}}
    for p in sorted(data_dir.iterdir()):
        out["top_level"][p.name] = du(p) if p.is_dir() else p.stat().st_size
    if sqlite_path is None:
        return out
    out["sqlite_file"] = str(sqlite_path.name)
    out["sqlite_bytes_incl_wal"] = sum(f.stat().st_size for f in data_dir.glob(sqlite_path.name + "*"))
    con = sqlite3.connect(str(sqlite_path))
    try:
        rows = con.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
        by = {n: s for n, s in rows}
        fts = sum(s for n, s in by.items() if n.startswith("chunks_fts") or "fts" in n.lower())
        out["sqlite_dbstat_by_object_top"] = dict(sorted(by.items(), key=lambda kv: -kv[1])[:12])
        out["sqlite_fts5_bytes"] = fts
        out["sqlite_chunks_table_bytes"] = by.get("chunks")
        out["sqlite_non_fts_non_chunks_bytes"] = sum(by.values()) - fts - (by.get("chunks") or 0)
        out["n_chunks"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        out["chunk_text_bytes"] = con.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks").fetchone()[0]
        out["n_evidence_versions"] = con.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0]
        out["n_evidence_versions_ready"] = con.execute(
            "SELECT COUNT(*) FROM evidence_versions WHERE status='READY'").fetchone()[0]
        # originals vs page images vs everything else inside the evidence store
        orig = {r[0] for r in con.execute("SELECT DISTINCT content_hash FROM evidence_versions")}
        imgs: set[str] = set()
        for (j,) in con.execute("SELECT page_images_json FROM evidence_versions WHERE page_images_json IS NOT NULL"):
            try:
                v = json.loads(j)
                imgs |= set(v.values() if isinstance(v, dict) else v)
            except Exception:
                pass
        ev = data_dir / "evidence_store" if (data_dir / "evidence_store").exists() else None
        if ev is None:
            cands = [p for p in data_dir.iterdir() if p.is_dir() and "evidence" in p.name]
            ev = cands[0] if cands else None
        if ev is not None:
            b_orig = b_img = b_other = n_orig = n_img = 0
            for root, _d, files in os.walk(ev / "objects"):
                for f in files:
                    sz = os.lstat(os.path.join(root, f)).st_size
                    if f in orig:
                        b_orig += sz; n_orig += 1
                    elif f in imgs:
                        b_img += sz; n_img += 1
                    else:
                        b_other += sz
            out["evidence_store"] = {"originals_bytes": b_orig, "originals_objects": n_orig,
                                     "page_png_bytes": b_img, "page_png_objects": n_img,
                                     "other_object_bytes": b_other,
                                     "manifests_bytes": du(ev / "manifests") if (ev / "manifests").exists() else 0,
                                     "total_bytes": du(ev)}
        # per extension chunks
        per_ext = defaultdict(lambda: [0, 0])
        for (fp, n, tb) in con.execute(
                "SELECT ev.file_path, COUNT(c.id), COALESCE(SUM(LENGTH(c.text)),0) FROM chunks c "
                "JOIN evidence_versions ev ON ev.id=c.evidence_version_id GROUP BY ev.file_path"):
            e = (fp or "").rsplit(".", 1)[-1].lower()
            per_ext[e][0] += n
            per_ext[e][1] += tb
        out["chunks_by_ext"] = {k: {"chunks": v[0], "chunk_text_bytes": v[1]} for k, v in per_ext.items()}
    finally:
        con.close()
    ld = data_dir / "lancedb"
    if ld.exists():
        out["lancedb_bytes"] = du(ld)
        try:
            import lancedb
            t = lancedb.connect(str(ld)).open_table("chunks")
            out["lancedb_rows"] = t.count_rows()
            stats = t.stats() if hasattr(t, "stats") else None
            out["lancedb_stats"] = json.loads(json.dumps(stats, default=lambda o: getattr(o, "__dict__", str(o)))) if stats else None
            out["lancedb_n_versions"] = len(t.list_versions())
            out["lancedb_data_files"] = sum(1 for _ in (ld / "chunks.lance" / "data").glob("*.lance"))
        except Exception as e:  # noqa: BLE001
            out["lancedb_inspect_error"] = repr(e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-page-images", action="store_true")
    ap.add_argument("--ask", type=str, default=None)
    ap.add_argument("--ask-after-files", type=int, default=0, help="issue --ask once this many files are done")
    ap.add_argument("--label", type=str, default="")
    a = ap.parse_args()

    real = Path.home() / ".local/share/docket"
    assert not str(a.data_dir.resolve()).startswith(str(real)), "refusing to touch the real data dir"
    if a.data_dir.exists() and any(a.data_dir.iterdir()):
        sys.exit(f"{a.data_dir} is not empty")
    a.data_dir.mkdir(parents=True, exist_ok=True)

    from docket.interfaces.cli.context import AppContext

    install_patches({})
    ctx = AppContext.for_testing(data_dir=a.data_dir)
    if a.no_page_images:
        from docling.datamodel.base_models import InputFormat
        fo = ctx.parser._converter.format_to_options[InputFormat.PDF]
        fo.pipeline_options.generate_page_images = False

    raw_bytes = sum(p.stat().st_size for p in a.folder.rglob("*") if p.is_file())
    n_files_raw = sum(1 for p in a.folder.rglob("*") if p.is_file())
    t_start = time.perf_counter()
    source = ctx.source_manager.register_source(a.folder)

    files: list[dict] = []
    state = {"t0": 0.0, "done": 0, "asked": None}
    ask_results: list[dict] = []

    def ask(label: str) -> None:
        env = dict(os.environ, DOCKET_DATA_DIR=str(a.data_dir))
        t0 = time.perf_counter()
        p = subprocess.run([str(REPO_BACKEND / ".venv/bin/docket"), "query", a.ask], env=env,
                           capture_output=True, text=True, timeout=1800)
        ask_results.append({"label": label, "wall_s": round(time.perf_counter() - t0, 2), "rc": p.returncode,
                            "answer_head": (p.stdout or p.stderr)[:300]})

    def progress(*args) -> None:
        ev = args[0] if len(args) == 1 else args[-1]
        if getattr(ev, "kind", None) == "start":
            cur["ext"] = ev.path.suffix.lower().lstrip(".")
            cur["pages"] = 0
            file_stage.clear()
            state["t0"] = time.perf_counter()
        elif getattr(ev, "kind", None) == "done":
            wall = time.perf_counter() - state["t0"]
            r = ev.result
            fs = dict(file_stage)
            files.append({"path": str(ev.path.relative_to(a.folder)), "ext": cur["ext"],
                          "bytes": ev.path.stat().st_size if ev.path.exists() else 0, "status": r.status,
                          "chunks": r.chunks_written, "pages": cur["pages"], "wall_s": round(wall, 4),
                          "stages": {k: round(v, 4) for k, v in fs.items()}, "error": (r.error or "")[:200]})
            state["done"] += 1
            if state["done"] % 200 == 0:
                print(f"[{time.strftime('%H:%M:%S')}] {state['done']}/{ev.total} files, "
                      f"{time.perf_counter() - t_start:.0f}s", flush=True)
            if a.ask and a.ask_after_files and state["done"] == a.ask_after_files and state["asked"] is None:
                state["asked"] = threading.Thread(target=ask, args=("during_ingestion",))
                state["asked"].start()

    t_ing = time.perf_counter()
    result = ctx.pipeline.run_ingestion_for_source(source.id, progress)
    ingest_wall = time.perf_counter() - t_ing
    try:
        ctx.engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    if state["asked"] is not None:
        state["asked"].join()
    total_wall = time.perf_counter() - t_start

    if a.ask:
        ask("idle_after_ingestion_cold_cli")
        ask("idle_after_ingestion_repeat")

    by_ext: dict[str, dict] = {}
    for f in files:
        e = by_ext.setdefault(f["ext"], {"files": 0, "failed": 0, "bytes": 0, "chunks": 0, "pages": 0, "wall_s": 0.0})
        e["files"] += 1
        e["failed"] += f["status"] == "failed"
        e["bytes"] += f["bytes"]
        e["chunks"] += f["chunks"]
        e["pages"] += f["pages"]
        e["wall_s"] += f["wall_s"]
    stage_by_ext: dict[str, dict] = defaultdict(dict)
    for (st, ext), v in tot.items():
        stage_by_ext[ext][st] = {"seconds": round(v, 3), "units": cnt[(st, ext)]}
    for ext, e in by_ext.items():
        accounted = sum(v["seconds"] for k, v in stage_by_ext[ext].items() if k != "discovery")
        e["other_s"] = round(e["wall_s"] - accounted, 3)
        e["wall_s"] = round(e["wall_s"], 2)
        e["chunks_per_s_overall"] = round(e["chunks"] / e["wall_s"], 2) if e["wall_s"] else None
        e["MB_per_s_overall"] = round(e["bytes"] / 1e6 / e["wall_s"], 3) if e["wall_s"] else None
        e["chunks_per_GB_source"] = round(e["chunks"] / (e["bytes"] / 1e9)) if e["bytes"] else None
        p = stage_by_ext[ext].get("parse")
        if e["pages"] and p:
            e["parse_pages_per_s"] = round(e["pages"] / p["seconds"], 3)
        emb = stage_by_ext[ext].get("embed")
        if emb and emb["seconds"]:
            e["embed_chunks_per_s"] = round(emb["units"] / emb["seconds"], 1)

    out = {
        "label": a.label, "folder": str(a.folder), "n_files_in_folder": n_files_raw, "raw_bytes": raw_bytes,
        "page_images_enabled": not a.no_page_images,
        "ingest_wall_s": round(ingest_wall, 1), "total_wall_s_incl_register": round(total_wall, 1),
        "files_processed": result.files_processed, "files_failed": result.files_failed,
        "dirs_skipped": result.dirs_skipped, "files_skipped": result.files_skipped,
        "by_ext": by_ext, "stage_by_ext": stage_by_ext,
        "peak_rss_MB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "ask": ask_results, "disk": disk_report(a.data_dir, raw_bytes),
        "failures_sample": [f for f in files if f["status"] == "failed"][:15],
    }
    a.out.write_text(json.dumps(out, indent=1, default=str))
    (a.out.with_suffix(".files.jsonl")).write_text("\n".join(json.dumps(f) for f in files))
    print(json.dumps({k: out[k] for k in ("ingest_wall_s", "files_processed", "files_failed", "peak_rss_MB")}))


if __name__ == "__main__":
    main()
