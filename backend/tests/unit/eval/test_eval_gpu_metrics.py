"""GPU metrics recorder with fake tool output only (no GPU, no Ollama)."""

from __future__ import annotations

import json

import pytest

from docket.eval.gpu_metrics import (
    GpuMetricsRecorder,
    assert_no_cpu_fallback,
    parse_ollama_ps,
    parse_processor,
    parse_rocm_smi,
)

PS_GPU = """NAME           ID            SIZE     PROCESSOR    CONTEXT    UNTIL
qwen3:14b      bdbd181c33f2  11 GB    100% GPU     8192       4 minutes from now
"""
PS_SPLIT = """NAME           ID            SIZE     PROCESSOR          CONTEXT    UNTIL
qwen3:14b      bdbd181c33f2  17 GB    48%/52% CPU/GPU    16384      4 minutes from now
"""
PS_CPU = """NAME           ID            SIZE     PROCESSOR    UNTIL
gemma3:12b     abcdef123456  9.0 GB   100% CPU     4 minutes from now
"""
ROCM = json.dumps({
    "card0": {"GPU use (%)": "87", "GPU Memory Allocated (VRAM%)": "63"},
    "system": {"Driver version": "6.x"},
})


def runner(ps=PS_GPU, rocm=ROCM, fail=()):
    def run(cmd):
        if cmd[0] in fail:
            raise FileNotFoundError(cmd[0])
        return rocm if cmd[0] == "rocm-smi" else ps
    return run


def test_parse_processor_variants():
    assert parse_processor("100% GPU") == (100.0, 0.0)
    assert parse_processor("100% CPU") == (0.0, 100.0)
    assert parse_processor("48%/52% CPU/GPU") == (52.0, 48.0)
    assert parse_processor("???") == (None, None)


def test_parse_ollama_ps_tables():
    (p,) = parse_ollama_ps(PS_GPU)
    assert (p.model, p.gpu_percent, p.cpu_percent) == ("qwen3:14b", 100.0, 0.0)
    assert parse_ollama_ps(PS_SPLIT)[0].gpu_percent == 52.0
    assert parse_ollama_ps(PS_CPU)[0].cpu_percent == 100.0
    assert parse_ollama_ps("NAME ID SIZE PROCESSOR\n") == []
    assert parse_ollama_ps("") == []


def test_parse_rocm_smi():
    assert parse_rocm_smi(ROCM) == {"card0": {"gpu_use_percent": 87.0, "vram_percent": 63.0}}
    with pytest.raises(ValueError):
        parse_rocm_smi("not json")


def test_sample_once_and_summary_full_gpu():
    rec = GpuMetricsRecorder(runner())
    rec.sample_once()
    rec.sample_once()
    s = rec.summary()
    assert (s.samples, s.errors, s.max_gpu_use_percent, s.max_vram_percent) == (2, 0, 87.0, 63.0)
    assert s.min_gpu_share_percent == 100.0 and not s.cpu_offload_detected and s.models_seen == ["qwen3:14b"]
    assert_no_cpu_fallback(s)


def test_cpu_offload_is_detected_and_raises():
    rec = GpuMetricsRecorder(runner(ps=PS_SPLIT))
    rec.sample_once()
    s = rec.summary()
    assert s.cpu_offload_detected and s.min_gpu_share_percent == 52.0
    with pytest.raises(RuntimeError, match="offloaded to CPU"):
        assert_no_cpu_fallback(s)
    cpu = GpuMetricsRecorder(runner(ps=PS_CPU))
    cpu.sample_once()
    with pytest.raises(RuntimeError):
        assert_no_cpu_fallback(cpu.summary())


def test_missing_tools_give_error_samples_and_unverifiable():
    rec = GpuMetricsRecorder(runner(fail=("rocm-smi", "ollama")))
    sample = rec.sample_once()
    assert "rocm-smi" in sample.error and "ollama ps" in sample.error
    with pytest.raises(RuntimeError, match="could not be verified"):
        assert_no_cpu_fallback(rec.summary())


def test_background_thread_samples_and_stops():
    rec = GpuMetricsRecorder(runner(), interval_s=0.01)
    rec.start()
    rec.start()  # idempotent
    for _ in range(200):
        if len(rec.samples) >= 2:
            break
        import time
        time.sleep(0.01)
    summary = rec.stop()
    assert summary.samples >= 2 and rec._thread is None
    assert summary.to_dict()["models_seen"] == ["qwen3:14b"]
