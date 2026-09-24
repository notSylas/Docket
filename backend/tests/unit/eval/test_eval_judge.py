"""LLM judge: parsing, retry, doubt, cross-check, judged report."""

from __future__ import annotations

import json

from docket.eval.judge import (
    ClaimJudge,
    JudgeVerdict,
    claims_for,
    judge_runs,
    load_judged,
    parse_judge_output,
)
from docket.eval.report import build_report, format_report
from docket.eval.schema import GoldSet, Question
from docket.eval.scoring import Verdict, score_run

SPAN = "25 days of paid vacation per year"
CHUNK = "Employees receive 25 days of paid vacation per year."


class QueueGateway:
    """Replies from a queue (last reply repeats); records every call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def generate(self, *, system, prompt, **opts):
        self.calls.append({"system": system, "prompt": prompt, **opts})
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def embed(self, text):
        return [0.0]


def yes(reason="ok"):
    return json.dumps({"supported": True, "reason": reason})


NO = json.dumps({"supported": False, "reason": "nope"})


def _q(qid="p", **kw) -> Question:
    base = dict(id=qid, type="single_fact", question="What do staff get?", answerable=True,
                must_contain=["vacation"], gold_spans=[SPAN], split="dev")
    base.update(kw)
    return Question(**base)


def _paraphrase(make_record, qid="p", repeat=0):
    """Deterministic check cannot decide: 'vacation' is missing, but the answer may be a paraphrase."""
    return make_record(question_id=qid, repeat=repeat, answer="Staff get 25 days off. [f #c0]",
                       chunks=[CHUNK], cited=[0])


def test_parse_judge_output_variants():
    assert parse_judge_output('{"supported": true, "reason": "r"}') == (True, "r")
    assert parse_judge_output('```json\n{"supported": false}\n```') == (False, "")
    assert parse_judge_output('<think>hmm {"supported": false}</think>{"supported": "true"}') == (True, "")
    assert parse_judge_output('Sure! {"supported": true, "reason": "x"} hope that helps') == (True, "x")
    assert parse_judge_output("not json") is None
    assert parse_judge_output('{"supported": "maybe"}') is None
    assert parse_judge_output('{"other": 1}') is None
    assert parse_judge_output("") is None


def test_claim_judge_uses_deterministic_json_opts_and_retries(make_record):
    q = _q()
    record = _paraphrase(make_record)
    (claim,) = claims_for(q, record, score_run(q, record))
    assert claim.kind == "fact"
    gateway = QueueGateway(["garbage", yes("fine")])
    verdict = ClaimJudge(gateway, "j").judge(claim)
    assert verdict.supported is True and verdict.attempts == 2
    first = gateway.calls[0]
    assert first["think"] is False and first["options"] == {"temperature": 0} and first["format"] == "json"
    assert "not valid JSON" in gateway.calls[1]["prompt"]


def test_claim_judge_doubt_after_exhausted_retries(make_record):
    q = _q()
    record = _paraphrase(make_record)
    (claim,) = claims_for(q, record, score_run(q, record))
    gateway = QueueGateway(["nope"])
    verdict = ClaimJudge(gateway, "j", max_attempts=3).judge(claim)
    assert verdict.supported is None and verdict.attempts == 3 and len(gateway.calls) == 3


def test_claims_only_for_needs_judge(make_record):
    q = _q(must_contain=["25"])
    record = make_record(answer="25 days [f #c0]", chunks=[CHUNK], cited=[0])
    assert score_run(q, record).verdict is Verdict.PASS
    assert claims_for(q, record, score_run(q, record)) == []


def test_support_claim_when_cited_chunk_lacks_gold_span(make_record):
    q = _q(must_contain=["25"])
    record = make_record(answer="25 days [f #c1]", chunks=[CHUNK, "Unrelated text about parking."], cited=[1])
    score = score_run(q, record)
    assert score.verdict is Verdict.NEEDS_JUDGE
    (claim,) = claims_for(q, record, score)
    assert claim.kind == "support" and "parking" in claim.prompt


def test_judge_runs_verdicts_and_file(make_record, tmp_path):
    gold = GoldSet(questions=[_q("p"), _q("f"), _q("d"), _q("det", must_contain=["25"])])
    records = [
        _paraphrase(make_record, "p"),
        _paraphrase(make_record, "f"),
        _paraphrase(make_record, "d"),
        make_record(question_id="det", answer="25 days [f #c0]", chunks=[CHUNK], cited=[0]),
    ]

    # Route by call order instead: p -> yes, f -> no, d -> garbage.
    gateway = QueueGateway([yes(), NO, "garbage"])
    out = tmp_path / "judged.jsonl"
    judged = judge_runs(gold, records, ClaimJudge(gateway, "j", max_attempts=1), out_path=out)
    by_id = {j.question_id: j for j in judged}
    assert by_id["p"].verdict is JudgeVerdict.PASS and by_id["p"].source == "judge"
    assert by_id["f"].verdict is JudgeVerdict.FAIL
    assert by_id["d"].verdict is JudgeVerdict.DOUBT
    assert by_id["det"].verdict is JudgeVerdict.PASS and by_id["det"].source == "deterministic"
    assert [j.model_dump() for j in load_judged(out)] == [j.model_dump() for j in judged]
    assert len(gateway.calls) == 3  # deterministic runs cost no judge call


def test_cross_check_records_disagreement(make_record):
    gold = GoldSet(questions=[_q("a"), _q("b")])
    records = [_paraphrase(make_record, "a"), _paraphrase(make_record, "b")]
    primary = ClaimJudge(QueueGateway([yes()]), "big")
    cross = ClaimJudge(QueueGateway([yes(), NO]), "small")  # agrees on a, disagrees on b
    judged = {j.question_id: j for j in judge_runs(gold, records, primary, cross)}
    assert not judged["a"].disagreement and judged["b"].disagreement
    assert judged["b"].verdict is JudgeVerdict.PASS  # primary verdict wins
    assert judged["b"].judge_models == ["big", "small"]
    assert set(judged["b"].claims[0].verdicts) == {"big", "small"}


def test_judged_results_replace_bracket_in_report(make_record):
    gold = GoldSet(questions=[_q("p"), _q("f")])
    records = [_paraphrase(make_record, "p"), _paraphrase(make_record, "f")]
    plain = build_report(gold, records)
    assert plain.overall.strict.passed == 0 and plain.overall.optimistic.passed == 2

    judged = judge_runs(gold, records, ClaimJudge(QueueGateway([yes(), NO]), "j"))
    report = build_report(gold, records, judged)
    assert report.overall.strict.passed == 1 and report.overall.optimistic.passed == 1
    assert report.judged_runs == 2 and report.failures["judge_doubt"] == 0
    assert "Accuracy (judged)" in format_report(report)
    assert "Accuracy strict" in format_report(plain)


def test_judge_doubt_keeps_bracket_open(make_record):
    gold = GoldSet(questions=[_q("d")])
    records = [_paraphrase(make_record, "d")]
    judged = judge_runs(gold, records, ClaimJudge(QueueGateway(["junk"]), "j", max_attempts=1))
    report = build_report(gold, records, judged)
    assert report.overall.strict.passed == 0 and report.overall.optimistic.passed == 1
    assert report.failures["judge_doubt"] == 1
