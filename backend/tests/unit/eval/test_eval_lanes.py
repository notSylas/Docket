"""CPU/GPU lane plumbing: marker registered, CI deselects it, scripts agree."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[3]
REPO = BACKEND.parent


def test_gpu_marker_is_registered():
    cfg = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("gpu:") for m in markers)


def test_ci_workflow_is_valid_yaml_with_cpu_only_commands():
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    jobs = wf["jobs"]
    assert jobs["python-unit"]["strategy"]["matrix"]["python-version"] == ["3.12"]
    steps = " ".join(str(s.get("run", "")) for s in jobs["python-unit"]["steps"])
    assert "pytest tests/unit" in steps and "not gpu" in steps
    rust = " ".join(str(s.get("run", "")) for s in jobs["rust-workspace"]["steps"])
    assert "cargo test --workspace" in rust
    assert "ollama" not in (REPO / ".github" / "workflows" / "ci.yml").read_text().lower().replace("no ollama", "")


def test_local_script_caps_memory_and_deselects_gpu():
    script = (REPO / "scripts" / "ci-local.sh").read_text()
    assert "systemd-run --user --scope -q -p MemoryMax=6G" in script
    assert 'not gpu' in script and "cargo test --workspace" in script
    assert "python -I" not in script.replace("never `python -I`", "")
    assert "--gpu" in script and "-m gpu" in script


def test_gpu_test_dir_is_marked_and_opt_in():
    text = (BACKEND / "tests" / "gpu" / "test_gpu_lane.py").read_text()
    assert "pytest.mark.gpu" in text and "DOCKET_GPU_LANE" in text
