"""GPU metrics recorder with fake tool output only (no GPU, no Ollama)."""

from __future__ import annotations

import json

import pytest
from conftest import PlainTextParser, ScriptedGateway

from docket.eval.gpu_metrics import (
    GpuMetricsRecorder,
    assert_no_cpu_fallback,
    foreign_gpu_processes,
    parse_kfd_pids,
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


# -- real captured output (Ubuntu 24.04, ROCm 7.2.1, Ollama 0.32.15, RX 9070 XT + iGPU) ----------

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_real_ollama_ps_two_models_and_gemma_and_empty():
    two = parse_ollama_ps(_fx("ollama_ps_real_two_models.txt"))
    assert [(p.model, p.size, p.gpu_percent, p.cpu_percent) for p in two] == [
        ("qwen3-embedding:0.6b", "2.4 GB", 100.0, 0.0),
        ("qwen3:14b", "9.6 GB", 100.0, 0.0),
    ]
    assert two[0].processor == "100% GPU"
    (gemma,) = parse_ollama_ps(_fx("ollama_ps_real_gemma.txt"))
    assert (gemma.model, gemma.gpu_percent) == ("gemma3:12b", 100.0)
    assert parse_ollama_ps(_fx("ollama_ps_real_empty.txt")) == []


def test_real_rocm_smi_two_cards_and_card_selection():
    cards = parse_rocm_smi(_fx("rocm_smi_real_loaded.json"))
    assert cards["card0"] == {"gpu_use_percent": 3.0, "vram_percent": 81.0}
    assert cards["card1"] == {"gpu_use_percent": 0.0, "vram_percent": 4.0}  # the CPU's iGPU
    assert parse_rocm_smi(_fx("rocm_smi_real_idle.json"))["card0"]["gpu_use_percent"] == 96.0

    def run(cmd):
        return _fx("rocm_smi_real_loaded.json") if cmd[0] == "rocm-smi" else _fx("ollama_ps_real_two_models.txt")

    whole = GpuMetricsRecorder(run, card="card0")
    s = whole.sample_once()
    assert (s.gpu_use_percent, s.vram_percent) == (3.0, 81.0) and s.rss_mb is not None
    assert whole.summary().card == "card0"
    assert whole.summary().min_gpu_share_by_model == {"qwen3-embedding:0.6b": 100.0, "qwen3:14b": 100.0}
    assert GpuMetricsRecorder(run).sample_once().vram_percent == 81.0  # default: max over cards
    bad = GpuMetricsRecorder(run, card="card9").sample_once()
    assert "card9" in bad.error


def test_real_kfd_pids_and_foreign_detection():
    rows = parse_kfd_pids(_fx("rocm_smi_pids_two_llama_real.txt"))
    assert rows == [(110347, "llama-server"), (110213, "llama-server")]
    assert foreign_gpu_processes(_fx("rocm_smi_pids_two_llama_real.txt")) == []
    assert parse_kfd_pids(_fx("rocm_smi_pids_none_real.txt")) == []
    text = _fx("rocm_smi_pids_two_llama_real.txt") + "4242   blender   1   1000   0   UNKNOWN\n"
    assert foreign_gpu_processes(text) == [(4242, "blender")]


def test_real_driver_version_and_manifest_hardware():
    from docket.eval.manifest import hardware_info

    table = {
        "rocm-smi --showdriverversion": _fx("rocm_smi_driverversion_real.txt"),
        "rocm-smi --showproductname": _fx("rocm_smi_product_meminfo_real.txt"),
        "rocm-smi --showmeminfo vram": _fx("rocm_smi_product_meminfo_real.txt"),
        "ollama --version": _fx("ollama_version_real.txt"),
        "systemctl show ollama -p Environment --value": "PATH=/usr/bin OLLAMA_GPU_OVERHEAD=1073741824 OLLAMA_HOST=0.0.0.0:11434",
    }

    def run(cmd):
        return table[" ".join(cmd)]

    info = hardware_info(run)
    assert info["rocm_driver"] == "6.16.13"
    assert info["gpu_name"] == "AMD Radeon RX 9070 XT"  # card0 is the discrete GPU, not the iGPU
    assert info["vram_total_bytes"] == "17095983104"
    assert info["ollama_version"] == "ollama version is 0.32.15"
    assert info["ollama_env_OLLAMA_GPU_OVERHEAD"] == "1073741824" and "ollama_env_OLLAMA_HOST" not in info


def test_process_tree_rss_is_positive_and_missing_pid_is_zeroish():
    from docket.eval.gpu_metrics import process_tree_rss_mb

    assert process_tree_rss_mb() > 1.0


# -- the GPU-lane guard ------------------------------------------------------------------------


def _guard_recorder(ps=PS_GPU, rocm=ROCM):
    return GpuMetricsRecorder(runner(ps=ps, rocm=rocm), interval_s=0.01)


def test_guard_is_a_noop_for_the_cpu_lane(tmp_path):
    from docket.eval.gpu_metrics import gpu_lane_guard

    with gpu_lane_guard("cpu", tmp_path / "g.json") as rec:
        assert rec is None
    assert not (tmp_path / "g.json").exists()


def test_guard_writes_private_summary_and_passes_on_full_gpu(tmp_path):
    import os
    import stat

    from docket.eval.gpu_metrics import gpu_lane_guard

    seen = []
    with gpu_lane_guard("gpu", tmp_path / "out" / "g.json", recorder=_guard_recorder(), on_summary=seen.append):
        pass
    data = json.loads((tmp_path / "out" / "g.json").read_text())
    assert data["summary"]["models_seen"] == ["qwen3:14b"] and data["summary"]["max_gpu_use_percent"] == 87.0
    assert stat.S_IMODE(os.stat(tmp_path / "out" / "g.json").st_mode) == 0o600
    assert seen and seen[0].cpu_offload_detected is False


def test_guard_fails_loudly_on_cpu_fallback_but_still_writes_summary(tmp_path):
    from docket.eval.gpu_metrics import CpuFallbackError, gpu_lane_guard

    seen = []
    with pytest.raises(CpuFallbackError, match="offloaded to CPU"):
        with gpu_lane_guard("gpu", tmp_path / "g.json", recorder=_guard_recorder(ps=PS_SPLIT), on_summary=seen.append):
            pass
    assert json.loads((tmp_path / "g.json").read_text())["summary"]["cpu_offload_detected"] is True
    assert seen[0].cpu_fallback_allowed is False


def test_guard_escape_hatch_flag_and_env(tmp_path, monkeypatch):
    from docket.eval.gpu_metrics import gpu_lane_guard

    with gpu_lane_guard("gpu", tmp_path / "a.json", recorder=_guard_recorder(ps=PS_CPU), allow_cpu_fallback=True):
        pass
    assert json.loads((tmp_path / "a.json").read_text())["summary"]["cpu_fallback_allowed"] is True
    monkeypatch.setenv("DOCKET_ALLOW_CPU_FALLBACK", "1")
    with gpu_lane_guard("gpu", tmp_path / "b.json", recorder=_guard_recorder(ps=PS_CPU)):
        pass


def test_guard_never_masks_the_original_exception(tmp_path):
    from docket.eval.gpu_metrics import gpu_lane_guard

    with pytest.raises(ValueError, match="boom"):
        with gpu_lane_guard("gpu", tmp_path / "g.json", recorder=_guard_recorder(ps=PS_CPU)):
            raise ValueError("boom")


def test_idle_gpu_with_resident_model_is_unverified_activity():
    zero = json.dumps({"card0": {"GPU use (%)": "0", "GPU Memory Allocated (VRAM%)": "50"}})
    rec = GpuMetricsRecorder(runner(rocm=zero))
    for _ in range(3):
        rec.sample_once()
    with pytest.raises(RuntimeError, match="no GPU utilisation"):
        assert_no_cpu_fallback(rec.summary())


def test_run_eval_gpu_lane_records_summary_in_manifest_and_fails_on_offload(fixtures_dir, tmp_path, monkeypatch):
    import docket.eval.gpu_metrics as gm
    from docket.eval.manifest import load_manifest
    from docket.eval.runner import run_eval
    from docket.eval.schema import load_gold_set
    from docket.services.query.classifier import QueryMode

    gold = load_gold_set(fixtures_dir / "gold.yaml")
    ps = {"v": PS_GPU}
    monkeypatch.setattr(gm, "GpuMetricsRecorder", lambda **kw: GpuMetricsRecorder(runner(ps=ps["v"]), **kw))

    def go(name, **extra):
        return run_eval(
            gold, fixtures_dir / "corpus", tmp_path / name, gateway=ScriptedGateway({}), parser=PlainTextParser(),
            repeats=1, mode=QueryMode.FAST, lane="gpu", ids=[gold.questions[0].id], **extra,
        )

    go("ok.jsonl")
    manifest = load_manifest(tmp_path / "ok.jsonl.manifest.json")
    assert manifest.lane == "gpu" and manifest.gpu["models_seen"] == ["qwen3:14b"] and manifest.gpu["samples"] >= 2
    assert (tmp_path / "ok.jsonl.gpu.json").exists()

    ps["v"] = PS_SPLIT
    with pytest.raises(gm.CpuFallbackError):
        go("bad.jsonl")
    assert load_manifest(tmp_path / "bad.jsonl.manifest.json").gpu["cpu_offload_detected"] is True
    go("allowed.jsonl", allow_cpu_fallback=True)
