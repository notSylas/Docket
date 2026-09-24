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

import random
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from docket.eval.judge import JudgedRun, JudgeVerdict, judged_index
from docket.eval.schema import GoldSet, GoldSetError, Question, RunRecord
from docket.eval.scoring import strip_citations
from docket.eval.stats import cohen_kappa

KAPPA_TARGET = 0.7
DEFAULT_SAMPLE_SIZE = 30
_STRATA = ("judge_pass", "judge_fail", "judge_doubt", "det_pass", "det_fail")
_MAX_CITED_CHARS = 600


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
    rng = random.Random(seed)
    buckets: dict[str, list[JudgedRun]] = {s: [] for s in _STRATA}
    for run in sorted(judged, key=lambda r: (r.question_id, r.repeat)):
        buckets[_stratum(run)].append(run)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    chosen: list[JudgedRun] = []
    used_questions: set[str] = set()
    for allow_repeat_question in (False, True):
        progress = True
        while len(chosen) < n and progress:
            progress = False
            for name in _STRATA:
                if len(chosen) >= n:
                    break
                bucket = buckets[name]
                for i, run in enumerate(bucket):
                    if allow_repeat_question or run.question_id not in used_questions:
                        chosen.append(bucket.pop(i))
                        used_questions.add(run.question_id)
                        progress = True
                        break
    return chosen


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
                "answer": strip_citations(record.answer).strip(),
                "cited_evidence": [c.text[:_MAX_CITED_CHARS] for c in record.retrieved if c.chunk_id in cited],
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
class CalibrationResult:
    labeled: int
    unlabeled: int
    agreement: float
    kappa: float  # all labeled items (deterministic + judge verdicts vs the user)
    judge_items: int
    judge_agreement: float | None
    judge_kappa: float | None  # only runs the judge resolved
    disagreements: list[str] = field(default_factory=list)

    @property
    def meets_target(self) -> bool:
        return self.judge_kappa is not None and self.judge_kappa >= KAPPA_TARGET


def _label(verdict: JudgeVerdict) -> str:
    return "pass" if verdict is JudgeVerdict.PASS else "fail" if verdict is JudgeVerdict.FAIL else "doubt"


def _kappa(a: list[str], b: list[str]) -> tuple[float, float]:
    agreement = sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
    return cohen_kappa(a, b), agreement


def load_labels(path: Path) -> list[dict]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GoldSetError(f"cannot read labels file {path}: {exc}") from exc
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise GoldSetError(f"labels file {path} must have an 'items' list")
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            raise GoldSetError(f"labels file {path}: every item needs an id")
        if item.get("correct") not in (True, False, None):
            raise GoldSetError(f"item {item['id']}: 'correct' must be true or false, got {item['correct']!r}")
    return items


def score_labels(labels_path: Path, judged: list[JudgedRun]) -> CalibrationResult:
    """Cohen's kappa between the automatic verdicts and the user's labels."""
    index = {item_id(j): j for j in judged}
    all_judge: list[str] = []
    all_user: list[str] = []
    j_judge: list[str] = []
    j_user: list[str] = []
    disagreements: list[str] = []
    unlabeled = 0
    for item in load_labels(labels_path):
        if item.get("correct") is None:
            unlabeled += 1
            continue
        run = index.get(item["id"])
        if run is None:
            raise GoldSetError(f"label {item['id']!r} has no matching run in the judged file")
        user = "pass" if item["correct"] else "fail"
        auto = _label(run.verdict)
        all_judge.append(auto)
        all_user.append(user)
        if run.source == "judge":
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
