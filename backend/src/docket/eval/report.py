"""Aggregate scored runs into the accuracy report.

Headline unit is the *question*: it passes if a majority of its repeats pass
(>=2 of 3). Runs the deterministic checks could not decide (`needs_judge`) are
counted two ways -- `strict` (not a pass) and `optimistic` (a pass) -- so the
number is bracketed until the LLM judge has run; when judged runs are supplied
they replace the bracket (only judge doubts keep it open).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, TYPE_CHECKING

from docket.eval.judge import JudgedRun, judged_index, verdict_of
from docket.eval.schema import GoldSet, GoldSetError, Question, RunRecord, fingerprint, validate_run_keys
from docket.eval.scoring import RunScore, Verdict, score_run
from docket.eval.stats import pass_all, pass_majority, wilson_interval

if TYPE_CHECKING:
    from docket.eval.benchmark import FrozenBenchmark
    from docket.eval.calibration import CalibrationResult


FAILURE_CATEGORIES = (
    "parse",
    "retrieval_miss",
    "context_truncation",
    "generation_error",
    "citation_error",
    "judge_doubt",
    "run_error",
)


@dataclass
class Rate:
    passed: int
    total: int
    lo: float
    hi: float

    @classmethod
    def of(cls, passed: int, total: int) -> Rate:
        lo, hi = wilson_interval(passed, total)
        return cls(passed, total, lo, hi)

    @property
    def value(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def fmt(self) -> str:
        if not self.total:
            return "n/a"
        return f"{self.value:6.1%} ({self.passed}/{self.total}) [{self.lo:.1%}-{self.hi:.1%}]"


@dataclass
class Slice:
    """Question-level rates for one group of questions."""

    questions: int
    strict: Rate  # majority of repeats PASS; needs_judge counts as not passing
    optimistic: Rate  # needs_judge counts as passing
    pass_all: Rate
    needs_judge_runs: int


@dataclass
class Report:
    repeats: int
    overall: Slice
    by_type: dict[str, Slice]
    by_split: dict[str, Slice]
    by_document: dict[str, Slice]
    by_formula: dict[str, Slice]
    milestone: dict[str, Any]
    recall_k: int
    recall_any: Rate  # runs where >=1 gold span was retrieved
    recall_all: Rate  # runs where every gold span was retrieved
    fact_in_context: Rate  # runs where every gold span reached the prompt
    abstention_precision: Rate
    abstention_recall: Rate
    wrongful_abstention: Rate  # abstained on answerable questions
    revoked_retrieval_leaks: int
    revoked_answer_leaks: int
    failures: dict[str, int] = field(default_factory=dict)
    failures_by_type: dict[str, dict[str, int]] = field(default_factory=dict)
    judged_runs: int = 0  # semantic fact/support checks applied
    judge_disagreements: int = 0  # ...where the cross-check judge disagreed


def classify_failure(question: Question, record: RunRecord, score: RunScore) -> str | None:
    """Root-cause bucket for a run that did not cleanly pass, or None for a pass.

    Ladder (first match wins): a run error; judge doubt; the gold quote is not
    even in the indexed text (parse/chunking); gold quote not retrieved;
    retrieved but absent from the prompt or the prompt looks truncated; facts
    were in context but the answer is wrong/missing them (generation); facts
    right but the citations are bad (citation).
    """
    if score.verdict is Verdict.PASS:
        return None
    if record.error:
        return "run_error"
    if score.verdict is Verdict.NEEDS_JUDGE:
        return "judge_doubt"
    if not question.answerable:
        return "generation_error"  # answered instead of abstaining

    if record.spans_indexed and not all(record.spans_indexed):
        return "parse"
    if question.gold_spans and not all(score.span_retrieved):
        return "retrieval_miss"
    if question.gold_spans and (not all(score.span_in_context) or score.truncated):
        return "context_truncation"
    if score.missing_facts or score.forbidden_found or score.abstained:
        return "generation_error"
    if not score.citation_ok:
        return "citation_error"
    return "generation_error"


def _slice(
    questions: list[Question],
    scores: dict[str, list[RunScore]],
) -> Slice:
    strict_pass = optimistic_pass = all_pass = judge_runs = n = 0
    for question in questions:
        runs = scores.get(question.id)
        if not runs:
            continue
        n += 1
        strict = [s.verdict is Verdict.PASS for s in runs]
        loose = [s.verdict is not Verdict.FAIL for s in runs]
        strict_pass += pass_majority(strict)
        optimistic_pass += pass_majority(loose)
        all_pass += pass_all(strict)
        judge_runs += sum(s.verdict is Verdict.NEEDS_JUDGE for s in runs)
    return Slice(n, Rate.of(strict_pass, n), Rate.of(optimistic_pass, n), Rate.of(all_pass, n), judge_runs)


def resolve_scores(
    gold: GoldSet, records: list[RunRecord], judged: list[JudgedRun] | None = None
) -> list[tuple[Question, RunRecord, RunScore]]:
    """Apply matching judgments to candidate passes; deterministic failures stand."""
    validate_run_keys(records)
    questions = gold.by_id()
    index = judged_index(judged) if judged else {}
    gold_digest = fingerprint(gold)
    resolved: list[tuple[Question, RunRecord, RunScore]] = []
    for record in records:
        question = questions.get(record.question_id)
        if question is None:
            continue  # record for a question no longer in the gold set
        score = score_run(question, record)
        run = index.get((record.question_id, record.repeat))
        if run is not None:
            if run.record_fingerprint is not None and run.record_fingerprint != fingerprint(record):
                raise GoldSetError(f"judged result does not match run {record.question_id}#{record.repeat}")
            if run.gold_fingerprint is not None and run.gold_fingerprint != gold_digest:
                raise GoldSetError("judged results were produced against a different gold set")
        if run is not None and run.source == "judge" and score.verdict is not Verdict.FAIL:
            score.verdict = verdict_of(run)
            score.reasons.append(f"judge verdict: {run.verdict.value}")
            score.judged = True
            score.judge_disagreement = run.disagreement
            if any(c.kind == "support" and any(v.supported is False for v in c.verdicts.values())
                   for c in run.claims) and score.verdict is Verdict.FAIL:
                score.citation_ok = False
        resolved.append((question, record, score))
    return resolved


def build_report(
    gold: GoldSet, records: list[RunRecord], judged: list[JudgedRun] | None = None,
    *, benchmark: FrozenBenchmark | None = None, calibration: CalibrationResult | None = None,
) -> Report:
    questions = gold.by_id()
    scores: dict[str, list[RunScore]] = defaultdict(list)
    pairs: list[tuple[Question, RunRecord, RunScore]] = []
    for question, record, score in resolve_scores(gold, records, judged):
        scores[question.id].append(score)
        pairs.append((question, record, score))

    scored_questions = [q for q in gold.questions if q.id in scores]

    def group(key: Any) -> dict[str, list[Question]]:
        grouped: dict[str, list[Question]] = defaultdict(list)
        for q in scored_questions:
            grouped[key(q)].append(q)
        return dict(sorted(grouped.items()))

    by_type = {k: _slice(v, scores) for k, v in group(lambda q: q.type.value).items()}
    by_split = {k: _slice(v, scores) for k, v in group(lambda q: q.split.value).items()}
    document_groups: dict[str, list[Question]] = defaultdict(list)
    for q in scored_questions:
        for document in q.source_documents or ["unattributed"]:
            document_groups[document].append(q)
    by_document = {k: _slice(v, scores) for k, v in sorted(document_groups.items())}
    by_formula = {
        k: _slice(v, scores)
        for k, v in group(lambda q: "unlabeled" if q.formula_dependent is None else "formula_dependent" if q.formula_dependent else "not_formula_dependent").items()
    }

    test_questions = [q for q in gold.questions if q.split.value == "test"]
    test_rate = by_split.get("test", _slice([], scores)).strict
    reasons: list[str] = []
    if gold.population != "real_user_documents":
        reasons.append("population is not real_user_documents")
    test_ids = {q.id for q in test_questions}
    test_records = [r for r in records if r.question_id in test_ids]
    if not test_questions or any(
        {r.repeat for r in test_records if r.question_id == q.id} != {0, 1, 2}
        for q in test_questions
    ):
        reasons.append("test questions need exactly three distinct repeats (0, 1, 2)")
    if any(q.formula_dependent is None for q in test_questions):
        reasons.append("test questions need reviewed formula-dependence labels")
    if any(not q.reviewed for q in test_questions):
        reasons.append("all test questions must be reviewed")
    if any(q.answerable and not q.source_documents for q in test_questions):
        reasons.append("answerable test questions need source_documents")
    index = judged_index(judged or [])
    if any((r.question_id, r.repeat) not in index for r in test_records) or judged is None:
        reasons.append("every test run needs a matching judged result")
    gold_digest = fingerprint(gold)
    if any(r.gold_fingerprint != gold_digest for r in test_records):
        reasons.append("test runs are not bound to this gold set")
    if any(
        (j := index.get((r.question_id, r.repeat))) is not None
        and (j.record_fingerprint != fingerprint(r) or j.gold_fingerprint != gold_digest)
        for r in test_records
    ):
        reasons.append("test judgments need matching run and gold fingerprints")
    if any(
        q.id in test_ids and q.answerable and s.verdict is Verdict.PASS
        and not (index.get((r.question_id, r.repeat)) and index[(r.question_id, r.repeat)].support_checked)
        for q, r, s in pairs
    ):
        reasons.append("passing test answers need semantic citation support checks")
    if benchmark is None:
        reasons.append("a frozen benchmark manifest is required")
    elif benchmark.gold_fingerprint != gold_digest or any(
        r.benchmark_fingerprint != fingerprint(benchmark) for r in test_records
    ):
        reasons.append("test runs do not match the frozen benchmark")
    if (calibration is None or calibration.distinct_judge_questions < 30
            or calibration.verified_judge_items != calibration.judge_items or not calibration.meets_target):
        reasons.append("judge calibration needs 30 distinct, bound judge items, pass/fail labels, and kappa >= 0.7")
    used_judges = {name for q, r, _ in pairs if q.id in test_ids
                   for j in [index.get((r.question_id, r.repeat))] if j is not None
                   for name in j.judge_models}
    if calibration is not None and (not used_judges or used_judges != set(calibration.judge_models)):
        reasons.append("calibration and test judgments must use the same judge models")
    if not any(q.type.value == "revoked" for q in test_questions):
        reasons.append("the test split needs revoked-source cases")
    if not any(q.type.value == "out_of_corpus" for q in test_questions):
        reasons.append("the test split needs out-of-corpus cases")
    configs = {fingerprint(r.configuration) for r in test_records}
    if len(configs) > 1 or any(not r.configuration for r in test_records):
        reasons.append("test runs need one recorded runtime configuration")
    if test_rate.lo < 0.90:
        reasons.append("test accuracy 95% lower bound is below 90%")

    # Retrieval / context metrics: answerable runs (revoked and out-of-corpus
    # questions have no evidence to retrieve), only where gold spans exist.
    span_runs = [(q, r, s) for q, r, s in pairs if q.answerable and q.gold_spans and not r.error]
    recall_any = sum(any(s.span_retrieved) for _, _, s in span_runs)
    recall_all = sum(all(s.span_retrieved) for _, _, s in span_runs)
    in_context = sum(all(s.span_in_context) for _, _, s in span_runs)

    unanswerable = [(q, r, s) for q, r, s in pairs if not q.answerable and not r.error]
    answerable = [(q, r, s) for q, r, s in pairs if q.answerable and not r.error]
    abstained_all = [(q, r, s) for q, r, s in pairs if s.abstained and not r.error]
    correct_abstain = sum(s.abstained for _, _, s in unanswerable)

    failures: dict[str, int] = dict.fromkeys(FAILURE_CATEGORIES, 0)
    failures_by_type: dict[str, dict[str, int]] = {}
    for question, record, score in pairs:
        category = classify_failure(question, record, score)
        if category is None:
            continue
        failures[category] += 1
        row = failures_by_type.setdefault(question.type.value, dict.fromkeys(FAILURE_CATEGORIES, 0))
        row[category] += 1

    revoked = [(q, r, s) for q, r, s in pairs if q.type.value == "revoked" and not r.error]
    if any(s.leaked or not s.abstained for _, _, s in revoked):
        reasons.append("revoked-source evidence or answer leak")

    return Report(
        repeats=max((len(v) for v in scores.values()), default=0),
        overall=_slice(scored_questions, scores),
        by_type=by_type,
        by_split=by_split,
        by_document=by_document,
        by_formula=by_formula,
        milestone={"passed": not reasons, "test_rate": asdict(test_rate), "reasons": reasons},
        recall_k=max((len(r.retrieved) for r in records), default=0),
        recall_any=Rate.of(recall_any, len(span_runs)),
        recall_all=Rate.of(recall_all, len(span_runs)),
        fact_in_context=Rate.of(in_context, len(span_runs)),
        abstention_precision=Rate.of(correct_abstain, len(abstained_all)),
        abstention_recall=Rate.of(correct_abstain, len(unanswerable)),
        wrongful_abstention=Rate.of(sum(s.abstained for _, _, s in answerable), len(answerable)),
        revoked_retrieval_leaks=sum(s.leaked for _, _, s in revoked),
        revoked_answer_leaks=sum(not s.abstained for _, _, s in revoked),
        failures=failures,
        failures_by_type=dict(sorted(failures_by_type.items())),
        judged_runs=sum(s.judged for _, _, s in pairs),
        judge_disagreements=sum(s.judge_disagreement for _, _, s in pairs),
    )


def report_to_dict(report: Report) -> dict[str, Any]:
    return asdict(report)


def _slice_lines(title: str, slices: dict[str, Slice]) -> list[str]:
    lines = [title]
    for name, sl in slices.items():
        lines.append(f"  {name:<14} n={sl.questions:<4} strict {sl.strict.fmt()}  optimistic {sl.optimistic.value:.1%}")
    return lines


def _accuracy_lines(report: Report) -> list[str]:
    o = report.overall
    if report.judged_runs == 0:
        return [
            f"Accuracy strict      {o.strict.fmt()}",
            f"Accuracy optimistic  {o.optimistic.fmt()}   ({o.needs_judge_runs} runs need the judge)",
        ]
    lines = [
        f"Accuracy (judged)    {o.strict.fmt()}",
        f"Judge: {report.judged_runs} runs resolved, {report.judge_disagreements} cross-check disagreements",
    ]
    if o.needs_judge_runs:
        lines.append(
            f"  {o.needs_judge_runs} runs are judge doubt: counted as fail above, "
            f"as pass in the optimistic bound {o.optimistic.value:.1%}"
        )
    return lines


def format_report(report: Report) -> str:
    o = report.overall
    lines = [
        f"Questions: {o.questions}   repeats: {report.repeats}   (pass = majority of repeats)",
        *_accuracy_lines(report),
        f"Pass-all repeats     {o.pass_all.fmt()}",
        "",
        *_slice_lines("By type:", report.by_type),
        "",
        *_slice_lines("By split:", report.by_split),
        "",
        *_slice_lines("By document:", report.by_document),
        "",
        *_slice_lines("Formula dependence:", report.by_formula),
        "",
        "Milestone: " + ("PASS" if report.milestone["passed"] else "NOT MET"),
        *(f"  {reason}" for reason in report.milestone["reasons"]),
        "",
        f"Retrieval recall@{report.recall_k} (any span)  {report.recall_any.fmt()}",
        f"Retrieval recall@{report.recall_k} (all spans) {report.recall_all.fmt()}",
        f"Fact-in-context                  {report.fact_in_context.fmt()}",
        f"Abstention precision             {report.abstention_precision.fmt()}",
        f"Abstention recall                {report.abstention_recall.fmt()}",
        f"Wrongful abstention (answerable) {report.wrongful_abstention.fmt()}",
        f"Revoked leaks: retrieved {report.revoked_retrieval_leaks}, answered {report.revoked_answer_leaks}",
        "",
        "Failure classification (runs):",
    ]
    total = sum(report.failures.values())
    for category in FAILURE_CATEGORIES:
        count = report.failures.get(category, 0)
        share = f"{count / total:5.1%}" if total else "  n/a"
        lines.append(f"  {category:<20} {count:>4}  {share}")
    return "\n".join(lines)
