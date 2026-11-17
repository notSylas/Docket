"""Output hygiene: eval files are 0600 in 0700 dirs; redaction strips text."""

from __future__ import annotations

import json
import os
import stat

import pytest
from test_eval_cli import fake_env  # noqa: F401
from test_eval_judge import QueueGateway, yes
from typer.testing import CliRunner

from docket.eval import cli as eval_cli
from docket.eval.judge import JudgedRun, JudgeVerdict, load_judged, write_judged
from docket.eval.outputs import (
    REDACTED,
    ensure_private_dir,
    open_private,
    redact_dict,
    redact_record,
    write_private_text,
)
from docket.eval.schema import load_records
from docket.interfaces.cli.main import app

runner = CliRunner()


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_private_dir_and_file_modes(tmp_path):
    os.umask(0o022)
    target = ensure_private_dir(tmp_path / "a" / "b")
    assert mode(target) == 0o700 and mode(target.parent) == 0o700
    path = target / "x.txt"
    write_private_text(path, "hello")
    assert mode(path) == 0o600
    path.chmod(0o644)  # a pre-existing loose file is tightened on rewrite
    write_private_text(path, "again")
    assert mode(path) == 0o600 and path.read_text() == "again"


def test_existing_dir_untouched_and_create_only(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    d.chmod(0o755)
    ensure_private_dir(d)
    assert mode(d) == 0o755
    with open_private(d / "f", overwrite=False) as fh:
        fh.write("x")
    with pytest.raises(FileExistsError):
        open_private(d / "f", overwrite=False)


def test_write_judged_is_private(tmp_path):
    out = tmp_path / "out" / "j.jsonl"
    write_judged(out, [JudgedRun(question_id="q", repeat=0, verdict=JudgeVerdict.PASS, source="judge")])
    assert mode(out) == 0o600 and mode(out.parent) == 0o700
    assert len(load_judged(out)) == 1


def test_redact_record_and_dict(make_record):
    rec = make_record(answer="secret answer [f #c0]", chunks=["private passage"], cited=[0], prompt="p")
    red = redact_record(rec)
    assert red.answer == REDACTED and red.prompt is None and red.retrieved[0].text == ""
    assert red.retrieved[0].chunk_id == "c0" and red.citations == rec.citations
    assert "private passage" not in red.model_dump_json()
    d = redact_dict({"n": 3, "failures": [{"question": "q?", "answer": "a", "id": "x"}], "answer": ""})
    assert d["failures"][0] == {"question": REDACTED, "answer": REDACTED, "id": "x"} and d["n"] == 3


def test_cli_run_outputs_private_and_export_redacts(fake_env, fixtures_dir, monkeypatch):  # noqa: F811
    runs = fake_env / "eval-out" / "runs.jsonl"
    result = runner.invoke(app, ["eval", "run", "--corpus", str(fixtures_dir / "corpus"),
                                 "--gold", str(fixtures_dir / "gold.yaml"), "--repeats", "1",
                                 "--split", "all", "--out", str(runs)])
    assert result.exit_code == 0, result.output
    assert mode(runs) == 0o600 and mode(runs.parent) == 0o700
    assert mode(runs.with_name("runs.jsonl.manifest.json")) == 0o600

    out = fake_env / "shared" / "redacted.jsonl"
    result = runner.invoke(app, ["eval", "export", "--runs", str(runs), "--out", str(out), "--redact"])
    assert result.exit_code == 0, result.output
    assert mode(out) == 0o600
    for rec in load_records(out):
        assert rec.prompt is None and all(c.text == "" for c in rec.retrieved)
    assert runner.invoke(app, ["eval", "export", "--runs", str(runs), "--out", str(out)]).exit_code == 1

    monkeypatch.setattr(eval_cli, "_make_gateway", lambda model: QueueGateway([yes()]))
    judged = fake_env / "eval-out" / "judged.jsonl"
    gold = str(fixtures_dir / "gold.yaml")
    assert runner.invoke(app, ["eval", "judge", "--gold", gold, "--runs", str(runs), "--out", str(judged)]).exit_code == 0
    assert mode(judged) == 0o600
    shared = fake_env / "shared" / "judged-redacted.jsonl"
    assert runner.invoke(app, ["eval", "export", "--runs", str(runs), "--judged", str(judged),
                               "--out", str(shared), "--redact"]).exit_code == 0
    assert all(json.loads(line)["question_id"] for line in shared.read_text().splitlines())
    rep = runner.invoke(app, ["eval", "report", "--gold", gold, "--runs", str(runs), "--json", "--redact"])
    assert rep.exit_code == 0 and "overall" in json.loads(rep.output)
