"""`docket eval ...` smoke tests through Typer's CliRunner (fake gateway/parser)."""

from __future__ import annotations

import json
import shutil

import pytest
import yaml
from conftest import PlainTextParser, ScriptedGateway
from test_eval_judge import QueueGateway, yes
from typer.testing import CliRunner

from docket.cli.main import app
from docket.eval import cli as eval_cli
from docket.eval.schema import load_gold_set

runner = CliRunner()

SCRIPT = {
    "vacation days are there": ("25 days", "25 days. {tag}"),
    "days of paid vacation": ("25 days", "Employees get 25 days. {tag}"),
    "unused vacation": ("March 31", "They expire after March 31. {tag}"),
    "support channels": ("three channels", "Email, phone, and chat. {tag}"),
    "marketing budget": ("marketing budget", "It is $1,200,000. {tag}"),
    "And the engineering": ("engineering budget", "It is $3,500,000. {tag}"),
}


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(eval_cli, "_make_parser", lambda: PlainTextParser())
    monkeypatch.setattr(eval_cli, "_make_gateway", lambda model: ScriptedGateway(SCRIPT))
    return tmp_path


def test_eval_registered_in_help():
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    for name in ("run", "judge", "report", "compare", "draft", "review", "calibrate"):
        assert name in result.output


def test_run_report_judge_compare_calibrate_flow(fake_env, fixtures_dir, monkeypatch):
    tmp = fake_env
    runs = tmp / "runs.jsonl"
    result = runner.invoke(app, ["eval", "run", "--corpus", str(fixtures_dir / "corpus"),
                                 "--gold", str(fixtures_dir / "gold.yaml"), "--repeats", "2",
                                 "--split", "all", "--out", str(runs)])
    assert result.exit_code == 0, result.output
    assert "Wrote 16 runs" in result.output and "Accuracy" in result.output
    assert len(runs.read_text().splitlines()) == 16

    gold = str(fixtures_dir / "gold.yaml")
    result = runner.invoke(app, ["eval", "report", "--gold", gold, "--runs", str(runs), "--json"])
    assert result.exit_code == 0 and "overall" in json.loads(result.output)

    # Judge: every claim is answered "supported" by the fake judge.
    judge_gateway = QueueGateway([yes()])
    monkeypatch.setattr(eval_cli, "_make_gateway", lambda model: judge_gateway)
    judged = tmp / "judged.jsonl"
    result = runner.invoke(app, ["eval", "judge", "--gold", gold, "--runs", str(runs), "--out", str(judged),
                                 "--cross-check"])
    assert result.exit_code == 0, result.output
    assert "cross-check disagreements" in result.output
    assert len(judged.read_text().splitlines()) == 16

    result = runner.invoke(app, ["eval", "report", "--gold", gold, "--runs", str(runs), "--judged", str(judged)])
    assert result.exit_code == 0 and "Accuracy" in result.output

    # compare a judged file with itself: no regression; against a degraded copy: regression.
    result = runner.invoke(app, ["eval", "compare", str(judged), str(judged)])
    assert result.exit_code == 0, result.output
    assert "McNemar" in result.output
    degraded = tmp / "degraded.jsonl"
    lines = [json.loads(line) for line in judged.read_text().splitlines()]
    for line in lines:
        line["verdict"] = "fail"
    degraded.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    result = runner.invoke(app, ["eval", "compare", str(judged), str(degraded), "--fail-on", "any"])
    assert result.exit_code == 1 and "REGRESSION" in result.output
    # improvement direction does not fail
    assert runner.invoke(app, ["eval", "compare", str(degraded), str(judged), "--fail-on", "any"]).exit_code == 0
    # raw run files need --gold
    result = runner.invoke(app, ["eval", "compare", str(runs), str(runs)])
    assert result.exit_code == 1 and "--gold" in result.output
    assert runner.invoke(app, ["eval", "compare", str(runs), str(runs), "--gold", gold]).exit_code == 0

    labels = tmp / "labels.yaml"
    result = runner.invoke(app, ["eval", "calibrate", "export", "--gold", gold, "--runs", str(runs),
                                 "--judged", str(judged), "--out", str(labels), "--n", "6"])
    assert result.exit_code == 0 and "Exported 6 items" in result.output
    data = yaml.safe_load(labels.read_text())
    judged_by_id = {f"{j['question_id']}#{j['repeat']}": j for j in lines_of(judged)}
    for item in data["items"]:
        item["correct"] = judged_by_id[item["id"]]["verdict"] == "pass"
    labels.write_text(yaml.safe_dump(data))
    result = runner.invoke(app, ["eval", "calibrate", "score", "--labels", str(labels), "--judged", str(judged)])
    assert result.exit_code == 0 and "kappa" in result.output


def lines_of(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_bad_inputs(fake_env, fixtures_dir):
    result = runner.invoke(app, ["eval", "run", "--corpus", str(fixtures_dir / "corpus"),
                                 "--gold", str(fake_env / "missing.yaml"), "--out", str(fake_env / "o.jsonl")])
    assert result.exit_code == 1 and "cannot read gold set" in result.output
    result = runner.invoke(app, ["eval", "run", "--corpus", str(fixtures_dir / "corpus"),
                                 "--gold", str(fixtures_dir / "gold.yaml"), "--split", "bogus"])
    assert result.exit_code == 1


def test_draft_then_review_via_cli(monkeypatch, fake_env, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "handbook.pdf").write_text(
        "Vacation Policy\n\nEmployees receive 25 days of paid vacation per year. "
        "Unused vacation days do not carry over past March 31 of the next year.\n"
    )
    reply = json.dumps({"question": "How many vacation days do employees get per year?",
                        "quote": "25 days of paid vacation per year", "facts": ["25"], "type": "single_fact"})
    from docket.inference.gateway import FakeInferenceGateway

    class DraftGateway(FakeInferenceGateway):
        def generate(self, *, system, prompt, **opts):
            return reply

    monkeypatch.setattr(eval_cli, "_make_gateway", lambda model: DraftGateway())
    out = tmp_path / "draft.yaml"
    result = runner.invoke(app, ["eval", "draft", "--corpus", str(corpus), "--out", str(out), "--count", "2"])
    assert result.exit_code == 0, result.output
    assert "1 drafts written" in result.output
    (q,) = load_gold_set(out).questions
    assert not q.reviewed

    result = runner.invoke(app, ["eval", "review", str(out)], input="a\n")
    assert result.exit_code == 0, result.output
    assert "accepted 1" in result.output and load_gold_set(out).questions[0].reviewed

    # Re-drafting into the same file dedupes against what is there.
    result = runner.invoke(app, ["eval", "draft", "--corpus", str(corpus), "--out", str(out), "--count", "2"])
    assert "0 drafts written" in result.output and "duplicate" in result.output
