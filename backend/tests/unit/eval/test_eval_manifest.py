"""Baseline manifest: contents, lane, hardware probing, safety of writes."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from docket.eval.manifest import (
    BaselineManifest,
    build_manifest,
    hardware_info,
    load_manifest,
    manifest_lines,
    verify_manifest_gold,
    write_manifest,
)
from docket.eval.schema import GoldSet, GoldSetError, Question, RunRecord, fingerprint


def _gold(text="Q?"):
    return GoldSet(questions=[Question(id="a", type="out_of_corpus", question=text, answerable=False)])


def _settings(**kw):
    base = dict(gen_model="qwen3:14b", embed_model="e", rewrite_model=None, vision_model="v",
                kag_scoping="on", doc_routing="off")
    base.update(kw)
    return SimpleNamespace(**base)


def fake_run(table):
    def run(cmd):
        key = " ".join(cmd)
        if key in table:
            value = table[key]
            if isinstance(value, Exception):
                raise value
            return value
        raise FileNotFoundError(cmd[0])
    return run


GIT = {
    "git -C /r rev-parse HEAD": "abc123def456\n",
    "git -C /r status --porcelain": " M x.py\n",
    "git -C /r rev-parse --abbrev-ref HEAD": "m0-baseline\n",
}


def test_manifest_records_commit_flags_models_judges_and_gold(tmp_path):
    from pathlib import Path

    m = build_manifest(_settings(), _gold(), lane="cpu", judges=["gemma3:12b"], gold_path="g.yaml",
                       corpus={"a.pdf": "h"}, library={"servable_documents": 3}, repo=Path("/r"),
                       run=fake_run(GIT), env={"DOCKET_KAG_SCOPING": "on", "DOCKET_API_TOKEN": "x", "HOME": "/h"})
    assert m.code_commit == "abc123def456" and m.git_dirty is True and m.git_branch == "m0-baseline"
    assert m.flags["kag_scoping"] == "on" and m.flags["doc_routing"] == "off"
    assert m.env_flags == {"DOCKET_KAG_SCOPING": "on"}  # secrets-looking and non-DOCKET vars are excluded
    assert m.models["gen"] == "qwen3:14b" and m.models["rewrite"] == "qwen3:14b"
    assert m.judges == ["gemma3:12b"] and m.self_judged is False
    assert m.gold_fingerprint == fingerprint(_gold()) and m.corpus_files == 1
    assert m.hardware == {} and m.lane == "cpu" and m.python_version


def test_self_judged_flag_when_judge_equals_answerer():
    m = build_manifest(_settings(), judges=["qwen3:14b", "gemma3:12b"], run=fake_run({}), probe_hardware=False)
    assert m.self_judged is True


def test_git_absence_is_tolerated():
    m = build_manifest(_settings(), run=fake_run({}), probe_hardware=False)
    assert m.code_commit is None and m.git_dirty is None


def test_lane_validation_and_fingerprint_changes_with_flag():
    with pytest.raises(ValueError):
        build_manifest(_settings(), lane="tpu", run=fake_run({}))
    a = build_manifest(_settings(kag_scoping="off"), run=fake_run({}), probe_hardware=False)
    b = build_manifest(_settings(kag_scoping="on"), run=fake_run({}), probe_hardware=False)
    c = build_manifest(_settings(kag_scoping="on"), run=fake_run({}), probe_hardware=False)
    assert a.fingerprint() != b.fingerprint() and b.fingerprint() == c.fingerprint()


def test_gpu_lane_probes_hardware_with_fake_tools():
    table = {
        "ollama --version": "ollama version is 0.12.3\n",
        "rocm-smi --showproductname": "GPU[0] : Card Series: Radeon RX 9070 XT\n",
        "rocm-smi --showmeminfo vram": "GPU[0] : VRAM Total Memory (B): 17163091968\n",
    }
    m = build_manifest(_settings(), lane="gpu", run=fake_run(table))
    assert m.hardware["ollama_version"].endswith("0.12.3")
    assert m.hardware["gpu_name"] == "Radeon RX 9070 XT"
    assert m.hardware["vram_total_bytes"] == "17163091968"


def test_hardware_tolerates_missing_and_failing_tools():
    assert hardware_info(fake_run({"ollama --version": RuntimeError("boom")})).get("ollama_version") is None
    info = hardware_info(fake_run({}))
    assert "gpu_name" not in info and "ollama_version" not in info


def test_write_refuses_overwrite_and_is_0600(tmp_path):
    m = build_manifest(_settings(), run=fake_run({}), probe_hardware=False)
    path = tmp_path / "sub" / "m.json"
    write_manifest(m, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_manifest(m, path)
    write_manifest(m, path, overwrite=True)
    assert load_manifest(path) == m


def test_load_rejects_garbage(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"nope": 1}')
    with pytest.raises(GoldSetError):
        load_manifest(p)


def test_verify_manifest_gold():
    m = build_manifest(_settings(), _gold(), run=fake_run({}), probe_hardware=False)
    verify_manifest_gold(m, _gold())
    with pytest.raises(GoldSetError):
        verify_manifest_gold(m, _gold("different"))


def test_old_run_records_still_load_and_fingerprint_is_stable():
    old = {"question_id": "q", "repeat": 0, "answer": "x"}
    rec = RunRecord.model_validate(old)
    assert rec.manifest is None
    base = fingerprint(rec)
    with_manifest = rec.model_copy(update={"manifest": {"lane": "cpu"}})
    assert fingerprint(with_manifest) != base
    assert "manifest" not in json.dumps({k: v for k, v in rec.model_dump().items() if k != "manifest"})


def test_manifest_lines_handles_none_and_dict():
    assert "not recorded" in manifest_lines(None)[0]
    m = build_manifest(_settings(), run=fake_run({}), probe_hardware=False)
    assert manifest_lines(m.model_dump(mode="json"))[0].startswith("Run manifest: lane=cpu")
