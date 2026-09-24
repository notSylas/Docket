"""LLM judge for runs the deterministic checks could not decide.

Two-phase design: `docket eval run` generates answers (JSONL of `RunRecord`);
`docket eval judge` then reads that JSONL, resolves every `NEEDS_JUDGE` run and
writes a *judged* JSONL (one `JudgedRun` per run, deterministic verdicts
included so the file stands alone for `compare`).

One claim per call: each required fact that is not verbatim in the answer is a
"fact" claim (does the answer state it, consistently with the gold passages?),
and a run whose cited chunks carry no gold quote adds a "support" claim (does
the cited evidence support the answer?). A run passes only if every claim is
supported; any unsupported claim fails it; any claim the judge could not answer
(unparseable output after retries) leaves the run as `judge_doubt`.

The judge is run at temperature 0 with thinking off and JSON output. With a
cross-check model, all claims are judged by the primary model first, then by
the second (so only one model is loaded at a time); disagreement is recorded
and counted, and the primary verdict is the one used.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from docket.eval.schema import GoldSetError, GoldSet, Question, RunRecord
from docket.eval.scoring import (
    RunScore,
    Verdict,
    retrieval_hit,
    score_run,
    strip_citations,
)
from docket.inference.gateway import InferenceGateway

DEFAULT_JUDGE_MODEL = "qwen3:30b"
DEFAULT_CROSS_CHECK_MODEL = "gemma3:12b"
MAX_ATTEMPTS = 3
MAX_EVIDENCE_CHARS = 6000

# Passed straight through the gateway to `ollama.generate`.
JUDGE_OPTS: dict = {"think": False, "format": "json", "options": {"temperature": 0}}

JUDGE_SYSTEM = (
    "You are a strict, literal grader for a question-answering evaluation. "
    "Judge only what you are asked. Reply with a single JSON object and nothing else."
)

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


def _fact_prompt(question: Question, fact: str, answer: str) -> str:
    refs = "\n".join(f'- "{s}"' for s in question.gold_spans) or "- (none given)"
    shown = fact[len("re:") :] if fact.startswith("re:") else fact
    return (
        f"Question: {question.question}\n\n"
        f"Reference passages from the source document (ground truth):\n{refs}\n\n"
        f"Required fact: {shown}\n\n"
        f'Answer under test:\n"""\n{answer}\n"""\n\n'
        "Does the answer under test state the required fact? A paraphrase, different "
        "wording, or an equivalent number format counts. It must be consistent with the "
        "reference passages and must not contradict the fact.\n"
        'Reply as JSON: {"supported": true or false, "reason": "<one short sentence>"}'
    )


def _support_prompt(question: Question, answer: str, evidence: list[str]) -> str:
    blocks: list[str] = []
    budget = MAX_EVIDENCE_CHARS
    for i, text in enumerate(evidence, start=1):
        clipped = text[: max(budget, 0)]
        budget -= len(clipped)
        blocks.append(f"[{i}] {clipped}")
    joined = "\n\n".join(blocks) or "(no cited evidence)"
    return (
        f"Question: {question.question}\n\n"
        f'Answer under test:\n"""\n{answer}\n"""\n\n'
        f"Evidence the answer cites:\n{joined}\n\n"
        "Is the factual content of the answer supported by the cited evidence, so that "
        "someone reading only that evidence would agree with it? Ignore citation tags.\n"
        'Reply as JSON: {"supported": true or false, "reason": "<one short sentence>"}'
    )


def claims_for(question: Question, record: RunRecord, score: RunScore) -> list[Claim]:
    """The claims a NEEDS_JUDGE run must satisfy (empty for any other run)."""
    if score.verdict is not Verdict.NEEDS_JUDGE:
        return []
    answer = strip_citations(record.answer).strip()
    claims = [
        Claim("fact", fact, _fact_prompt(question, fact, answer)) for fact in score.missing_facts
    ]
    cited = {c.chunk_id for c in record.citations}
    cited_texts = [c.text for c in record.retrieved if c.chunk_id in cited]
    if question.gold_spans and not retrieval_hit(question.gold_spans, cited_texts):
        claims.append(
            Claim("support", "cited evidence supports the answer", _support_prompt(question, answer, cited_texts))
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


def _combine(supported: list[bool | None]) -> JudgeVerdict:
    if any(s is False for s in supported):
        return JudgeVerdict.FAIL  # a proven-wrong claim outweighs an unanswerable one
    if any(s is None for s in supported):
        return JudgeVerdict.DOUBT
    return JudgeVerdict.PASS


def run_verdict(claims: list[ClaimResult], primary_model: str) -> JudgeVerdict:
    return _combine([c.verdicts[primary_model].supported for c in claims])


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
    """Judge every NEEDS_JUDGE run. All claims go through the primary model
    first, then (if given) the cross-check model, so only one is loaded at a
    time. Returns one `JudgedRun` per record whose question is in `gold`; also
    written to `out_path` (JSONL) when given."""
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
                    verdict=run_verdict(claim_results, primary.model),
                    source="judge",
                    claims=claim_results,
                    judge_models=models,
                    disagreement=_has_disagreement(claim_results, models),
                )
            )
        else:
            judged.append(
                JudgedRun(
                    question_id=record.question_id,
                    repeat=record.repeat,
                    verdict=JudgeVerdict.PASS if score.verdict is Verdict.PASS else JudgeVerdict.FAIL,
                    source="deterministic",
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
    return {(j.question_id, j.repeat): j for j in judged}


def verdict_of(judged: JudgedRun) -> Verdict:
    return {
        JudgeVerdict.PASS: Verdict.PASS,
        JudgeVerdict.FAIL: Verdict.FAIL,
        JudgeVerdict.DOUBT: Verdict.NEEDS_JUDGE,
    }[judged.verdict]
