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
    claim = claims_for(q, record, score_run(q, record))[0]
    assert claim.kind == "fact"
    gateway = QueueGateway(["garbage", yes("fine")])
    verdict = ClaimJudge(gateway, "j").judge(claim)
    assert verdict.supported is True and verdict.attempts == 2
    first = gateway.calls[0]
    assert first["think"] is False and first["options"] == {"temperature": 0, "num_ctx": 16384, "num_predict": 1024} and first["format"] == "json"
    assert "not valid JSON" in gateway.calls[1]["prompt"]


def test_claim_judge_doubt_after_exhausted_retries(make_record):
    q = _q()
    record = _paraphrase(make_record)
    claim = claims_for(q, record, score_run(q, record))[0]
    gateway = QueueGateway(["nope"])
    verdict = ClaimJudge(gateway, "j", max_attempts=3).judge(claim)
    assert verdict.supported is None and verdict.attempts == 3 and len(gateway.calls) == 3


def test_regex_pass_also_requires_semantic_support(make_record):
    q = _q(must_contain=["25"])
    record = make_record(answer="25 days [f #c0]", chunks=[CHUNK], cited=[0])
    assert score_run(q, record).verdict is Verdict.PASS
    claims = claims_for(q, record, score_run(q, record))
    assert [claim.kind for claim in claims] == ["support"]
    assert "[f #c0]" in claims[0].prompt


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
    gateway = QueueGateway([yes(), yes(), NO, yes(), "garbage", yes(), yes()])
    out = tmp_path / "judged.jsonl"
    judged = judge_runs(gold, records, ClaimJudge(gateway, "j", max_attempts=1), out_path=out)
    by_id = {j.question_id: j for j in judged}
    assert by_id["p"].verdict is JudgeVerdict.PASS and by_id["p"].source == "judge"
    assert by_id["f"].verdict is JudgeVerdict.FAIL
    assert by_id["d"].verdict is JudgeVerdict.DOUBT
    assert by_id["det"].verdict is JudgeVerdict.PASS and by_id["det"].source == "judge"
    assert [j.model_dump() for j in load_judged(out)] == [j.model_dump() for j in judged]
    assert len(gateway.calls) == 7  # three missing facts and four support checks


def test_cross_check_records_disagreement(make_record):
    gold = GoldSet(questions=[_q("a"), _q("b")])
    records = [_paraphrase(make_record, "a"), _paraphrase(make_record, "b")]
    primary = ClaimJudge(QueueGateway([yes()]), "big")
    cross = ClaimJudge(QueueGateway([yes(), yes(), NO, yes()]), "small")  # agrees on a, disagrees on b
    judged = {j.question_id: j for j in judge_runs(gold, records, primary, cross)}
    assert not judged["a"].disagreement and judged["b"].disagreement
    # A disagreement no longer lets the primary's call settle it alone: an
    # unresolved (one True, one False) claim is doubt, not a free pass.
    assert judged["b"].verdict is JudgeVerdict.DOUBT
    assert judged["b"].judge_models == ["big", "small"]
    assert set(judged["b"].claims[0].verdicts) == {"big", "small"}


def test_no_cross_check_primary_alone_still_decides(make_record):
    """Regression: with a single judge, a primary False still fails the run."""
    gold = GoldSet(questions=[_q("f")])
    records = [_paraphrase(make_record, "f")]
    judged = judge_runs(gold, records, ClaimJudge(QueueGateway([NO]), "j"))
    assert judged[0].verdict is JudgeVerdict.FAIL
    assert not judged[0].disagreement


def test_cross_check_agree_false_still_fails(make_record):
    """Both models confirming a claim is wrong must still fail the run."""
    gold = GoldSet(questions=[_q("a")])
    records = [_paraphrase(make_record, "a")]
    primary = ClaimJudge(QueueGateway([NO]), "big")
    cross = ClaimJudge(QueueGateway([NO]), "small")
    judged = judge_runs(gold, records, primary, cross)[0]
    assert judged.verdict is JudgeVerdict.FAIL
    assert not judged.disagreement


def test_cross_check_agree_true_still_passes(make_record):
    gold = GoldSet(questions=[_q("a")])
    records = [_paraphrase(make_record, "a")]
    primary = ClaimJudge(QueueGateway([yes()]), "big")
    cross = ClaimJudge(QueueGateway([yes()]), "small")
    judged = judge_runs(gold, records, primary, cross)[0]
    assert judged.verdict is JudgeVerdict.PASS
    assert not judged.disagreement


def test_cross_check_disagreement_primary_false_is_doubt_not_fail(make_record):
    """The real bug pattern: primary says fail, cross-check says pass.

    The claim must not auto-fail the run just because the primary happened
    to call it that way -- it should read as doubt until the two agree.
    """
    gold = GoldSet(questions=[_q("a")])
    records = [_paraphrase(make_record, "a")]
    primary = ClaimJudge(QueueGateway([NO]), "big")
    cross = ClaimJudge(QueueGateway([yes()]), "small")
    judged = judge_runs(gold, records, primary, cross)[0]
    assert judged.verdict is JudgeVerdict.DOUBT
    assert judged.disagreement


def test_cross_check_disagreement_primary_true_is_doubt_not_pass(make_record):
    """Mirror case: primary says pass, cross-check says fail -- still doubt,
    not an automatic pass just because the primary happened to like it."""
    gold = GoldSet(questions=[_q("a")])
    records = [_paraphrase(make_record, "a")]
    primary = ClaimJudge(QueueGateway([yes()]), "big")
    cross = ClaimJudge(QueueGateway([NO]), "small")
    judged = judge_runs(gold, records, primary, cross)[0]
    assert judged.verdict is JudgeVerdict.DOUBT
    assert judged.disagreement


def test_disagreement_on_one_claim_does_not_mask_a_clear_failure_on_another(make_record):
    """One claim in doubt from disagreement must not hide a run failure that
    both judges agree on for a different claim -- False still outweighs None."""
    # Both required facts are word-only (no digits), so a miss is
    # paraphrase-able and goes to the judge rather than failing outright.
    q = _q("m", must_contain=["vacation", "insurance"])
    record = make_record(question_id="m", repeat=0, answer="Staff get time off. [f #c0]",
                         chunks=[CHUNK], cited=[0])
    gold = GoldSet(questions=[q])
    # Two missing facts ("vacation", "insurance") plus one support claim = 3 claims.
    # Primary: disagree(pass), agree-false, agree-false.
    primary = ClaimJudge(QueueGateway([yes(), NO, NO]), "big")
    cross = ClaimJudge(QueueGateway([NO, NO, NO]), "small")
    judged = judge_runs(gold, [record], primary, cross)[0]
    assert judged.disagreement
    assert judged.verdict is JudgeVerdict.FAIL


def test_judged_results_replace_bracket_in_report(make_record):
    gold = GoldSet(questions=[_q("p"), _q("f")])
    records = [_paraphrase(make_record, "p"), _paraphrase(make_record, "f")]
    plain = build_report(gold, records)
    assert plain.overall.strict.passed == 0 and plain.overall.optimistic.passed == 2

    judged = judge_runs(gold, records, ClaimJudge(QueueGateway([yes(), yes(), NO, yes()]), "j"))
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


def test_support_failure_overrides_regex_pass(make_record):
    q = _q(must_contain=["25"])
    record = make_record(question_id=q.id, answer="25 days. Also free cars. [f #c0]",
                         chunks=[CHUNK], cited=[0])
    gold = GoldSet(questions=[q])
    assert score_run(q, record).verdict is Verdict.PASS
    judged = judge_runs(gold, [record], ClaimJudge(QueueGateway([NO]), "j"))
    assert judged[0].support_checked
    assert build_report(gold, [record], judged).overall.strict.passed == 0


def test_stale_judgment_cannot_score_a_different_answer(make_record):
    import pytest
    from docket.eval.schema import GoldSetError
    q = _q(must_contain=["25"])
    record = make_record(question_id=q.id, answer="25 days [f #c0]", chunks=[CHUNK], cited=[0])
    gold = GoldSet(questions=[q])
    judged = judge_runs(gold, [record], ClaimJudge(QueueGateway([yes()]), "j"))
    changed = record.model_copy(update={"answer": "25 days and free cars [f #c0]"})
    with pytest.raises(GoldSetError, match="does not match"):
        build_report(gold, [changed], judged)


def test_oversized_support_input_is_doubt_without_clipping():
    from docket.eval.judge import Claim, MAX_JUDGE_PROMPT_CHARS
    gateway = QueueGateway([yes()])
    verdict = ClaimJudge(gateway, "j").judge(Claim("support", "all evidence", "x" * (MAX_JUDGE_PROMPT_CHARS + 1)))
    assert verdict.supported is None and verdict.attempts == 0
    assert not gateway.calls
