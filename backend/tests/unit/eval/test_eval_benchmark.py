"""Frozen inputs, document holdouts, and the accuracy acceptance boundary."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import docket.eval.benchmark as benchmark_module
from docket.eval.benchmark import FrozenBenchmark, corpus_hashes, freeze_benchmark, load_benchmark, verify_benchmark
from docket.eval.calibration import CalibrationResult
from docket.eval.judge import ClaimJudge, judge_runs
from docket.eval.report import build_report
from docket.eval.schema import GoldSet, GoldSetError, Question, fingerprint
from docket.ingestion.pipeline import SUPPORTED_EXTENSIONS
from docket.services.query.prompts import ABSTENTION_PHRASE


def test_benchmark_reuses_the_canonical_supported_extensions_constant():
    # Not a re-typed literal: the same object ingestion.pipeline defines.
    assert benchmark_module.SUPPORTED_EXTENSIONS is SUPPORTED_EXTENSIONS


def test_corpus_hashes_rejects_a_missing_corpus_folder(tmp_path):
    with pytest.raises(GoldSetError, match="corpus folder does not exist"):
        corpus_hashes(tmp_path / "missing")

SPAN = "25 days of paid vacation per year"


def question(qid="q", **updates):
    data = dict(id=qid, type="single_fact", question="How many vacation days?", answerable=True,
                must_contain=["25"], gold_spans=[SPAN], reviewed=True, split="test",
                source_documents=["handbook.pdf"], formula_dependent=False)
    data.update(updates)
    return Question(**data)


def test_freeze_rejects_changed_gold_or_corpus_and_overwrite(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "handbook.pdf"
    source.write_bytes(b"fixed source bytes")
    gold = GoldSet(population="real_user_documents", questions=[question()])
    path = tmp_path / "freeze.json"
    frozen = freeze_benchmark(gold, corpus, path)
    assert verify_benchmark(load_benchmark(path), gold, corpus) == fingerprint(frozen)
    with pytest.raises(FileExistsError):
        freeze_benchmark(gold, corpus, path)
    changed = gold.model_copy(update={"population": "textbooks"})
    with pytest.raises(GoldSetError, match="gold set changed"):
        verify_benchmark(frozen, changed, corpus)
    source.write_bytes(b"changed source bytes")
    with pytest.raises(GoldSetError, match="corpus files changed"):
        verify_benchmark(frozen, gold, corpus)


def test_freeze_requires_reviewed_document_attribution(tmp_path):
    (tmp_path / "handbook.pdf").write_bytes(b"source")
    with pytest.raises(GoldSetError, match="reviewed"):
        freeze_benchmark(GoldSet(questions=[question(reviewed=False)]), tmp_path, tmp_path / "f.json")
    with pytest.raises(GoldSetError, match="source_documents"):
        freeze_benchmark(GoldSet(questions=[question(source_documents=[])]), tmp_path, tmp_path / "f.json")


def test_document_split_cannot_leak_between_dev_and_test():
    with pytest.raises(ValidationError, match="both dev and test"):
        GoldSet(questions=[question("dev", split="dev"), question("test", split="test")])
    a, b = question("one", split=None), question("two", split=None)
    assert a.split == b.split


class YesGateway:
    def generate(self, **kwargs):
        return '{"supported": true, "reason": "supported by the cited passage"}'


def accepted_inputs(make_record):
    questions = [question(f"q{i}", formula_dependent=(i == 0)) for i in range(198)]
    questions.extend([
        Question(id="outside", type="out_of_corpus", question="Who is CEO?", answerable=False,
                 reviewed=True, split="test", formula_dependent=False),
        Question(id="revoked", type="revoked", question="How many vacation days?", answerable=False,
                 reviewed=True, split="test", formula_dependent=False, setup={"revoke": ["handbook"]}),
    ])
    gold = GoldSet(population="real_user_documents", questions=questions)
    frozen = FrozenBenchmark(gold_fingerprint=fingerprint(gold), files={"handbook.pdf": "a" * 64})
    records = []
    for i, q in enumerate(questions):
        for repeat in range(3):
            answer = "25 days [f #c0]" if i < 188 else ABSTENTION_PHRASE
            records.append(make_record(
                question_id=q.id, repeat=repeat, answer=answer,
                chunks=[SPAN] if q.answerable else [], cited=[0] if i < 188 else [],
                gold_fingerprint=fingerprint(gold), benchmark_fingerprint=fingerprint(frozen),
                configuration={"gen_model": "fixture", "requested_mode": "fast"},
            ))
    judgments = judge_runs(gold, records, ClaimJudge(YesGateway(), "fixture-judge"))
    calibration = CalibrationResult(
        labeled=30, unlabeled=0, agreement=1.0, kappa=1.0,
        judge_items=30, judge_agreement=1.0, judge_kappa=1.0,
        distinct_judge_questions=30, verified_judge_items=30, judge_label_classes=2, judge_models=["fixture-judge"],
    )
    return gold, frozen, records, judgments, calibration


def test_milestone_requires_frozen_calibrated_complete_test_run(make_record):
    gold, frozen, records, judgments, calibration = accepted_inputs(make_record)
    report = build_report(gold, records, judgments, benchmark=frozen, calibration=calibration)
    assert report.milestone["passed"]
    assert report.by_split["test"].strict.passed == 190
    assert report.by_split["test"].strict.lo >= 0.90
    assert report.by_formula["formula_dependent"].questions == 1
    assert report.by_document["handbook.pdf"].questions == 198
    assert not build_report(gold, records, judgments).milestone["passed"]
    incomplete = build_report(gold, records[:-1], judgments, benchmark=frozen, calibration=calibration)
    assert not incomplete.milestone["passed"]
    assert any("three distinct repeats" in reason for reason in incomplete.milestone["reasons"])
    legacy = [j.model_copy(update={"support_checked": False}) for j in judgments]
    assert not build_report(gold, records, legacy, benchmark=frozen, calibration=calibration).milestone["passed"]


def test_duplicate_repeats_cannot_inflate_accuracy(make_record):
    record = make_record(question_id="q", answer="25 [f #c0]", chunks=[SPAN], cited=[0])
    with pytest.raises(GoldSetError, match="duplicate run"):
        build_report(GoldSet(questions=[question()]), [record, record])


def test_raw_high_accuracy_cannot_satisfy_milestone(make_record):
    gold, frozen, records, _, calibration = accepted_inputs(make_record)
    report = build_report(gold, records, benchmark=frozen, calibration=calibration)
    assert not report.milestone["passed"]
    assert any("judged" in reason for reason in report.milestone["reasons"])


def test_review_save_preserves_population_split_and_formula_labels(tmp_path):
    import yaml
    from docket.eval.draft import save_gold_file
    from docket.eval.schema import load_gold_set
    gold = GoldSet(population="real_user_documents", questions=[question(formula_dependent=True)])
    path = tmp_path / "gold.yaml"
    path.write_text(yaml.safe_dump(gold.model_dump(mode="json")))
    save_gold_file(path, gold.questions)
    saved = load_gold_set(path)
    assert saved.population == gold.population
    assert saved.questions[0].split == gold.questions[0].split
    assert saved.questions[0].formula_dependent is True
