"""Tier 1 spike: measure local model generation speed and resource usage via Ollama.

Usage: python bench_model.py [model_name]
"""
import subprocess
import sys
import time

import ollama

MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3:14b"

PROMPTS = {
    "short_factual": "In one sentence, what is a vector database used for?",
    "long_context": (
        "Given the following context, answer the question and cite the source chunk id "
        "for every claim, or say 'insufficient evidence' if the context does not answer it.\n\n"
        "[chunk_1] The system uses SQLite FTS5 for lexical search and LanceDB for vector search.\n"
        "[chunk_2] Reciprocal Rank Fusion combines the two ranked lists before an optional rerank step.\n"
        "[chunk_3] The desktop shell is built with Tauri and React.\n\n"
        "Question: What two search techniques are combined, and how are their results merged?"
    ),
}


def gpu_vram_used_mb() -> str:
    try:
        out = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram"], capture_output=True, text=True, timeout=10
        ).stdout
        return out.strip()
    except FileNotFoundError:
        return "rocm-smi not found"


def run_prompt(name: str, prompt: str) -> None:
    print(f"\n=== {name} ===")
    print(f"VRAM before:\n{gpu_vram_used_mb()}\n")

    start = time.time()
    response = ollama.generate(model=MODEL, prompt=prompt)
    elapsed = time.time() - start

    eval_count = response.get("eval_count", 0)
    eval_duration_ns = response.get("eval_duration", 0)
    tok_per_sec = eval_count / (eval_duration_ns / 1e9) if eval_duration_ns else 0.0

    print(f"Response:\n{response['response']}\n")
    print(f"Wall time: {elapsed:.2f}s | tokens generated: {eval_count} | tok/s: {tok_per_sec:.2f}")
    print(f"VRAM after:\n{gpu_vram_used_mb()}")


if __name__ == "__main__":
    print(f"Benchmarking model: {MODEL}")
    for name, prompt in PROMPTS.items():
        run_prompt(name, prompt)
