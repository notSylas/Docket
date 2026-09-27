"""Judge paraphrased facts and citation support using local models.

Deterministic failures remain failures. Every other answerable run receives a
semantic support check, even when its gold facts match literally: matching a
quote in one cited chunk does not establish support for all claims. Verdicts
are bound to the exact gold and run records. Human calibration is separate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from docket.eval.schema import GoldSetError, GoldSet, Question, RunRecord, fingerprint, validate_run_keys
from docket.eval.scoring import (
    RunScore,
    Verdict,
    score_run,
    strip_citations,
)
from docket.infra.inference.gateway import InferenceGateway
from docket.prompts.judge import JUDGE_SYSTEM, fact_prompt, support_prompt

DEFAULT_JUDGE_MODEL = "qwen3:30b"
DEFAULT_CROSS_CHECK_MODEL = "gemma3:12b"
MAX_ATTEMPTS = 3
MAX_JUDGE_PROMPT_CHARS = 24000

# Passed straight through the gateway to `ollama.generate`.
JUDGE_OPTS: dict = {
    "think": False, "format": "json",
    "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 1024},
}

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class JudgeVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    DOUBT = "judge_doubt"


# -- claims ------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    kind: str  # "fact" | "support"
    statement: str  # the required fact, or a label for the support check
    prompt: str


def claims_for(question: Question, record: RunRecord, score: RunScore) -> list[Claim]:
    """Check missing facts and semantic attribution, including regex passes."""
    if score.verdict is Verdict.FAIL or not question.answerable:
        return []
    answer = strip_citations(record.answer).strip()
    claims = [
        Claim("fact", fact, fact_prompt(question, fact, answer)) for fact in score.missing_facts
    ]
    cited = {c.chunk_id for c in record.citations}
    cited_texts = [f"{c.citation_label}\n{c.text}" for c in record.retrieved if c.chunk_id in cited]
    claims.append(
        Claim("support", "every factual claim is supported by its own citations",
              support_prompt(question, record.answer, cited_texts))
    )
    return claims


# -- judging one claim -------------------------------------------------------


class ClaimVerdict(BaseModel):
    supported: bool | None  # None = the judge never produced parseable output
    reason: str = ""
    attempts: int = 1


def parse_judge_output(text: str) -> tuple[bool, str] | None:
    """Extract `(supported, reason)` from a model reply, or None if unusable.

    Tolerates <think> blocks, code fences, prose around the object, and
    "true"/"false" strings. Never raises.
    """
    text = _THINK_RE.sub("", text or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if not isinstance(obj, dict) or "supported" not in obj:
            continue
        value = obj["supported"]
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        if isinstance(value, bool):
            return value, str(obj.get("reason", "")).strip()
    return None


class ClaimJudge:
    """One judge model behind an injected gateway."""

    def __init__(self, gateway: InferenceGateway, model: str, *, max_attempts: int = MAX_ATTEMPTS):
        self.gateway = gateway
        self.model = model
        self.max_attempts = max_attempts

    def judge(self, claim: Claim) -> ClaimVerdict:
        prompt = claim.prompt
        if len(prompt) > MAX_JUDGE_PROMPT_CHARS:
            return ClaimVerdict(supported=None, reason="judge input exceeds the supported prompt budget", attempts=0)
        for attempt in range(1, self.max_attempts + 1):
            raw = self.gateway.generate(system=JUDGE_SYSTEM, prompt=prompt, **JUDGE_OPTS)
            parsed = parse_judge_output(raw)
            if parsed is not None:
                return ClaimVerdict(supported=parsed[0], reason=parsed[1], attempts=attempt)
            prompt = (
                claim.prompt
                + '\n\nYour previous reply was not valid JSON. Reply with ONLY {"supported": true or false, "reason": "..."}.'
            )
        return ClaimVerdict(supported=None, reason="unparseable judge output", attempts=self.max_attempts)


# -- judged records ----------------------------------------------------------


class ClaimResult(BaseModel):
    kind: str
    statement: str
    verdicts: dict[str, ClaimVerdict] = Field(default_factory=dict)  # by judge model


class JudgedRun(BaseModel):
    question_id: str
    repeat: int
    verdict: JudgeVerdict
    source: str  # "deterministic" | "judge"
    claims: list[ClaimResult] = Field(default_factory=list)
    judge_models: list[str] = Field(default_factory=list)
    disagreement: bool = False  # primary and cross-check judges differ on some claim
    support_checked: bool = False
    record_fingerprint: str | None = None
    gold_fingerprint: str | None = None


def _combine(supported: list[bool | None]) -> JudgeVerdict:
    if any(s is False for s in supported):
        return JudgeVerdict.FAIL  # a proven-wrong claim outweighs an unanswerable one
    if any(s is None for s in supported):
        return JudgeVerdict.DOUBT
    return JudgeVerdict.PASS


def _effective_supported(
    claim: ClaimResult, primary_model: str, cross_check_model: str | None
) -> bool | None:
    """The value a claim contributes to the run verdict.

    A claim the primary alone judged decides the run exactly as it always
    has. A claim a cross-check model also judged only fails the run when
    both models agree it fails; when the two disagree (one True, one False),
    the claim is unresolved rather than settled by whichever way the
    primary happened to call it, so it contributes doubt instead.
    """
    primary_supported = claim.verdicts[primary_model].supported
    if cross_check_model is not None and cross_check_model in claim.verdicts:
        cross_supported = claim.verdicts[cross_check_model].supported
        if (
            primary_supported is not None
            and cross_supported is not None
            and primary_supported != cross_supported
        ):
            return None
    return primary_supported


def run_verdict(
    claims: list[ClaimResult], primary_model: str, cross_check_model: str | None = None
) -> JudgeVerdict:
    return _combine(
        [_effective_supported(c, primary_model, cross_check_model) for c in claims]
    )


def _has_disagreement(claims: list[ClaimResult], models: list[str]) -> bool:
    if len(models) < 2:
        return False
    for claim in claims:
        seen = {claim.verdicts[m].supported for m in models if m in claim.verdicts}
        if len(seen) > 1:
            return True
    return False


ProgressFn = Callable[[str, int, int], None]  # (model, done, total)


def judge_runs(
    gold: GoldSet,
    records: list[RunRecord],
    primary: ClaimJudge,
    cross_check: ClaimJudge | None = None,
    *,
    out_path: Path | None = None,
    progress: ProgressFn | None = None,
) -> list[JudgedRun]:
    """Check facts and attribution for all candidate passes, then cross-check.

    Deterministic failures do not consume judge calls. Return one verdict per
    run, retaining artifact fingerprints even for deterministic outcomes.
    """
    validate_run_keys(records)
    gold_digest = fingerprint(gold)
    questions = gold.by_id()
    entries: list[tuple[RunRecord, RunScore, list[Claim]]] = []
    for record in records:
        question = questions.get(record.question_id)
        if question is None:
            continue
        score = score_run(question, record)
        entries.append((record, score, claims_for(question, record, score)))

    results: dict[tuple[str, int], list[ClaimResult]] = {
        (r.question_id, r.repeat): [ClaimResult(kind=c.kind, statement=c.statement) for c in claims]
        for r, _, claims in entries
    }
    judges = [primary] + ([cross_check] if cross_check else [])
    total = sum(len(claims) for _, _, claims in entries)
    for judge in judges:
        done = 0
        for record, _, claims in entries:
            for claim, result in zip(claims, results[(record.question_id, record.repeat)], strict=True):
                result.verdicts[judge.model] = judge.judge(claim)
                done += 1
                if progress:
                    progress(judge.model, done, total)

    models = [j.model for j in judges]
    judged: list[JudgedRun] = []
    for record, score, claims in entries:
        claim_results = results[(record.question_id, record.repeat)]
        if claims:
            judged.append(
                JudgedRun(
                    question_id=record.question_id,
                    repeat=record.repeat,
                    verdict=run_verdict(
                        claim_results, primary.model, cross_check.model if cross_check else None
                    ),
                    source="judge",
                    claims=claim_results,
                    judge_models=models,
                    disagreement=_has_disagreement(claim_results, models),
                    support_checked=any(c.kind == "support" for c in claims),
                    record_fingerprint=fingerprint(record),
                    gold_fingerprint=gold_digest,
                )
            )
        else:
            judged.append(
                JudgedRun(
                    question_id=record.question_id,
                    repeat=record.repeat,
                    verdict=JudgeVerdict.PASS if score.verdict is Verdict.PASS else JudgeVerdict.FAIL,
                    source="deterministic",
                    record_fingerprint=fingerprint(record),
                    gold_fingerprint=gold_digest,
                )
            )
    if out_path is not None:
        write_judged(out_path, judged)
    return judged


def write_judged(path: Path, judged: list[JudgedRun]) -> None:
    with Path(path).open("w", encoding="utf-8") as fh:
        for run in judged:
            fh.write(run.model_dump_json() + "\n")


def load_judged(path: str | Path) -> list[JudgedRun]:
    runs: list[JudgedRun] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                runs.append(JudgedRun.model_validate_json(line))
            except ValidationError as exc:
                raise GoldSetError(f"{path}:{line_no}: bad judged record: {exc}") from exc
    return runs


def judged_index(judged: list[JudgedRun]) -> dict[tuple[str, int], JudgedRun]:
    index: dict[tuple[str, int], JudgedRun] = {}
    for run in judged:
        key = (run.question_id, run.repeat)
        if key in index:
            raise GoldSetError(f"duplicate judged run {run.question_id}#{run.repeat}")
        index[key] = run
    return index


def verdict_of(judged: JudgedRun) -> Verdict:
    return {
        JudgeVerdict.PASS: Verdict.PASS,
        JudgeVerdict.FAIL: Verdict.FAIL,
        JudgeVerdict.DOUBT: Verdict.NEEDS_JUDGE,
    }[judged.verdict]
