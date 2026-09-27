"""Judge calibration: export answers for the user to label, then measure
Cohen's kappa between the automatic verdicts and the user's labels.

The labels file deliberately omits every automatic verdict so the user labels
blind; verdicts are re-joined from the judged JSONL when scoring.

    - id: vacation-days#1
      question: ...
      gold: {facts: [...], quotes: [...]}
      answer: ...
      correct: null        # fill in: true / false
      notes: ""
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from docket.eval.judge import JudgedRun, JudgeVerdict, judged_index
from docket.eval.review import DEFAULT_SAMPLE_SIZE, ReviewResult, bucket_sample, load_labels_yaml
from docket.eval.schema import GoldSet, GoldSetError, Question, RunRecord, fingerprint
from docket.eval.stats import cohen_kappa

KAPPA_TARGET = 0.7
_STRATA = ("judge_pass", "judge_fail", "judge_doubt", "det_pass", "det_fail")


def _stratum(run: JudgedRun) -> str:
    prefix = "judge" if run.source == "judge" else "det"
    if run.verdict is JudgeVerdict.PASS:
        return f"{prefix}_pass"
    if run.verdict is JudgeVerdict.FAIL:
        return f"{prefix}_fail"
    return "judge_doubt"


def sample_runs(judged: list[JudgedRun], n: int, seed: int = 0) -> list[JudgedRun]:
    """Stratified sample: round-robin over (judge|deterministic) x (pass|fail|doubt)
    so every kind of verdict is represented, preferring distinct questions."""
    return bucket_sample(
        judged,
        n,
        bucket_key=_stratum,
        sort_key=lambda r: (r.question_id, r.repeat),
        order_key=_STRATA.index,
        seed=seed,
        distinct_key=lambda r: r.question_id,
    )


def item_id(run: JudgedRun | RunRecord) -> str:
    return f"{run.question_id}#{run.repeat}"


def export_labels(
    gold: GoldSet,
    records: list[RunRecord],
    judged: list[JudgedRun],
    out_path: Path,
    *,
    n: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 0,
) -> int:
    """Write the labels YAML; returns the number of items exported."""
    questions = gold.by_id()
    by_key = {(r.question_id, r.repeat): r for r in records}
    candidates = [j for j in judged if (j.question_id, j.repeat) in by_key and j.question_id in questions]
    items = []
    for run in sample_runs(candidates, n, seed):
        question: Question = questions[run.question_id]
        record = by_key[(run.question_id, run.repeat)]
        cited = {c.chunk_id for c in record.citations}
        items.append(
            {
                "id": item_id(run),
                "type": question.type.value,
                "question": question.question,
                "answerable": question.answerable,
                "gold": {
                    "facts": question.must_contain + question.enumeration,
                    "quotes": question.gold_spans,
                },
                "answer": record.answer.strip(),
                "record_fingerprint": fingerprint(record),
                "cited_evidence": [f"{c.citation_label}\n{c.text}" for c in record.retrieved if c.chunk_id in cited],
                "correct": None,
                "notes": "",
            }
        )
    header = (
        "# Label each item: correct: true if the answer is right (states the gold facts, "
        "nothing contradicting them,\n# and is supported by its evidence; for an "
        "unanswerable question, correct means it declined), else false.\n"
    )
    Path(out_path).write_text(
        header + yaml.safe_dump({"version": 1, "items": items}, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return len(items)


@dataclass
class CalibrationResult(ReviewResult):
    kappa: float  # all labeled items (deterministic + judge verdicts vs the user)
    judge_items: int
    judge_agreement: float | None
    judge_kappa: float | None  # only runs the judge resolved
    disagreements: list[str] = field(default_factory=list)
    distinct_judge_questions: int = 0
    verified_judge_items: int = 0
    judge_label_classes: int = 0
    judge_models: list[str] = field(default_factory=list)

    @property
    def meets_target(self) -> bool:
        return self.judge_kappa is not None and self.judge_kappa >= KAPPA_TARGET and self.judge_label_classes >= 2


def _label(verdict: JudgeVerdict) -> str:
    return "pass" if verdict is JudgeVerdict.PASS else "fail" if verdict is JudgeVerdict.FAIL else "doubt"


def _kappa(a: list[str], b: list[str]) -> tuple[float, float]:
    agreement = sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
    return cohen_kappa(a, b), agreement


def load_labels(path: Path) -> list[dict]:
    return load_labels_yaml(path, error_cls=GoldSetError, duplicate_label="calibration label")


def score_labels(labels_path: Path, judged: list[JudgedRun]) -> CalibrationResult:
    """Cohen's kappa between the automatic verdicts and the user's labels."""
    index = {item_id(j): j for j in judged_index(judged).values()}
    all_judge: list[str] = []
    all_user: list[str] = []
    j_judge: list[str] = []
    j_user: list[str] = []
    disagreements: list[str] = []
    unlabeled = verified_judge_items = 0
    judge_questions: set[str] = set()
    judge_models: set[str] = set()
    for item in load_labels(labels_path):
        if item.get("correct") is None:
            unlabeled += 1
            continue
        run = index.get(item["id"])
        if run is None:
            raise GoldSetError(f"label {item['id']!r} has no matching run in the judged file")
        if run.record_fingerprint is not None and item.get("record_fingerprint") != run.record_fingerprint:
            raise GoldSetError(f"label {item['id']!r} belongs to a different or unbound run")
        user = "pass" if item["correct"] else "fail"
        auto = _label(run.verdict)
        all_judge.append(auto)
        all_user.append(user)
        if run.source == "judge":
            judge_questions.add(run.question_id)
            judge_models.update(run.judge_models)
            verified_judge_items += run.record_fingerprint is not None
            j_judge.append(auto)
            j_user.append(user)
        if auto != user:
            disagreements.append(f"{item['id']}: automatic={auto} user={user} ({run.source})")
    if not all_user:
        raise GoldSetError("no labeled items: fill in `correct: true/false` first")
    kappa, agreement = _kappa(all_judge, all_user)
    judge_kappa = judge_agreement = None
    if j_user:
        judge_kappa, judge_agreement = _kappa(j_judge, j_user)
    return CalibrationResult(
        labeled=len(all_user),
        unlabeled=unlabeled,
        agreement=agreement,
        kappa=kappa,
        judge_items=len(j_user),
        judge_agreement=judge_agreement,
        judge_kappa=judge_kappa,
        disagreements=disagreements,
        distinct_judge_questions=len(judge_questions),
        verified_judge_items=verified_judge_items,
        judge_label_classes=len(set(j_user)),
        judge_models=sorted(judge_models),
    )


def format_calibration(result: CalibrationResult) -> str:
    lines = [
        f"Labeled items: {result.labeled} ({result.unlabeled} left unlabeled)",
        f"All verdicts vs your labels: agreement {result.agreement:.1%}, kappa {result.kappa:.2f}",
    ]
    if result.judge_kappa is None:
        lines.append("Judge-resolved items: none labeled, judge kappa unavailable")
    else:
        verdict = "meets" if result.meets_target else "BELOW"
        lines.append(
            f"Judge-resolved items ({result.judge_items}): agreement {result.judge_agreement:.1%}, "
            f"kappa {result.judge_kappa:.2f} -- {verdict} the {KAPPA_TARGET} target"
        )
    if result.disagreements:
        lines.append("Disagreements:")
        lines.extend(f"  {d}" for d in result.disagreements)
    return "\n".join(lines)
