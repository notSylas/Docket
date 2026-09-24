"""Paired comparison of two scored/judged runs of the same gold questions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from docket.eval.judge import JudgedRun, JudgeVerdict, judged_index, load_judged
from docket.eval.report import resolve_scores
from docket.eval.schema import GoldSet, GoldSetError, load_records
from docket.eval.scoring import Verdict
from docket.eval.stats import mcnemar_exact, paired_flips, pass_majority


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

    def regressed(self, alpha: float = 0.05, fail_on: str = "significant") -> bool:
        if self.a_only <= self.b_only:
            return False
        return True if fail_on == "any" else self.p_value < alpha


def compare_outcomes(a: dict[str, bool], b: dict[str, bool]) -> Comparison:
    shared = sorted(set(a) & set(b))
    if not shared:
        raise GoldSetError("the two runs share no question ids")
    pa = {q: a[q] for q in shared}
    pb = {q: b[q] for q in shared}
    flips = paired_flips(pa, pb)
    return Comparison(
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
    ]
    if c.a_only_ids:
        lines.append(f"  lost in {b_name}: " + ", ".join(c.a_only_ids))
    if c.b_only_ids:
        lines.append(f"  gained in {b_name}: " + ", ".join(c.b_only_ids))
    return "\n".join(lines)
