"""Lane B micro-benchmarks (GPU): decode/prefill tokens/s per model, embedding throughput, and a timed
ingest of the 12-contract library into a scratch directory. Residency (`ollama ps` PROCESSOR split) is
verified separately for qwen3:14b, gemma3:12b and qwen3-embedding:0.6b. Direct Ollama HTTP calls; no app code.

    python bench_lane_b.py --out bench.json --scratch ~/docket-scale/m0 [--allow-cpu-fallback]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scale"))
from scale_guard import check_docket_checkout  # noqa: E402

check_docket_checkout()

from docket.core.config import Settings  # noqa: E402
from docket.eval.gpu_metrics import gpu_lane_guard  # noqa: E402
from docket.eval.outputs import write_private_text  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
HOME = Path.home()
CUAD12 = HOME / "docket-scale" / "libs" / "lib12"
CHUNK_DB = HOME / "docket-scale" / "m0" / "lib12" / "docket.sqlite3"  # scratch library, opened immutable
PARAGRAPH = (
    "The Distributor shall purchase the Products exclusively from the Company and shall not, directly or indirectly, "
    "market competing goods in the Territory during the Term. Either party may terminate this Agreement upon sixty "
    "days written notice if the other party commits a material breach that remains uncured. Fees are payable in "
    "United States dollars within thirty days of invoice, and late amounts accrue interest at one percent per month. "
)


def post(path: str, payload: dict, timeout: float = 600) -> dict:
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def ollama_stop(model: str) -> None:
    subprocess.run(["ollama", "stop", model], capture_output=True, timeout=60, check=False)
    time.sleep(2)


def generation_bench(model: str, allow: bool) -> dict:
    prompt = PARAGRAPH * 6 + "\n\nSummarise the termination rights in two sentences."
    ollama_stop(model)  # cold start measured from an empty GPU
    runs: list[dict] = []
    with gpu_lane_guard("gpu", None, allow_cpu_fallback=allow) as rec:
        for i in range(4):  # run 0 includes the model load
            t0 = time.perf_counter()
            body = {"model": model, "prompt": prompt, "stream": False, "options": {"num_predict": 160, "temperature": 0, "num_ctx": 4096}}
            if model.startswith("qwen3"):
                body["think"] = False
            r = post("/api/generate", body)
            wall = time.perf_counter() - t0
            runs.append({
                "wall_s": round(wall, 3), "load_s": round(r.get("load_duration", 0) / 1e9, 3),
                "prompt_tokens": r.get("prompt_eval_count"),
                "prefill_tok_s": round(r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9), 1) if r.get("prompt_eval_duration") else None,
                "decode_tokens": r.get("eval_count"),
                "decode_tok_s": round(r["eval_count"] / (r["eval_duration"] / 1e9), 1) if r.get("eval_duration") else None,
            })
        ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout
    warm = [x["decode_tok_s"] for x in runs[1:] if x["decode_tok_s"]]
    summary = rec.summary().to_dict() if rec else {}
    ollama_stop(model)
    return {"model": model, "runs": runs, "cold_load_s": runs[0]["load_s"],
            "decode_tok_s_median_warm": statistics.median(warm) if warm else None,
            "prefill_tok_s_median_warm": statistics.median([x["prefill_tok_s"] for x in runs[1:] if x["prefill_tok_s"]] or [0]),
            "ollama_ps_during": ps, "gpu": summary}


def embedding_bench(allow: bool) -> dict:
    model = Settings().embed_model
    conn = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro&immutable=1", uri=True)
    texts = [r[0] for r in conn.execute("SELECT text FROM chunks ORDER BY id LIMIT 512")]
    conn.close()
    ollama_stop(model)
    out: dict = {"model": model, "chunks_available": len(texts), "batches": []}
    with gpu_lane_guard("gpu", None, allow_cpu_fallback=allow) as rec:
        post("/api/embed", {"model": model, "input": texts[:2]})  # load
        for bs in (1, 16, 64):
            n = min(len(texts), 256)
            t0 = time.perf_counter()
            for i in range(0, n, bs):
                post("/api/embed", {"model": model, "input": texts[i:i + bs]})
            dt = time.perf_counter() - t0
            out["batches"].append({"batch_size": bs, "chunks": n, "seconds": round(dt, 3), "chunks_per_s": round(n / dt, 1)})
    out["mean_chunk_chars"] = round(statistics.mean(len(t) for t in texts[:256]), 1) if texts else None
    out["gpu"] = rec.summary().to_dict() if rec else {}
    ollama_stop(model)
    return out


def ingest_bench(scratch: Path, allow: bool) -> dict:
    from docket.eval.runner import EvalRunner, OllamaMetaGateway

    class TimingGateway(OllamaMetaGateway):
        embed_s = 0.0
        embed_calls = 0
        embedded = 0

        def embed_batch(self, texts):
            t0 = time.perf_counter()
            try:
                return super().embed_batch(texts)
            finally:
                TimingGateway.embed_s += time.perf_counter() - t0
                TimingGateway.embed_calls += 1
                TimingGateway.embedded += len(texts)

        def embed(self, text):
            t0 = time.perf_counter()
            try:
                return super().embed(text)
            finally:
                TimingGateway.embed_s += time.perf_counter() - t0
                TimingGateway.embed_calls += 1
                TimingGateway.embedded += 1

    data = scratch / f"bench-ingest-{time.strftime('%Y%m%d-%H%M%S')}"
    assert "/.local/share/docket" not in str(data.resolve())
    try:
        with gpu_lane_guard("gpu", None, allow_cpu_fallback=allow) as rec:
            t0 = time.perf_counter()
            with EvalRunner(CUAD12, gateway=TimingGateway(gen_model=Settings().gen_model), data_dir=data) as runner:
                runner.ingest()
                wall = time.perf_counter() - t0
                chunks = len(runner.corpus_chunks())
                failures = runner.ingest_failures[:3]
    finally:
        shutil.rmtree(data, ignore_errors=True)
    return {
        "files": len(list((CUAD12 / "contracts").iterdir())), "wall_s": round(wall, 1), "chunks": chunks,
        "chunks_per_s_end_to_end": round(chunks / wall, 2), "embed_wall_s": round(TimingGateway.embed_s, 1),
        "embed_calls": TimingGateway.embed_calls, "texts_embedded": TimingGateway.embedded,
        "chunks_per_s_embedding_only": round(TimingGateway.embedded / TimingGateway.embed_s, 1) if TimingGateway.embed_s else None,
        "parse_workers": 1, "failures": failures, "gpu": rec.summary().to_dict() if rec else {},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--allow-cpu-fallback", action="store_true")
    a = ap.parse_args()
    result = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "generation": {}, "embedding": None, "ingest_12_contracts": None}
    for model in ("qwen3:14b", "gemma3:12b"):
        result["generation"][model] = generation_bench(model, a.allow_cpu_fallback)
        write_private_text(a.out, json.dumps(result, indent=1) + "\n")
    result["embedding"] = embedding_bench(a.allow_cpu_fallback)
    result["ingest_12_contracts"] = ingest_bench(a.scratch, a.allow_cpu_fallback)
    result["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_private_text(a.out, json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main()
