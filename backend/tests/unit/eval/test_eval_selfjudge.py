"""Self-judge policy: refused by default, allowed with an explicit flag."""

from __future__ import annotations

import pytest
from test_eval_cli import fake_env  # noqa: F401
from test_eval_judge import QueueGateway, yes
from typer.testing import CliRunner

from docket.eval import cli as eval_cli
from docket.eval.judge import (
    INDEPENDENT_JUDGE_MODEL,
    SelfJudgeError,
    require_independent_judge,
)
from docket.interfaces.cli.main import app

runner = CliRunner()


def test_require_independent_judge_policy():
    assert require_independent_judge({"qwen3:14b"}, ["gemma3:12b"]) == []
    with pytest.raises(SelfJudgeError, match="--allow-self-judge"):
        require_independent_judge({"qwen3:14b"}, ["qwen3:14b"])
    assert require_independent_judge({"qwen3:14b"}, ["qwen3:14b"], allow_self_judge=True) == ["qwen3:14b"]
    assert INDEPENDENT_JUDGE_MODEL == "gemma3:12b"


def _runs(fake_env, fixtures_dir):
    gold = str(fixtures_dir / "gold.yaml")
    runs = fake_env / "runs.jsonl"
    corpus = fixtures_dir / "corpus"
    result = runner.invoke(app, ["eval", "run", "--gold", gold, "--corpus", str(corpus), "--out", str(runs),
                                 "--repeats", "1", "--model", "qwen3:14b"])
    assert result.exit_code == 0, result.output
    return gold, runs


def test_cli_judge_refuses_self_judging_by_default(fake_env, fixtures_dir, monkeypatch):  # noqa: F811
    gold, runs = _runs(fake_env, fixtures_dir)
    monkeypatch.setattr(eval_cli, "_make_gateway", lambda model: QueueGateway([yes()]))
    out = fake_env / "judged.jsonl"
    args = ["eval", "judge", "--gold", gold, "--runs", str(runs), "--out", str(out)]
    refused = runner.invoke(app, [*args, "--model", "qwen3:14b"])
    assert refused.exit_code == 1 and "self-judging refused" in refused.output and not out.exists()
    allowed = runner.invoke(app, [*args, "--model", "qwen3:14b", "--allow-self-judge"])
    assert allowed.exit_code == 0 and out.exists()
    independent = runner.invoke(app, [*args, "--model", "gemma3:12b", "--out", str(fake_env / "j2.jsonl")])
    assert independent.exit_code == 0, independent.output
