"""Tier 3 spike: does the system stay responsive when ingestion (parsing + embedding,
GPU-bound) runs concurrently with a query (generation, also GPU-bound)?

Measures query latency with no concurrent load (baseline), then again while ingestion
is actively running, plus VRAM/RAM/CPU samples throughout.

Usage: python test_resource_contention.py
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
TEST_QUESTION = "What vector database was chosen as the primary candidate?"

samples = []
stop_sampling = threading.Event()


def sample_resources():
    while not stop_sampling.is_set():
        vram_used = None
        try:
            out = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram", "--json"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            data = json.loads(out)
            vram_used = int(data["card0"]["VRAM Total Used Memory (B)"])
        except Exception:
            pass
        samples.append({"t": time.time(), "vram_used_bytes": vram_used})
        time.sleep(2)


def run_query(label: str) -> float:
    start = time.time()
    result = subprocess.run(
        [PY, str(ROOT / "query.py"), TEST_QUESTION],
        capture_output=True, text=True, timeout=180,
    )
    elapsed = time.time() - start
    ok = result.returncode == 0 and "--- Answer ---" in result.stdout
    print(f"[{label}] query latency: {elapsed:.2f}s | ok={ok}")
    if not ok:
        print(f"  stderr tail: {result.stderr[-500:]}")
    return elapsed


def main() -> None:
    sampler = threading.Thread(target=sample_resources, daemon=True)
    sampler.start()

    print("=== Baseline: query with no concurrent load ===")
    baseline_latencies = [run_query(f"baseline-{i}") for i in range(2)]

    print("\n=== Starting ingestion in background ===")
    ingest_start = time.time()
    ingest_proc = subprocess.Popen(
        [PY, str(ROOT / "ingest.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    print("=== Running queries WHILE ingestion is active ===")
    concurrent_latencies = []
    query_num = 0
    while ingest_proc.poll() is None:
        concurrent_latencies.append(run_query(f"concurrent-{query_num}"))
        query_num += 1
        if query_num >= 6:  # cap so this doesn't run forever if ingestion is slow
            break

    ingest_proc.wait(timeout=300)
    ingest_elapsed = time.time() - ingest_start
    print(f"\nIngestion finished in {ingest_elapsed:.2f}s (return code {ingest_proc.returncode})")

    print("\n=== Post-ingestion: query with no concurrent load ===")
    post_latencies = [run_query(f"post-{i}") for i in range(2)]

    stop_sampling.set()
    sampler.join(timeout=5)

    def summarize(name, latencies):
        if not latencies:
            print(f"{name}: no samples")
            return
        print(f"{name}: n={len(latencies)} avg={sum(latencies)/len(latencies):.2f}s "
              f"min={min(latencies):.2f}s max={max(latencies):.2f}s")

    print("\n=== Summary ===")
    summarize("Baseline (no load)", baseline_latencies)
    summarize("Concurrent (during ingestion)", concurrent_latencies)
    summarize("Post-ingestion", post_latencies)

    vram_values = [s["vram_used_bytes"] for s in samples if s["vram_used_bytes"] is not None]
    if vram_values:
        print(f"\nVRAM used during test: min={min(vram_values)/1e9:.2f}GB "
              f"max={max(vram_values)/1e9:.2f}GB")

    print(f"\nTotal resource samples collected: {len(samples)}")


if __name__ == "__main__":
    main()
