"""Calibration export/import and Cohen's kappa."""

from __future__ import annotations

import pytest
import yaml

from docket.eval.calibration import export_labels, sample_runs, score_labels
from docket.eval.judge import JudgedRun, JudgeVerdict
from docket.eval.schema import GoldSet, GoldSetError, Question

SPAN = "25 days of paid vacation per year"
CHUNK = "Employees receive 25 days of paid vacation per year."


def _run(qid, verdict, source, repeat=0):
    return JudgedRun(question_id=qid, repeat=repeat, verdict=verdict, source=source)


def _setup(make_record, n=12):
    questions, records, judged = [], [], []
    kinds = [
        (JudgeVerdict.PASS, "judge"), (JudgeVerdict.FAIL, "judge"), (JudgeVerdict.DOUBT, "judge"),
        (JudgeVerdict.PASS, "deterministic"), (JudgeVerdict.FAIL, "deterministic"),
    ]
    for i in range(n):
        qid = f"q{i}"
        questions.append(Question(id=qid, type="single_fact", question=f"Q{i}?", answerable=True,
                                  must_contain=["25"], gold_spans=[SPAN]))
        records.append(make_record(question_id=qid, answer=f"answer {i} [f #c0]", chunks=[CHUNK], cited=[0]))
        verdict, source = kinds[i % len(kinds)]
        judged.append(_run(qid, verdict, source))
    return GoldSet(questions=questions), records, judged


def test_sample_is_stratified_and_deterministic(make_record):
    _, _, judged = _setup(make_record, n=20)
    picked = sample_runs(judged, 10, seed=1)
    assert len(picked) == 10
    kinds = {(r.verdict, r.source) for r in picked}
    assert len(kinds) == 5  # all strata present
    assert [r.question_id for r in picked] == [r.question_id for r in sample_runs(judged, 10, seed=1)]
    assert len(sample_runs(judged, 100)) == 20  # capped by availability


def test_export_hides_verdicts_and_roundtrips_kappa(make_record, tmp_path):
    gold, records, judged = _setup(make_record, n=10)
    judged = [j for j in judged if j.verdict is not JudgeVerdict.DOUBT]  # users cannot label "doubt"
    path = tmp_path / "labels.yaml"
    assert export_labels(gold, records, judged, path, n=10) == 8
    text = path.read_text()
    assert "judge_pass" not in text and "verdict" not in text
    data = yaml.safe_load(text)
    items = data["items"]
    assert all(it["correct"] is None for it in items)
    assert items[0]["gold"]["quotes"] == [SPAN] and "[f #c0]" in items[0]["answer"]

    with pytest.raises(GoldSetError, match="no labeled items"):
        score_labels(path, judged)

    # User agrees with every automatic verdict.
    by_id = {f"{j.question_id}#{j.repeat}": j for j in judged}
    for it in items:
        it["correct"] = by_id[it["id"]].verdict is JudgeVerdict.PASS
    path.write_text(yaml.safe_dump(data))
    result = score_labels(path, judged)
    assert result.labeled == 8 and result.agreement == 1.0 and result.kappa == 1.0
    assert result.meets_target

    # Flip one judge-resolved label: agreement drops, kappa below 1, disagreement listed.
    target = next(it for it in items if by_id[it["id"]].source == "judge" and by_id[it["id"]].verdict is JudgeVerdict.PASS)
    target["correct"] = False
    path.write_text(yaml.safe_dump(data))
    result = score_labels(path, judged)
    assert result.agreement == 0.875 and result.kappa < 1.0
    assert result.disagreements == [f"{target['id']}: automatic=pass user=fail (judge)"]


def test_kappa_value_and_partial_labels(make_record, tmp_path):
    judged = [_run(f"q{i}", v, "judge") for i, v in enumerate(
        [JudgeVerdict.PASS] * 5 + [JudgeVerdict.FAIL] * 5)]
    labels = tmp_path / "l.yaml"
    user = [True] * 4 + [False] + [False] * 4 + [True]  # 8/10 agree
    items = [{"id": f"q{i}#0", "correct": u} for i, u in enumerate(user)] + [{"id": "zz#0", "correct": None}]
    labels.write_text(yaml.safe_dump({"items": items}))
    result = score_labels(labels, judged)
    assert result.labeled == 10 and result.unlabeled == 1
    assert result.agreement == 0.8 and result.kappa == pytest.approx(0.6)
    assert not result.meets_target


def test_bad_labels_rejected(tmp_path):
    p = tmp_path / "l.yaml"
    p.write_text(yaml.safe_dump({"items": [{"id": "a#0", "correct": "maybe"}]}))
    with pytest.raises(GoldSetError, match="true or false"):
        score_labels(p, [])
    p.write_text(yaml.safe_dump({"items": [{"id": "a#0", "correct": True}]}))
    with pytest.raises(GoldSetError, match="no matching run"):
        score_labels(p, [])
