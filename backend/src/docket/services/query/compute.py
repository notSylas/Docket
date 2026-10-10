"""Deterministic compute stage for the fast path (Upgrade doc 05 section 8,
candidate move; doc 07 section 5.2 prerequisite for verified derived numbers).

Flow, owned by `QueryService._compute_stage`:

1. `looks_computational(question)` -- a small regex detector for arithmetic or
   aggregate questions (totals, averages, differences, percent change, ratios).
2. One short JSON-planning model call (see `docket.prompts.query`) that names
   an allow-listed operation and the CELLS to use. The model never supplies a
   number: `parse_plan` rejects any key but `op`, `refs` and `round_to`, any ref
   key but `chunk_id`/`cell`/`range`/`sheet`, and any chunk id that is not one of
   the spreadsheet chunks shown in the prompt.
3. `execute_plan` runs each step through the existing
   `docket.services.agent.calculator.calculate` (which reads the cells itself
   from stored workbook bytes and refuses blanks, text, mixed units and
   division by zero). No arithmetic is done here.
4. The derivation is turned into a pinned note for the answering call, built
   only from calculator output (never from model-written text).

Every failure is a `ComputeFallback` carrying a short reason; the caller then
runs the normal path unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from docket.infra.evidence.workbook_reader import WorkbookReader, WorkbookReadError
from docket.services.agent.calculator import (
    MAX_INPUTS,
    MAX_ROUND_TO,
    OPERATIONS,
    CalculationError,
    calculate,
)

DEFAULT_ROUND_TO = 2
_STEP_KEYS = {"op", "refs", "round_to"}
_REF_KEYS = {"chunk_id", "cell", "range", "sheet"}
_RAW_LIMIT = 500

_COMPUTATIONAL_RE = re.compile(
    "|".join([
        r"\btotal(?:s|ed|led|ling)?\b",
        r"\bsum\b",
        r"\badd(?:ed)? up\b",
        r"\bcombined\b",
        r"\baverage\b",
        r"\bmean\b",
        r"\bdifference\b",
        r"\bchange[ds]?\b",
        r"\bgrow(?:th|n|s)?\b|\bgrew\b",
        r"\bincrease[ds]?\b",
        r"\bdecrease[ds]?\b",
        r"\bdeclin(?:e|ed|es)\b",
        r"\bpercent(?:age)?\b|%",
        r"\bratio\b",
        r"\bhow much (?:more|less|higher|lower)\b",
        r"\bmore than\b|\bless than\b",
        r"\bminimum\b|\bmaximum\b",
    ]),
    re.IGNORECASE,
)


def looks_computational(*texts: str | None) -> bool:
    """True when any text states an arithmetic or aggregate request."""
    return any(bool(t) and _COMPUTATIONAL_RE.search(t) is not None for t in texts)


class ComputeFallback(Exception):
    """The compute stage cannot be used; `str(exc)` is the recorded reason."""


@dataclass
class PlanStep:
    op: str
    refs: list[dict[str, str]]
    round_to: int = DEFAULT_ROUND_TO


@dataclass
class ComputeOutcome:
    steps: list[dict[str, Any]] = field(default_factory=list)  # calculator output, per step
    citation_labels: list[str] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)


def _truncate(text: str) -> str:
    return text if len(text) <= _RAW_LIMIT else text[:_RAW_LIMIT] + "..."


def parse_plan(raw: str, allowed_chunk_ids: set[str], max_steps: int) -> list[PlanStep]:
    """Validate the model's JSON plan. Raises `ComputeFallback`. An empty plan
    means the model declined (also a fallback)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1]) if 0 <= start < end else None
        except ValueError:
            data = None
    if not isinstance(data, dict) or set(data) != {"steps"} or not isinstance(data["steps"], list):
        raise ComputeFallback("plan is not a JSON object of the form {\"steps\": [...]}")
    raw_steps = data["steps"]
    if not raw_steps:
        raise ComputeFallback("model declined: empty plan")
    if len(raw_steps) > max_steps:
        raise ComputeFallback(f"plan has {len(raw_steps)} steps; at most {max_steps} allowed")

    steps: list[PlanStep] = []
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise ComputeFallback(f"step {index} is not an object")
        extra = set(item) - _STEP_KEYS
        if extra:
            raise ComputeFallback(f"step {index} has unsupported keys: {sorted(extra)}")
        op = item.get("op")
        if not isinstance(op, str) or op.strip().lower() not in OPERATIONS:
            raise ComputeFallback(f"step {index}: unsupported operation {op!r}")
        round_to = item.get("round_to", DEFAULT_ROUND_TO)
        if isinstance(round_to, bool) or not isinstance(round_to, int) or not 0 <= round_to <= MAX_ROUND_TO:
            raise ComputeFallback(f"step {index}: invalid round_to")
        refs = item.get("refs")
        if not isinstance(refs, list) or not refs or len(refs) > MAX_INPUTS:
            raise ComputeFallback(f"step {index}: refs must be a non-empty list")
        clean_refs: list[dict[str, str]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ComputeFallback(f"step {index}: a ref is not an object")
            extra = set(ref) - _REF_KEYS
            if extra:
                raise ComputeFallback(f"step {index}: ref has unsupported keys: {sorted(extra)}")
            if not all(isinstance(v, str) and v.strip() for v in ref.values()):
                raise ComputeFallback(f"step {index}: ref values must be non-empty strings")
            if ref.get("chunk_id") not in allowed_chunk_ids:
                raise ComputeFallback(
                    f"step {index}: chunk_id {ref.get('chunk_id')!r} is not a spreadsheet chunk in the context"
                )
            if ("cell" in ref) == ("range" in ref):
                raise ComputeFallback(f"step {index}: a ref needs exactly one of cell or range")
            clean_refs.append({k: v.strip() for k, v in ref.items()})
        steps.append(PlanStep(op=op.strip().lower(), refs=clean_refs, round_to=round_to))
    return steps


def execute_plan(
    reader: WorkbookReader, steps: list[PlanStep], context_labels: set[str]
) -> ComputeOutcome:
    """Run every step through the existing `calculate`. All-or-nothing: any
    refusal (unresolved cell, mixed units, division by zero, ...) raises
    `ComputeFallback`. Each step's `citation_labels` is narrowed to labels that
    exist in the prompt context, since only those can be cited; a step with none
    is a fallback."""
    outcome = ComputeOutcome()
    for index, step in enumerate(steps, start=1):
        try:
            result = calculate(reader, step.op, step.refs, step.round_to)
        except (CalculationError, WorkbookReadError) as exc:
            raise ComputeFallback(f"step {index} refused: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 -- the stage must never fail the query
            raise ComputeFallback(f"step {index} failed: {type(exc).__name__}: {exc}") from exc
        labels = [l for l in result.get("citation_labels", []) if l in context_labels]
        if not labels:
            raise ComputeFallback(f"step {index}: none of its input chunks is in the answer context")
        result = {**result, "citation_labels": labels}
        outcome.steps.append(result)
        for label in labels:
            if label not in outcome.citation_labels:
                outcome.citation_labels.append(label)
        for chunk_id in result.get("chunk_ids", []):
            if chunk_id not in outcome.chunk_ids:
                outcome.chunk_ids.append(chunk_id)
    return outcome


def record_for(
    *, status: str, plan: Any = None, raw_plan: str | None = None,
    derivation: list[dict[str, Any]] | None = None, fallback_reason: str | None = None,
) -> dict[str, Any]:
    """The dict stored on `QueryResult.compute` / `RunRecord.compute`."""
    record: dict[str, Any] = {"status": status, "fallback_reason": fallback_reason}
    if plan is not None:
        record["plan"] = plan
    elif raw_plan is not None:
        record["plan_raw"] = _truncate(raw_plan)
    if derivation is not None:
        record["derivation"] = derivation
    return record


def plan_to_dicts(steps: list[PlanStep]) -> list[dict[str, Any]]:
    return [{"op": s.op, "refs": s.refs, "round_to": s.round_to} for s in steps]


def derivation_for_record(outcome: ComputeOutcome) -> list[dict[str, Any]]:
    return [
        {
            "operation": s["operation"],
            "result_text": s["result_text"],
            "units": s["units"],
            "expression": s["expression"],
            "inputs": s["inputs"],
            "citation_labels": s["citation_labels"],
            "chunk_ids": s["chunk_ids"],
            **({"result_percent_text": s["result_percent_text"]} if "result_percent_text" in s else {}),
        }
        for s in outcome.steps
    ]
