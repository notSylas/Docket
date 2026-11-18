"""Lane B tooling (no GPU, no Ollama): run script contract, orchestrator helpers, summary statistics."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
M0 = BACKEND / "eval-public" / "m0"
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def lane_b():
    sys.path[:0] = [str(M0), str(BACKEND / "eval-public" / "scale")]
    import lane_b as mod  # noqa: PLC0415

    return mod


def test_run_script_contract():
    text = (M0 / "run_lane_b.sh").read_text()
    assert "systemd-run --user --scope -q -p MemoryMax=12G" in text
    assert "DOCKET_PARSE_WORKERS=1" in text
    assert "DOCKET_LANE_B_PYTHON" in text and "$repo/backend/.venv/bin/python" in text
    assert "/home/" not in text  # no developer-specific absolute path (portable across checkouts)
    assert 'PYTHONPATH="$repo/backend/src' in text
    assert "python -I" not in text.replace("never `python -I`", "")
    assert "unset DOCKET_KAG_SCOPING" in text  # flags are set per configuration, never inherited
    assert (M0 / "run_lane_b.sh").stat().st_mode & 0o111


def test_orchestrator_is_sequential_resumable_and_scratch_only():
    text = (M0 / "lane_b.py").read_text()
    assert ".done" in text and "ollama" in text and '"stop"' in text  # markers + model unloading
    assert "Popen" in text and "ThreadPool" not in text and "concurrent" not in text  # one process at a time
    assert "--allow-self-judge" in text and "gemma3:12b" in text and "DOCKET_KAG_SCOPING" in text
    assert "/.local/share/docket" in text  # only inside the safety assertion
    assert "shutil.rmtree(work" in text  # clean-library copies are removed, originals never mutated


def test_plan_covers_all_sets_and_configs(lane_b):
    names = [s["name"] for s in lane_b.sets()]
    assert names == ["orig", "g6", "cuad12", "cuad50", "cuad200"]
    assert lane_b.CONFIGS == {"off": {}, "kag": {"DOCKET_KAG_SCOPING": "on"}}
    assert [j[1] for j in lane_b.JUDGES] == ["gemma3:12b", "qwen3:14b"] and [j[2] for j in lane_b.JUDGES] == [False, True]
    assert lane_b.CUAD_REPEATS == {12: 2, 50: 1, 200: 1}


def test_failure_patterns(lane_b):
    assert lane_b.FATAL.search("ollama: llama runner process has terminated: ROCm error: out of memory")
    assert lane_b.FALLBACK.search("Error: model offloaded to CPU: lowest GPU share 52%")
    assert not lane_b.FATAL.search("ordinary traceback: KeyError")


def test_preflight_refuses_foreign_gpu_process(lane_b, tmp_path, monkeypatch):
    foreign = (FIX / "rocm_smi_pids_two_llama_real.txt").read_text() + "4242   blender   1   1000   0   UNKNOWN\n"
    monkeypatch.setattr(lane_b, "sh", lambda cmd, timeout=20: foreign if cmd[0] == "rocm-smi" else "NAME ID SIZE PROCESSOR CONTEXT UNTIL\n")
    with pytest.raises(lane_b.StopRun, match="foreign GPU processes"):
        lane_b.preflight(tmp_path, lambda m: None)


def test_preflight_passes_on_empty_gpu_and_unloads_ollama_models(lane_b, tmp_path, monkeypatch):
    state = {"ps": (FIX / "ollama_ps_real_two_models.txt").read_text(), "pids": (FIX / "rocm_smi_pids_two_llama_real.txt").read_text()}
    stopped = []

    def fake(cmd, timeout=20):
        if cmd[:2] == ["ollama", "stop"]:
            stopped.append(cmd[2])
            state["ps"] = (FIX / "ollama_ps_real_empty.txt").read_text()
            state["pids"] = (FIX / "rocm_smi_pids_none_real.txt").read_text()
            return ""
        return state["pids"] if cmd[0] == "rocm-smi" else state["ps"]

    monkeypatch.setattr(lane_b, "sh", fake)
    info = lane_b.preflight(tmp_path, lambda m: None)
    assert info["clean"] and set(stopped) == {"qwen3-embedding:0.6b", "qwen3:14b"}


def test_preflight_reports_held_vram_after_stop(lane_b, tmp_path, monkeypatch):
    pids = (FIX / "rocm_smi_pids_two_llama_real.txt").read_text()
    monkeypatch.setattr(lane_b, "sh", lambda cmd, timeout=20: pids if cmd[0] == "rocm-smi" else "NAME ID SIZE PROCESSOR CONTEXT UNTIL\n")
    monkeypatch.setattr(lane_b.time, "sleep", lambda s: None)
    with pytest.raises(lane_b.StopRun, match="reboot"):
        lane_b.preflight(tmp_path, lambda m: None)


def test_summary_statistics():
    sys.path[:0] = [str(M0), str(BACKEND / "eval-public" / "scale")]
    import lane_b_summary as s  # noqa: PLC0415

    lo, hi = s.abstention_ci({"a": [False, False], "b": [True, False], "c": [False, False]})
    assert 0.0 <= lo <= hi <= 1.0 and hi > 0
    assert s.abstention_ci({}) == (0.0, 1.0)
    stats = s.pair_stats([(True, True), (True, False), (False, False), (False, True), (True, True)])
    assert stats["n"] == 5 and stats["disagreements"] == 2 and stats["raw_agreement"] == 0.6
    assert stats["gemma_pass_qwen_fail"] == 1 and stats["gemma_fail_qwen_pass"] == 1
    assert s.pair_stats([]) == {"n": 0}
