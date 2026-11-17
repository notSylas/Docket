"""Blind label sheet: export, key separation, mutations, import and kappa scoring.

All labels in this file are SYNTHETIC test fixtures; none are results.
"""

from __future__ import annotations

import csv
import json
import os
import random
import stat

import pytest

from docket.eval.calibration_sheet import (
    MODEL_TO_MODEL_NOTE,
    SheetSource,
    discover_sources,
    export_label_sheet,
    format_sheet_score,
    load_key,
    load_returned_labels,
    mutate_unsupported,
    mutate_wrong_entity,
    mutate_wrong_number,
    score_label_sheet,
)
from docket.eval.judge import JudgedRun, JudgeVerdict, write_judged
from docket.eval.schema import GoldSetError, fingerprint, load_gold_set

ORG_A = "Acme Holdings Inc."
ORG_B = "Zenith Widgets LLC"


def build_results(tmp_path, fixtures_dir, make_record):
    """A tiny saved-results tree: gold, runs, judged (qwen3:14b) for the fixture gold set."""
    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    gold_path = gold_dir / "gold-fake.yaml"
    body = "version: 1\nquestions:\n" + "".join(
        f"  - id: q{i}\n    type: single_fact\n    question: What does supplier {i} get paid?\n"
        f"    answerable: true\n    must_contain: ['1,200']\n    gold_spans: ['agrees to pay 1,200 dollars to the supplier']\n" for i in range(12)
    ) + "  - id: absent\n    type: out_of_corpus\n    question: Who is the CEO?\n    answerable: false\n"
    gold_path.write_text(body)
    gold = load_gold_set(gold_path)
    digest = fingerprint(gold)
    results = tmp_path / "results"
    results.mkdir()
    records, judged = [], []
    answerable = [q for q in gold.questions if q.answerable]
    for i, q in enumerate(answerable * 2):
        doc = f"Doc{i % 3}"
        passage = f"{ORG_A if i % 2 else ORG_B} agrees to pay 1,200 dollars to the supplier {doc}."
        rec = make_record(question_id=q.id, repeat=i // len(answerable),
                          answer=f"{ORG_A if i % 2 else ORG_B} will pay 1,200 dollars [f #c0]",
                          chunks=[passage], cited=[0], gold_fingerprint=digest,
                          configuration={"gen_model": "qwen3:14b"})
        rec.retrieved[0].source_display_name = doc
        records.append(rec)
        verdict = JudgeVerdict.PASS if i % 3 else JudgeVerdict.FAIL
        judged.append(JudgedRun(question_id=q.id, repeat=rec.repeat, verdict=verdict, source="judge",
                                judge_models=["qwen3:14b"], record_fingerprint=fingerprint(rec),
                                gold_fingerprint=digest, answer_model="qwen3:14b", self_judged=True))
    for q in (x for x in gold.questions if not x.answerable):
        for r in range(2):
            records.append(make_record(question_id=q.id, repeat=r, answer="Not in the documents.",
                                       chunks=["unrelated text"], abstained=True, gold_fingerprint=digest))
    with (results / "run-a.jsonl").open("w") as fh:
        for r in records:
            fh.write(r.model_dump_json() + "\n")
    write_judged(results / "run-a.j14.jsonl", judged)
    return results, gold_dir


@pytest.fixture
def sheet(tmp_path, fixtures_dir, make_record):
    results, gold_dir = build_results(tmp_path, fixtures_dir, make_record)
    sources = discover_sources(results, gold_dir)
    out = tmp_path / "out" / "labels"
    summary = export_label_sheet(sources, out, n=14, seeded=4, seed=3)
    return sources, out, summary


def test_discover_pairs_runs_judged_and_gold(tmp_path, fixtures_dir, make_record):
    results, gold_dir = build_results(tmp_path, fixtures_dir, make_record)
    (src,) = discover_sources(results, gold_dir)
    assert src.judged_path and src.judged_path.name == "run-a.j14.jsonl" and src.gold_path.name == "gold-fake.yaml"


def test_export_files_blind_private_and_keyed(sheet):
    _, out, summary = sheet
    nseed = summary["seeded"]
    assert nseed >= 3 and summary["items"] == 10 + nseed
    for name in ("label_sheet.csv", "label_sheet.jsonl", "labels_key.jsonl", "LABELING_INSTRUCTIONS.txt"):
        assert stat.S_IMODE(os.stat(out / name).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o700
    assert not list(out.glob("*.md"))
    rows = list(csv.DictReader((out / "label_sheet.csv").open(newline="")))
    assert len(rows) == summary["items"] and all(r["correct"] == "" and r["notes"] == "" for r in rows)
    assert all(r["cited_passages"] for r in rows)
    blind = (out / "label_sheet.csv").read_text() + (out / "label_sheet.jsonl").read_text()
    for leak in ("judge_pass", "qwen3", "seeded", "verdict", "stratum", "wrong_number", "unsupported"):
        assert leak not in blind
    key = load_key(out / "labels_key.jsonl")
    assert len(key) == summary["items"] and sum(k["kind"] == "seeded" for k in key.values()) == nseed
    assert all(k["seeded"]["expected_correct"] is False for k in key.values() if k["kind"] == "seeded")
    assert all(k["judge_verdict"] in ("pass", "fail") for k in key.values())
    assert {k["stratum"] for k in key.values()} >= {"pass", "fail", "absence_uncited"}


def test_export_is_deterministic(tmp_path, fixtures_dir, make_record):
    results, gold_dir = build_results(tmp_path, fixtures_dir, make_record)
    srcs = discover_sources(results, gold_dir)
    export_label_sheet(srcs, tmp_path / "o1", n=14, seeded=4, seed=5)
    export_label_sheet(srcs, tmp_path / "o2", n=14, seeded=4, seed=5)
    assert (tmp_path / "o1/label_sheet.csv").read_text() == (tmp_path / "o2/label_sheet.csv").read_text()


def test_mutations():
    rng = random.Random(0)
    new, detail = mutate_wrong_number("They pay 1,200 dollars [f #c0]", "pay 1,200 dollars", rng)
    assert "1,200" not in new and "[f #c0]" in new and "->" in detail
    assert mutate_wrong_number("no digits here [f #c1]", "", rng) is None
    new, _ = mutate_wrong_entity(f"{ORG_A} pays", f"{ORG_A} agrees", frozenset({"DocA"}), {"DocA": {ORG_A}, "DocB": {ORG_B}}, rng)
    assert ORG_B in new and ORG_A not in new
    assert mutate_wrong_entity(f"{ORG_A} pays", ORG_A, frozenset({"DocA"}), {"DocA": {ORG_A}}, rng) is None
    new, _ = mutate_unsupported("They pay.", "text", rng)
    assert new.startswith("They pay. ") and len(new) > 30


def test_import_validation(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("item_id,correct\nL001,yes\nL002,\nL003,No\n")
    assert load_returned_labels(p) == {"L001": True, "L002": None, "L003": False}
    p.write_text("item_id,correct\nL001,maybe\n")
    with pytest.raises(GoldSetError, match="not yes/no"):
        load_returned_labels(p)
    p.write_text("item_id,correct\nL001,yes\nL001,no\n")
    with pytest.raises(GoldSetError, match="duplicate"):
        load_returned_labels(p)
    j = tmp_path / "s.jsonl"
    j.write_text('{"item_id": "L1", "correct": true}\n{"item_id": "L2", "correct": null}\n')
    assert load_returned_labels(j) == {"L1": True, "L2": None}


def _key(rows):
    return {f"L{i}": {"item_id": f"L{i}", "kind": k, "judge_label": "m", "judge_verdict": v,
                      "seeded": {"mutation": "wrong_number"} if k == "seeded" else None, "self_judged": False}
            for i, (k, v) in enumerate(rows)}


def test_score_known_answer_synthetic_labels():
    # SYNTHETIC: 10 real items, judge pass/fail vs fake human labels (8 agree), plus 2 seeded
    key = _key([("real", "pass")] * 5 + [("real", "fail")] * 5 + [("seeded", "pass")] * 2 + [("real", "doubt")])
    labels = {f"L{i}": v for i, v in enumerate([True] * 4 + [False] + [False] * 4 + [True] + [False, False] + [True])}
    s = score_label_sheet(labels, key, n_boot=500)
    (j,) = s.judges
    assert (j.n, j.excluded_doubt) == (10, 1) and j.agreement == pytest.approx(0.8)
    assert j.kappa == pytest.approx(0.6)  # observed .8, expected .5
    assert j.kappa_ci[0] <= j.kappa <= j.kappa_ci[1] and not j.meets_target
    assert j.confusion["human=pass,judge=pass"] == 4 and j.confusion["human=pass,judge=doubt"] == 1
    assert (s.seeded_labeled, s.seeded_caught) == (2, 2) and s.real_labeled == 11
    text = format_sheet_score(s)
    assert MODEL_TO_MODEL_NOTE in text and "HUMAN" in text and "BELOW" in text


def test_score_rejects_unknown_and_empty():
    key = _key([("real", "pass")])
    with pytest.raises(GoldSetError, match="unknown"):
        score_label_sheet({"ZZ": True}, key)
    with pytest.raises(GoldSetError, match="no labeled"):
        score_label_sheet({"L0": None}, key)


def test_end_to_end_with_synthetic_returned_sheet(sheet, tmp_path):
    _, out, summary = sheet
    key = load_key(out / "labels_key.jsonl")
    rows = list(csv.DictReader((out / "label_sheet.csv").open(newline="")))
    for r in rows:  # SYNTHETIC labels: copy the judge verdict, seeded -> no
        k = key[r["item_id"]]
        r["correct"] = "no" if k["kind"] == "seeded" else ("yes" if k["judge_verdict"] == "pass" else "no")
    ret = tmp_path / "returned.csv"
    with ret.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    s = score_label_sheet(load_returned_labels(ret), key, n_boot=200)
    assert s.unlabeled == 0 and s.seeded_caught == s.seeded_labeled == summary["seeded"]
    assert all(j.agreement is not None for j in s.judges)
