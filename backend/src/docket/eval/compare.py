"""Paired comparison of two scored/judged runs of the same gold questions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from docket.eval.judge import JudgedRun, JudgeVerdict, judged_index, load_judged
from docket.eval.report import resolve_scores
from docket.eval.schema import GoldSet, GoldSetError, load_records
from docket.eval.scoring import Verdict
from docket.eval.stats import (
    CI_METHOD,
    INCONCLUSIVE,
    MEASURED_IMPROVEMENT,
    classify_difference,
    mcnemar_exact,
    paired_bootstrap_diff,
    paired_flips,
    pass_majority,
)


def outcomes_from_judged(judged: list[JudgedRun]) -> dict[str, bool]:
    """Per-question pass (majority of repeats); a judge doubt is not a pass."""
    runs: dict[str, list[bool]] = {}
    for run in judged:
        runs.setdefault(run.question_id, []).append(run.verdict is JudgeVerdict.PASS)
    return {qid: pass_majority(v) for qid, v in runs.items()}


def outcomes_from_records(
    gold: GoldSet, records, judged: list[JudgedRun] | None = None
) -> dict[str, bool]:
    runs: dict[str, list[bool]] = {}
    for question, _, score in resolve_scores(gold, records, judged):
        runs.setdefault(question.id, []).append(score.verdict is Verdict.PASS)
    return {qid: pass_majority(v) for qid, v in runs.items()}


def repeat_outcomes_from_judged(judged: list[JudgedRun]) -> dict[str, list[bool]]:
    """Per-question list of repeat passes (for cluster bootstrap)."""
    runs: dict[str, list[bool]] = {}
    for run in judged:
        runs.setdefault(run.question_id, []).append(run.verdict is JudgeVerdict.PASS)
    return runs


def is_judged_file(path: Path) -> bool:
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    return "verdict" in json.loads(line)
                except ValueError as exc:
                    raise GoldSetError(f"{path}: not a JSONL file: {exc}") from exc
    raise GoldSetError(f"{path} is empty")


def load_outcomes(path: Path, gold: GoldSet | None) -> dict[str, bool]:
    """Outcomes from a judged JSONL (self-contained) or a raw run JSONL
    (needs `gold`; needs-judge runs count as not passing)."""
    if is_judged_file(path):
        return outcomes_from_judged(load_judged(path))
    if gold is None:
        raise GoldSetError(f"{path} is a raw run file; pass --gold so it can be scored")
    return outcomes_from_records(gold, load_records(path))


def load_repeat_outcomes(path: Path, gold: GoldSet | None) -> dict[str, list[bool]]:
    """Like `load_outcomes` but keeps every repeat of every question."""
    if is_judged_file(path):
        return repeat_outcomes_from_judged(load_judged(path))
    if gold is None:
        raise GoldSetError(f"{path} is a raw run file; pass --gold so it can be scored")
    runs: dict[str, list[bool]] = {}
    for question, _, score in resolve_scores(gold, load_records(path)):
        runs.setdefault(question.id, []).append(score.verdict is Verdict.PASS)
    return runs


def describe_judging(path: Path) -> dict:
    """Judge identity of one run file: judge models and whether the answering
    model judged itself. Raw (unjudged) files have no judge."""
    if not is_judged_file(path):
        return {"judges": [], "self_judged": False, "judged": False}
    judged = load_judged(path)
    judges = sorted({m for r in judged for m in r.judge_models})
    return {
        "judges": judges,
        "self_judged": any(r.self_judged for r in judged),
        "judged": True,
    }


@dataclass
class Comparison:
    n: int
    both: int
    a_only: int
    b_only: int
    neither: int
    p_value: float
    a_rate: float
    b_rate: float
    a_only_ids: list[str] = field(default_factory=list)  # regressions (B lost these)
    b_only_ids: list[str] = field(default_factory=list)  # improvements
    unpaired: int = 0
    # Paired bootstrap of (B - A) question-level accuracy; None when computed
    # from plain per-question bools by an older caller.
    diff: float = 0.0
    diff_ci: tuple[float, float] = (-1.0, 1.0)
    label: str = INCONCLUSIVE
    runs_a: int = 0
    runs_b: int = 0
    ci_method: str = CI_METHOD
    judging_a: dict | None = None
    judging_b: dict | None = None

    def regressed(self, alpha: float = 0.05, fail_on: str = "significant") -> bool:
        if self.a_only <= self.b_only:
            return False
        return True if fail_on == "any" else self.p_value < alpha


def compare_outcomes(
    a: dict[str, bool | list[bool]], b: dict[str, bool | list[bool]],
    *, n_boot: int = 10000, seed: int = 0,
) -> Comparison:
    """Values are a question's majority pass (bool) or the list of its repeat
    passes (preferred: the bootstrap then resamples questions with repeats)."""
    shared = sorted(set(a) & set(b))
    if not shared:
        raise GoldSetError("the two runs share no question ids")
    ra = {q: list(a[q]) if isinstance(a[q], (list, tuple)) else [bool(a[q])] for q in shared}
    rb = {q: list(b[q]) if isinstance(b[q], (list, tuple)) else [bool(b[q])] for q in shared}
    pa = {q: pass_majority(ra[q]) for q in shared}
    pb = {q: pass_majority(rb[q]) for q in shared}
    flips = paired_flips(pa, pb)
    diff, diff_ci = paired_bootstrap_diff(ra, rb, n_boot=n_boot, seed=seed)
    return Comparison(
        diff=diff, diff_ci=diff_ci,
        label=classify_difference(diff, diff_ci, flips["a_only"], flips["b_only"]),
        runs_a=sum(len(v) for v in ra.values()), runs_b=sum(len(v) for v in rb.values()),
        n=len(shared),
        both=flips["both"],
        a_only=flips["a_only"],
        b_only=flips["b_only"],
        neither=flips["neither"],
        p_value=mcnemar_exact(flips["a_only"], flips["b_only"]),
        a_rate=sum(pa.values()) / len(shared),
        b_rate=sum(pb.values()) / len(shared),
        a_only_ids=[q for q in shared if pa[q] and not pb[q]],
        b_only_ids=[q for q in shared if pb[q] and not pa[q]],
        unpaired=len(set(a) ^ set(b)),
    )


def format_comparison(c: Comparison, a_name: str = "A", b_name: str = "B") -> str:
    lines = [
        f"Paired over {c.n} questions" + (f" ({c.unpaired} not in both, ignored)" if c.unpaired else ""),
        f"  {a_name}: {c.a_rate:.1%}   {b_name}: {c.b_rate:.1%}",
        f"  both pass {c.both}, both fail {c.neither}, only {a_name} {c.a_only}, only {b_name} {c.b_only}",
        f"  McNemar exact p = {c.p_value:.4f}",
        f"  Paired difference ({b_name} - {a_name}) = {c.diff:+.1%}  95% CI [{c.diff_ci[0]:+.1%}, {c.diff_ci[1]:+.1%}]",
        f"  n = {c.n} questions; runs: {a_name} {c.runs_a}, {b_name} {c.runs_b}",
        f"  CI method: {c.ci_method}",
        f"  Judge {a_name}: {_judge_text(c.judging_a)}",
        f"  Judge {b_name}: {_judge_text(c.judging_b)}",
        f"  Verdict: {c.label}",
    ]
    if c.label == INCONCLUSIVE and c.b_rate > c.a_rate:
        lines.append("  (B scores higher but the paired result does not support calling it an improvement)")
    for who, info in ((a_name, c.judging_a), (b_name, c.judging_b)):
        if info and info.get("self_judged"):
            lines.append(f"  WARNING: {who} is self-judged (answering model also judged it)")
    if c.a_only_ids:
        lines.append(f"  lost in {b_name}: " + ", ".join(c.a_only_ids))
    if c.b_only_ids:
        lines.append(f"  gained in {b_name}: " + ", ".join(c.b_only_ids))
    return "\n".join(lines)


def _judge_text(info: dict | None) -> str:
    if info is None:
        return "not recorded"
    if not info.get("judged"):
        return "none (deterministic scoring only)"
    return ", ".join(info["judges"]) or "unrecorded in file"
