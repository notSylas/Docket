"""GPU lane (marker `gpu`): read-only residency check, run via `scripts/ci-local.sh --gpu`.

Skipped unless DOCKET_GPU_LANE=1. It only reads `ollama ps` / `rocm-smi`; it
starts no model. The mandatory CPU CI deselects it with `-m "not gpu"`.
"""

from __future__ import annotations

import os

import pytest

from docket.eval.gpu_metrics import GpuMetricsRecorder, assert_no_cpu_fallback

pytestmark = [
    pytest.mark.gpu,
    pytest.mark.skipif(os.environ.get("DOCKET_GPU_LANE") != "1", reason="GPU lane is opt-in (DOCKET_GPU_LANE=1)"),
]


def test_loaded_models_are_fully_on_gpu():
    recorder = GpuMetricsRecorder()
    recorder.sample_once()
    summary = recorder.summary()
    if not summary.models_seen:
        pytest.skip("no model is loaded in Ollama; start the workload first, then sample")
    assert_no_cpu_fallback(summary)
