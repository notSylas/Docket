"""Private eval kit (backend/eval-private): fakes only, no Ollama/GPU."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import PlainTextParser, ScriptedGateway

from docket.infra.inference.gateway import FakeInferenceGateway
from docket.services.query.prompts import ABSTENTION_PHRASE

KIT = Path(__file__).resolve().parents[3] / "eval-private"
sys.path.insert(0, str(KIT))
import ingest_folder  # noqa: E402
import kit_common  # noqa: E402
import run_private_eval  # noqa: E402

CONTRACT = (
    "%PDF-1.4\nMASTER SERVICES AGREEMENT\n\nThis Master Services Agreement is entered into between "
    "Acme Rocketry, Inc. (\"Provider\") and Northwind Traders Ltd. (\"Customer\").\n\n"
    "Governing Law. This Agreement is governed by the laws of the State of Texas.\n\n"
    "Termination. Either party may terminate on 60 days written notice.\n"
)
SECRET = "Texas"


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "acme_msa.pdf").write_text(CONTRACT)
    return d


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("DOCKET_DATA_DIR", "DOCKET_KAG_SCOPING"):
        monkeypatch.delenv(k, raising=False)


def test_guard_refuses_real_data_dir(tmp_path):
    for bad in (kit_common.REAL_DATA_DIR, kit_common.REAL_DATA_DIR / "sub", kit_common.REAL_DATA_DIR.parent):
        with pytest.raises(kit_common.KitError):
            kit_common.guard_scratch(bad)
    with pytest.raises(kit_common.KitError, match="overlap"):
        kit_common.guard_scratch(tmp_path / "docs" / "scratch", tmp_path / "docs")
    assert kit_common.guard_scratch(tmp_path / "ok") == (tmp_path / "ok").resolve()


def test_ingest_refuses_live_dir_before_touching_anything(docs):
    assert ingest_folder.main([str(docs), str(kit_common.REAL_DATA_DIR)]) == 2


def test_load_questions_and_template(tmp_path):
    qs = kit_common.load_questions(KIT / "questions_template.yaml")
    assert len(qs) == 3 and [q.answerable for q in qs] == [True, True, False]
    bad = tmp_path / "q.yaml"
    bad.write_text("questions:\n  - question: x\n    answerable: yes\n")
    with pytest.raises(kit_common.KitError, match="expected_answer"):
        kit_common.load_questions(bad)


def test_file_matches():
    assert kit_common.file_matches("acme_msa.pdf", ["Acme_MSA.pdf"])
    assert kit_common.file_matches("acme_msa", ["acme_msa.pdf"])
    assert not kit_common.file_matches("acme_msa.pdf", ["other.pdf"])
    assert not kit_common.file_matches("", ["a.pdf"])


def test_summarize():
    O = kit_common.Outcome
    outs = [O(True, False, True, True, 2.0), O(True, True, False, False, 4.0),
            O(False, True, True, None, 6.0), O(True, False, False, True, 8.0, errored=True)]
    s = kit_common.summarize(outs, [True, False, True, True])
    assert s["accuracy_pct"] == 50.0
    assert s["wrongful_abstention_pct"] == pytest.approx(33.3)
    assert s["expected_file_recall_pct"] == pytest.approx(66.7)
    assert s["entity_match_pct"] == 75.0
    assert s["median_latency_s"] == 5.0 and s["errors"] == 1


def test_judge_answer_parsing():
    assert kit_common.judge_answer(FakeInferenceGateway(canned_response='{"supported": true, "reason": "ok"}'), "q", "e", "a")
    assert not kit_common.judge_answer(FakeInferenceGateway(canned_response='{"supported": false}'), "q", "e", "a")
    assert not kit_common.judge_answer(FakeInferenceGateway(canned_response="garbage"), "q", "e", "a")


def test_end_to_end_prints_only_aggregates(docs, tmp_path, capsys):
    scratch = tmp_path / "scratch"
    before = sorted(p.name for p in docs.iterdir())
    stats = ingest_folder.build(docs, scratch, gateway=FakeInferenceGateway(), parser=PlainTextParser())
    assert stats["chunks"] >= 1 and stats["files_failed"] == 0
    assert stats["graph_entities"] >= 1  # KAG graph was built
    assert sorted(p.name for p in docs.iterdir()) == before
    with pytest.raises(kit_common.KitError, match="already holds"):
        ingest_folder.build(docs, scratch, gateway=FakeInferenceGateway(), parser=PlainTextParser())

    qfile = tmp_path / "q.yaml"
    qfile.write_text(
        "questions:\n"
        "  - question: Under the agreement with Acme Rocketry, Inc., which state's law governs?\n"
        "    expected_answer: Texas\n    expected_file: acme_msa.pdf\n    answerable: yes\n"
        "  - question: What is the termination fee in the Zorblax Holdings contract?\n"
        "    expected_answer: ''\n    answerable: no\n"
    )
    gen = ScriptedGateway({"which state": ("laws of the State of Texas", "Texas law governs. {tag}")})
    judge = FakeInferenceGateway(canned_response='{"supported": true, "reason": "same"}')
    out = tmp_path / "summary.json"
    res = run_private_eval.run(scratch, kit_common.load_questions(qfile), gateway=gen, judge_gateway=judge,
                               parser=PlainTextParser(), progress=print)
    for mode in ("kag_on", "kag_off"):
        assert res[mode]["accuracy_pct"] == 100.0
        assert res[mode]["wrongful_abstention_pct"] == 0.0
        assert res[mode]["expected_file_recall_pct"] == 100.0
        assert res[mode]["entity_match_pct"] == 50.0  # Acme found, Zorblax not
    printed = run_private_eval.format_summary(res)
    out.write_text(json.dumps(res))
    seen = capsys.readouterr().out + printed + out.read_text()
    for leak in (SECRET, "Texas", "acme_msa", "Acme", "Zorblax", "Northwind", "termination", ABSTENTION_PHRASE):
        assert leak.lower() not in seen.lower(), leak
    assert "KAG on" in printed


def test_main_writes_summary_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_private_eval, "run", lambda s, q, **k: {
        "kag_on": {"accuracy_pct": 1.0}, "kag_off": {"accuracy_pct": 2.0}, "kit_version": 1})
    out = tmp_path / "s.json"
    rc = run_private_eval.main([str(tmp_path), str(KIT / "questions_template.yaml"), "--summary-json", str(out)])
    assert rc == 0 and json.loads(out.read_text())["kag_off"]["accuracy_pct"] == 2.0
