"""Report aggregation, failure classification, and rendering."""

from __future__ import annotations

import json

import pytest

from docket.eval.report import (
    FAILURE_CATEGORIES,
    build_report,
    classify_failure,
    format_report,
    report_to_dict,
)
from docket.eval.schema import GoldSet, Question
from docket.eval.scoring import score_run
from docket.services.query.prompts import ABSTENTION_PHRASE

SPAN = "25 days of paid vacation per year"
CHUNK = "Employees receive 25 days of paid vacation per year."


def _answerable(qid="a", **kw) -> Question:
    base = dict(id=qid, type="single_fact", question="Q?", answerable=True,
                must_contain=["25"], gold_spans=[SPAN], split="dev")
    base.update(kw)
    return Question(**base)


def _unanswerable(qid="u") -> Question:
    return Question(id=qid, type="out_of_corpus", question="Q?", answerable=False, split="test")


def _good(make_record, qid, repeat=0):
    return make_record(
        question_id=qid, repeat=repeat, answer="25 days [f #c0]", chunks=[CHUNK], cited=[0],
        prompt=f"Context:\n[f #c0]\n{CHUNK}\n\nQuestion: q\n\nAnswer:", spans_indexed=[True],
    )


def _category(question, record):
    return classify_failure(question, record, score_run(question, record))


def test_failure_ladder_each_category(make_record):
    q = _answerable()
    ctx = f"Context:\n[f #c0]\n{CHUNK}\n\nQuestion: q\n\nAnswer:"
    good_kw = dict(chunks=[CHUNK], prompt=ctx, spans_indexed=[True])

    assert _category(q, _good(make_record, "a")) is None
    assert _category(q, make_record(answer="x", spans_indexed=[False], chunks=[CHUNK])) == "parse"
    assert _category(q, make_record(answer="x", chunks=["other"], spans_indexed=[True])) == "retrieval_miss"
    assert _category(q, make_record(answer="x", chunks=[CHUNK], prompt="Context:\nnothing\n\nQuestion: q",
                                    spans_indexed=[True])) == "context_truncation"
    truncated = make_record(answer="thirty [f #c0]", cited=[0], system="", prompt=ctx + "y" * 4000,
                            prompt_eval_count=100, chunks=[CHUNK], spans_indexed=[True])
    assert _category(q, truncated) == "context_truncation"
    assert _category(q, make_record(answer="thirty [f #c0]", cited=[0], **good_kw)) == "generation_error"
    assert _category(q, make_record(answer=ABSTENTION_PHRASE, **good_kw)) == "generation_error"
    assert _category(q, make_record(answer="25 days", **good_kw)) == "citation_error"
    assert _category(_answerable(must_contain=["twenty-five"]),
                     make_record(answer="25 days [f #c0]", cited=[0], **good_kw)) == "judge_doubt"
    assert _category(q, make_record(error="down", **good_kw)) == "run_error"
    assert _category(_unanswerable(), make_record(answer="Bob [f #c0]")) == "generation_error"
    assert set(FAILURE_CATEGORIES) >= {"parse", "retrieval_miss", "context_truncation",
                                       "generation_error", "citation_error", "judge_doubt"}


def test_report_rates_slices_and_metrics(make_record):
    gold = GoldSet(questions=[
        _answerable("a1"), _answerable("a2"), _answerable("a3", type="numeric", split="test"),
        _unanswerable("u1"), _unanswerable("u2"),
    ])
    good, ctx = _good, f"Context:\n[f #c0]\n{CHUNK}\n\nQuestion: q\n\nAnswer:"
    records = [
        # a1: 3/3 pass
        *[good(make_record, "a1", r) for r in range(3)],
        # a2: passes 2 of 3 -> majority pass, not pass-all
        good(make_record, "a2", 0), good(make_record, "a2", 1),
        make_record(question_id="a2", repeat=2, answer="thirty [f #c0]", cited=[0],
                    chunks=[CHUNK], prompt=ctx, spans_indexed=[True]),
        # a3: retrieval miss on every repeat
        *[make_record(question_id="a3", repeat=r, answer="dunno", chunks=["other"],
                      prompt="Context:\nother\n\nQuestion: q", spans_indexed=[True]) for r in range(3)],
        # u1 abstains correctly, u2 answers wrongly (all repeats)
        *[make_record(question_id="u1", repeat=r, answer=ABSTENTION_PHRASE) for r in range(3)],
        *[make_record(question_id="u2", repeat=r, answer="Bob") for r in range(3)],
    ]
    report = build_report(gold, records)

    assert report.repeats == 3
    o = report.overall
    assert (o.questions, o.strict.passed, o.pass_all.passed) == (5, 3, 2)  # a1,a2,u1 / a1,u1
    assert o.strict.lo < 0.6 < o.strict.hi
    assert report.by_type["single_fact"].questions == 2
    assert report.by_type["numeric"].strict.passed == 0
    assert report.by_split["dev"].questions == 2 and report.by_split["test"].questions == 3

    # 3 answerable questions x 3 repeats = 9 span runs; a3's 3 miss retrieval
    assert report.recall_any.total == 9 and report.recall_any.passed == 6
    assert report.fact_in_context.passed == 6
    # runs abstaining: u1 x3 (correct) + a3 "dunno" is not the phrase -> precision 3/3
    assert (report.abstention_precision.passed, report.abstention_precision.total) == (3, 3)
    assert (report.abstention_recall.passed, report.abstention_recall.total) == (3, 6)
    assert report.wrongful_abstention.passed == 0

    assert report.failures["retrieval_miss"] == 3
    assert report.failures["generation_error"] == 1 + 3  # a2's bad repeat + u2 x3
    assert report.failures_by_type["numeric"]["retrieval_miss"] == 3


def test_needs_judge_brackets_the_headline(make_record):
    gold = GoldSet(questions=[_answerable("a", must_contain=["twenty-five"])])
    record = make_record(question_id="a", answer="25 days [f #c0]", chunks=[CHUNK], cited=[0],
                         prompt=f"Context:\n{CHUNK}\n\nQuestion: q", spans_indexed=[True])
    report = build_report(gold, [record])
    assert report.overall.strict.passed == 0
    assert report.overall.optimistic.passed == 1
    assert report.overall.needs_judge_runs == 1
    assert report.failures["judge_doubt"] == 1


def test_records_for_unknown_questions_are_ignored(make_record):
    gold = GoldSet(questions=[_answerable("a")])
    report = build_report(gold, [_good(make_record, "a"), _good(make_record, "ghost")])
    assert report.overall.questions == 1


def test_empty_report_does_not_crash():
    report = build_report(GoldSet(questions=[_answerable("a")]), [])
    assert report.overall.questions == 0
    assert "n/a" in format_report(report)


def test_format_and_dict_output(make_record):
    gold = GoldSet(questions=[_answerable("a"), _unanswerable("u")])
    records = [_good(make_record, "a"), make_record(question_id="u", answer=ABSTENTION_PHRASE)]
    report = build_report(gold, records)
    text = format_report(report)
    for needle in ("Accuracy strict", "By type:", "By split:", "recall@", "Fact-in-context",
                   "Abstention precision", "Failure classification", "retrieval_miss"):
        assert needle in text
    assert "100.0% (2/2)" in text
    data = report_to_dict(report)
    assert json.loads(json.dumps(data))["overall"]["strict"]["passed"] == 2
    assert data["overall"]["strict"]["lo"] == pytest.approx(0.3424, abs=1e-3)
