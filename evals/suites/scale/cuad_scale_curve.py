"""CUAD accuracy-vs-library-size curve. Same code path as `docket eval run` (EvalRunner.ingest + .run), but with
ONE persistent scratch data dir that grows between stages, so the 12 gold PDFs are Docling-parsed once instead of
once per size (Docling is ~0.4 pages/s on this CPU). Distractor contracts are CUAD full_contract_txt files.

    backend/.venv/bin/python cuad_scale_curve.py LIBS_DIR DATA_DIR OUT_DIR [--sizes 12 50 200 510] [--repeats 1 1 1 1]

LIBS_DIR comes from make_scale_corpora.py (lib12 ... lib510). Writes OUT_DIR/cuad<size>.jsonl and
OUT_DIR/stages.json (ingest wall time, chunks, sources per stage). Judge/report afterwards with
`docket eval judge --cross-check` / `docket eval report`.
"""
import argparse, json, shutil, sys, time
from pathlib import Path

from scale_guard import check_docket_checkout  # sibling module; fails loudly under `python -I`

check_docket_checkout()

from docket.core.config import Settings
from docket.eval.runner import EvalRunner, OllamaMetaGateway, select_questions
from docket.eval.schema import load_gold_set, fingerprint, Split

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("libs", type=Path); ap.add_argument("data", type=Path); ap.add_argument("out", type=Path)
    ap.add_argument("--gold", type=Path, default=Path(__file__).resolve().parent.parent / "gold-cuad-scale.yaml")
    ap.add_argument("--sizes", type=int, nargs="+", default=[12, 50, 200, 510])
    ap.add_argument("--repeats", type=int, nargs="+", default=[3, 1, 1, 3])
    ap.add_argument("--allow-duplicate-sources", action="store_true",
                    help="record (not fail on) contracts presented by several sources; this driver adds one source "
                         "per stage, so use cuad_query_clean.py for clean-library measurements")
    ap.add_argument("--allow-cpu-fallback", action="store_true",
                    help="escape hatch: do not fail when a model is offloaded to CPU (still recorded in <out>.gpu.json)")
    a = ap.parse_args()
    assert "/.local/share/docket" not in str(a.data.resolve())
    a.out.mkdir(parents=True, exist_ok=True)
    corpus = a.out / "corpus_growing"
    corpus.mkdir(exist_ok=True)
    gold = load_gold_set(a.gold)
    qs = select_questions(gold, split=None)
    stages = json.loads((a.out / "stages.json").read_text()) if (a.out / "stages.json").exists() else {}
    for size, reps in zip(a.sizes, a.repeats):
        src = a.libs / f"lib{size}"
        for sub in ("contracts", "distractors"):
            if (src / sub).exists():
                (corpus / sub).mkdir(exist_ok=True)
                for f in (src / sub).iterdir():
                    if not (corpus / sub / f.name).exists():
                        shutil.copy2(f, corpus / sub / f.name)
        outp = a.out / f"cuad{size}.jsonl"
        if outp.exists():
            print("skip existing", outp); continue
        t0 = time.perf_counter()
        with EvalRunner(corpus, gateway=OllamaMetaGateway(gen_model=Settings().gen_model), data_dir=a.data) as runner:
            runner.ingest()
            ingest_s = time.perf_counter() - t0
            from docket.eval.hygiene import LibraryHygieneError, require_one_source_per_document
            try:
                library = require_one_source_per_document(a.data / "docket.sqlite3")
            except LibraryHygieneError as exc:
                if not a.allow_duplicate_sources:
                    raise SystemExit(f"[{size}] library hygiene: {exc}")
                print(f"[{size}] WARNING library hygiene: {exc}", flush=True)
                library = {"hygiene_violation": str(exc)}
            n_chunks = len(runner.corpus_chunks())
            print(f"[{size}] ingest {ingest_s:.0f}s chunks={n_chunks} failures={runner.ingest_failures[:3]}", flush=True)
            from docket.eval.manifest import build_manifest, write_manifest
            baseline = build_manifest(runner._context.settings, gold, lane="gpu", gold_path=a.gold, library=library)
            mpath = outp.with_name(outp.name + ".manifest.json")
            write_manifest(baseline, mpath, overwrite=True)
            from docket.eval.gpu_metrics import gpu_lane_guard

            def record_gpu(summary):
                baseline.gpu = summary.to_dict()
                write_manifest(baseline, mpath, overwrite=True)

            with gpu_lane_guard("gpu", outp.with_name(outp.name + ".gpu.json"),
                                allow_cpu_fallback=a.allow_cpu_fallback, on_summary=record_gpu):
                runner.run(qs, repeats=reps, out_path=outp, gold_digest=fingerprint(gold),
                           manifest=baseline.model_dump(mode="json"),
                           progress=lambda d, t, r: print(f"[{size}] {d}/{t} {r.question_id}#{r.repeat} {r.latency_s:.1f}s"
                                                          + (f" ERROR {r.error}" if r.error else ""), flush=True))
        stages[str(size)] = {"ingest_wall_s_incremental": round(ingest_s, 1), "chunks_in_library": n_chunks,
                             "repeats": reps, "questions": len(qs), "ingest_failures": runner.ingest_failures[:5], "library": library}
        (a.out / "stages.json").write_text(json.dumps(stages, indent=1))


if __name__ == "__main__":  # parse workers re-import this file under spawn
    main()
