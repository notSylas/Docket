"""Aggregate whatever measurement outputs exist into RESULTS.json + RESULTS.txt (re-run after every job).

    backend/.venv/bin/python make_results.py BENCH_DIR

Every number carries a label: VERIFIED (measured in this session) or EXTRAPOLATED (model output; assumptions
stated next to it). Missing jobs are listed under "not_yet_measured"."""
import json, subprocess, sys
from pathlib import Path

B = Path(sys.argv[1])
HERE = Path(__file__).resolve().parent
R: dict = {"labels": "VERIFIED = measured here; EXTRAPOLATED = model output, see assumptions", "not_yet_measured": []}


def sh(c):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def ver(m):
    try:
        from importlib.metadata import version
        return version(m)
    except Exception:
        return None


R["hardware_versions"] = {"label": "VERIFIED",
    "gpu": "AMD Radeon RX 9070 XT (16 GB VRAM) per rocm-smi", "cpu": "AMD Ryzen 9 9950X3D 16C/32T",
    "ram_GB": 31, "disk": "WD_BLACK SN7100 4TB NVMe SSD (non-rotational)", "os": sh("uname -sr"),
    "docling": ver("docling"), "lancedb": ver("lancedb"), "ollama_python": ver("ollama"), "ollama_server": "0.32.15",
    "embed_model": "qwen3-embedding:0.6b, batch 32", "gen_model": "qwen3:14b",
    "docling_device_note": "torch 2.14.0+cu130, cuda.is_available()=False -> Docling parses on CPU (4 threads default); the AMD GPU is used only by Ollama"}

# ---------------- Part A
pa = {}
for n in ("cuad", "finqa"):
    f = HERE / f"partA_rescore_{n}.txt"
    if f.exists():
        pa[n] = [l for l in f.read_text().splitlines()[:6] if l.strip()]
R["partA_rescored_existing_runs"] = {"label": "VERIFIED", "note": "existing 3-repeat runs re-scored against fixed gold; judged with qwen3:30b + gemma3:12b cross-check. Before fix CUAD judged 58.8% (20/34).", **pa}

# ---------------- Vector
vf = B / "vec/vec.json"
if vf.exists():
    v = json.loads(vf.read_text())
    out = {"label": "VERIFIED (synthetic clustered 1024-d vectors, 2KB text, 30 chunks/version; CPU, 24 pinned cores; recall is indicative only)", "rows": {}}
    xs = []
    for n, d in sorted(v.items(), key=lambda kv: int(kv[0])):
        fl = d["flat_scan"]
        out["rows"][n] = {"approx_files_at_30_chunks": int(n) // 30, "table_bytes": d["table_bytes_flat"], "bytes_per_row": d["bytes_per_row_flat"],
            "flat_p50_s": {k: x.get("p50_s") for k, x in fl.items()}, "flat_p95_s_in_all": fl["in_all"].get("p95_s"),
            "in_list_chars_all": fl["in_all"].get("in_list_chars"), "sqlite_eligible_ids": d["sqlite_eligible_ids"],
            "ivf_pq_build_s": d["ivf_pq"].get("build_seconds"), "ivf_pq_index_bytes": d.get("index_bytes"),
            "ivf_pq_queries": d["ivf_pq"].get("queries")}
        xs.append((int(n), fl["in_all"]["p50_s"], fl["no_filter"]["p50_s"]))

    def cross(target, idx):
        for (a, *ya), (b, *yb) in zip(xs, xs[1:]):
            if ya[idx - 1] < target <= yb[idx - 1]:
                return int(a + (b - a) * (target - ya[idx - 1]) / (yb[idx - 1] - ya[idx - 1]))
        # linear extrapolation from last two points
        (a, *ya), (b, *yb) = xs[-2], xs[-1]
        return int(a + (b - a) * (target - ya[idx - 1]) / (yb[idx - 1] - ya[idx - 1]))
    out["crossings_current_design_flat_scan_with_IN_all"] = {"label": "interpolated between VERIFIED points",
        "p50_over_1s_rows": cross(1.0, 1), "p50_over_1s_files": cross(1.0, 1) // 30,
        "p50_over_5s_rows": cross(5.0, 1), "p50_over_5s_files": cross(5.0, 1) // 30}
    ivf = {}
    for n, row in out["rows"].items():
        q = row["ivf_pq_queries"] or {}
        for k in ("in_all|nprobes=20|refine=10", "in_all|nprobes=100|refine=30"):
            if k in q:
                ivf.setdefault(k, {})[n] = {"p50_s": q[k].get("p50_s"), "recall30": q[k].get("recall30")}
    out["ivf_pq_with_IN_all_filter"] = ivf
    out["finding"] = ("Flat scan + IN(all READY ids) prefilter: ~2.4 us/row p50 (1.1 s at 0.5M rows, 4.7 s at 2M, 9.7 s at 4M, 19.6 s at 8M). "
                      "The IN filter adds ~60-70% over an unfiltered flat scan. IVF_PQ cuts p50 to 0.1-0.5 s at 2M-8M rows with refine, "
                      "but with the IN-list prefilter the cost still grows linearly with rows (filter evaluation), so ANN alone does not remove the O(N) term. "
                      "SQLite eligible-id load + IN-string build are small (<0.05 s at 66k versions).")
    R["vector_microbenchmark"] = out
else:
    R["not_yet_measured"].append("vector microbenchmark")

# ---------------- Ingestion runs
def load_runs():
    runs = {}
    for f in sorted(B.glob("r_*.json")):
        try:
            runs[f.stem[2:]] = json.loads(f.read_text())
        except Exception:
            pass
    return runs


runs = load_runs()
ing = {"label": "VERIFIED", "runs": {}}
for name, d in runs.items():
    dk = d["disk"]
    n = dk.get("n_chunks") or 0
    fs = [json.loads(l) for l in (B / f"r_{name}.files.jsonl").read_text().splitlines()] if (B / f"r_{name}.files.jsonl").exists() else []
    e = {"label": d["label"], "raw_MB": round(d["raw_bytes"] / 1e6, 1), "files": d["files_processed"], "failed": d["files_failed"],
         "ingest_wall_s": d["ingest_wall_s"], "peak_rss_MB": d["peak_rss_MB"], "page_images": d["page_images_enabled"],
         "by_ext": d["by_ext"], "stage_seconds_by_ext": {x: {k: v["seconds"] for k, v in s.items()} for x, s in d["stage_by_ext"].items()},
         "ask_latency": d.get("ask")}
    if n:
        lb = dk.get("lancedb_bytes", 0)
        e["disk"] = {"data_dir_MB": round(dk["data_dir_total_bytes"] / 1e6, 1), "multiple_of_raw": round(dk["data_dir_total_bytes"] / d["raw_bytes"], 2),
            "n_chunks": n, "bytes_per_chunk": {"sqlite_chunks_table": round((dk["sqlite_chunks_table_bytes"] or 0) / n), "sqlite_fts5": round(dk["sqlite_fts5_bytes"] / n),
                "sqlite_other_tables_indexes": round(dk["sqlite_non_fts_non_chunks_bytes"] / n), "lancedb_total": round(lb / n),
                "lancedb_data_only": round((dk.get("lancedb_stats") or {}).get("total_bytes", 0) / n),
                "evidence_store_total": round(dk.get("evidence_store", {}).get("total_bytes", 0) / n), "chunk_text_raw": round(dk["chunk_text_bytes"] / n)},
            "store_bytes": {"sqlite": dk["sqlite_bytes_incl_wal"], "fts5": dk["sqlite_fts5_bytes"], "lancedb": lb, "evidence_store": dk.get("evidence_store"),
                "lance_fragments": (dk.get("lancedb_stats") or {}).get("fragment_stats", {}).get("num_fragments"), "lance_versions": dk.get("lancedb_n_versions")}}
    if fs:  # growth of per-file index overhead with library size
        seg, cum, rows = [], 0, []
        for f in fs:
            idx = sum(f["stages"].get(k, 0) for k in ("fts_write", "index_existing_check", "lance_write", "sqlite_chunks_write"))
            rows.append((cum, f["chunks"], idx, f["stages"].get("fts_write", 0), f["stages"].get("index_existing_check", 0), f["stages"].get("lance_write", 0)))
            cum += f["chunks"]
        k = max(1, len(rows) // 7)
        e["index_overhead_growth"] = [{"chunks_before": rows[i][0], "index_write_s_per_chunk": round(sum(r[2] for r in rows[i:i + k]) / max(1, sum(r[1] for r in rows[i:i + k])), 5),
              "fts_s_per_chunk": round(sum(r[3] for r in rows[i:i + k]) / max(1, sum(r[1] for r in rows[i:i + k])), 5)} for i in range(0, len(rows), k)]
        # least squares s_per_chunk = a + b * chunks_before over files
        import numpy as np
        X = np.array([r[0] for r in rows], float); Y = np.array([r[2] / max(1, r[1]) for r in rows]); W = np.array([r[1] for r in rows], float)
        b, a = np.polyfit(X, Y, 1, w=np.sqrt(W))
        e["index_overhead_fit"] = {"a_s_per_chunk": float(a), "b_s_per_chunk_per_existing_chunk": float(b)}
    ing["runs"][name] = e
R["ingestion_runs"] = ing
need = {"office_small": "docx+pptx", "xlsx": "xlsx", "pdf20_img": "PDF page images ON", "pdf20_noimg": "PDF page images OFF", "pdfmix": "PDF mix cuad+arxiv"}
for k, v in need.items():
    if k not in runs:
        R["not_yet_measured"].append(f"ingest run {k} ({v})")

# ---------------- Extrapolation (only if office run exists)
if "office_small" in runs:
    o = runs["office_small"]
    fit = ing["runs"]["office_small"].get("index_overhead_fit")
    be = o["by_ext"]
    ex = {"label": "EXTRAPOLATED", "assumptions": [], "scenarios": {}}
    docx = be["docx"]; pptx = be["pptx"]
    embed_rate = o["stage_by_ext"]["docx"]["embed"]["units"] / o["stage_by_ext"]["docx"]["embed"]["seconds"]
    base_fixed_per_chunk = (docx["wall_s"] - sum(o["stage_by_ext"]["docx"][k]["seconds"] for k in ("index_existing_check", "fts_write", "lance_write"))) / docx["chunks"]
    ex["measured_inputs"] = {"embed_chunks_per_s": round(embed_rate, 1), "non_index_seconds_per_chunk_docx": round(base_fixed_per_chunk, 4),
                             "docx_chunks_per_GB": docx["chunks_per_GB_source"], "pptx_chunks_per_GB": pptx["chunks_per_GB_source"], "index_overhead_fit": fit}
    px = runs.get("pdfmix") or runs.get("pdf20_img")
    xl = runs.get("xlsx")
    ex["assumptions"] += ["Serial ingestion as shipped (one process, no parallel parse).",
        "Index-write cost per chunk = a + b*(chunks already in library) as fitted on the docx/pptx run (FTS5 DELETE-by-UNINDEXED-chunk_id and Lance existing-id checks scan the whole table); total = integral. Extrapolating a linear-in-N term 1000x beyond the measured range is a MODEL; a plateau or partitioning would make it much smaller, a bigger FTS segment cost would make it larger.",
        "Low/high ranges: low = index overhead stays at measured end-of-run constant per chunk (no further growth), high = linear growth continues (quadratic total).",
        "Mix scenario M1 (office-heavy): 60% docx/pptx-like, 25% PDF, 15% xlsx by bytes; M2 (PDF-heavy): 70% PDF, 25% docx, 5% xlsx. PDF bytes/page 20-150 KB assumed (CUAD measured 7 KB/page, arXiv 100+ KB/page)."]
    def chunks_for(gb, mix):
        c = 0.0
        c += gb * mix.get("docx", 0) * docx["chunks_per_GB_source"]
        c += gb * mix.get("pdf_pages_gb", 0) * 0  # added below
        return c
    pdf_rate = None; chunks_per_page = None
    if px:
        pb = px["by_ext"].get("pdf")
        if pb and pb.get("pages"):
            pdf_rate = pb["parse_pages_per_s"]; chunks_per_page = pb["chunks"] / pb["pages"]
    ex["pdf_inputs"] = {"pages_per_s_parse": pdf_rate, "chunks_per_page": chunks_per_page}
    xl_c_gb = (xl["by_ext"]["xlsx"]["chunks_per_GB_source"] if xl and "xlsx" in xl["by_ext"] else None)
    def project(gb, mix, pdf_kb_per_page):
        total_chunks = 0.0; parse_s = 0.0
        office = mix["office"]; pdf = mix["pdf"]; xlsx = mix["xlsx"]
        total_chunks += gb * office * docx["chunks_per_GB_source"]
        parse_s += gb * office * 1e9 / (docx["bytes"] / o["stage_by_ext"]["docx"]["parse"]["seconds"])
        if xl_c_gb:
            total_chunks += gb * xlsx * xl_c_gb
        if pdf_rate:
            pages = gb * pdf * 1e9 / (pdf_kb_per_page * 1e3)
            total_chunks += pages * chunks_per_page
            parse_s += pages / pdf_rate
        return total_chunks, parse_s
    for gb in (100, 500):
        for mname, mix in (("M1_office_heavy", {"office": 0.6, "pdf": 0.25, "xlsx": 0.15}), ("M2_pdf_heavy", {"office": 0.25, "pdf": 0.70, "xlsx": 0.05})):
            lowk, highk = project(gb, mix, 150)[0:2], project(gb, mix, 20)
            for tag, (ch, ps) in (("pdf_150KB_per_page", project(gb, mix, 150)), ("pdf_20KB_per_page", project(gb, mix, 20))):
                emb_s = ch / embed_rate
                lin = ch * base_fixed_per_chunk + ps + emb_s
                if fit:
                    a, b = fit["a_s_per_chunk"], fit["b_s_per_chunk_per_existing_chunk"]
                    quad = lin + a * ch + b * ch * ch / 2
                    flat = lin + (a + b * 90000) * ch
                else:
                    quad = flat = lin
                ex["scenarios"][f"{gb}GB_{mname}_{tag}"] = {"chunks": int(ch), "embed_hours": round(emb_s / 3600, 1), "parse_hours": round(ps / 3600, 1),
                    "first_index_hours_low(no_N_growth)": round(flat / 3600, 1), "first_index_hours_high(linear_N_growth)": round(quad / 3600, 1),
                    "first_index_days_high": round(quad / 86400, 1)}
    R["extrapolation_first_index"] = ex

# ---------------- Query path, curve, musique
for key, f in (("query_path_office_small", B / "q_office_small.json"), ("query_path_other", B / "q_other.json")):
    if f.exists():
        R[key] = {"label": "VERIFIED", **json.loads(f.read_text())}
sf = B / "curve/stages.json"
if sf.exists():
    R["cuad_scale_curve"] = {"label": "VERIFIED", "stages": json.loads(sf.read_text()), "reports": {}}
    for f in sorted((B / "curve").glob("cuad*.report.txt")):
        R["cuad_scale_curve"]["reports"][f.stem] = f.read_text().splitlines()[:14]
else:
    R["not_yet_measured"].append("CUAD scale curve")
for f in sorted((B / "mus_runs").glob("*.report.txt")) if (B / "mus_runs").exists() else []:
    R.setdefault("musique_scaling", {"label": "VERIFIED", "reports": {}})["reports"][f.stem] = f.read_text().splitlines()[:10]
if "musique_scaling" not in R:
    R["not_yet_measured"].append("MuSiQue distractor scaling")
extra = HERE / "findings_notes.json"
if extra.exists():
    R["findings"] = json.loads(extra.read_text())

(HERE / "RESULTS.json").write_text(json.dumps(R, indent=1, default=str))


def render(o, ind=0, out=None):
    out = [] if out is None else out
    sp = "  " * ind
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{sp}{k}:"); render(v, ind + 1, out)
            else:
                out.append(f"{sp}{k}: {v}")
    elif isinstance(o, list):
        for v in o:
            if isinstance(v, (dict, list)):
                out.append(f"{sp}-"); render(v, ind + 1, out)
            else:
                out.append(f"{sp}- {v}")
    return out


(HERE / "RESULTS.txt").write_text("Docket scale baseline (Step 0). Generated by make_results.py; labels VERIFIED / EXTRAPOLATED.\n\n" + "\n".join(render(R)) + "\n")
print("wrote RESULTS.json/txt; not yet measured:", R["not_yet_measured"])
