"""Shared human-labeling review harness: export -> label -> load -> score.

`docket.eval.calibration` (compares a human's label against an automatic
judge verdict, via Cohen's kappa) and `docket.eval.formula_review` (pure
human aggregation, no automatic verdict to compare against) independently
implement the same export/label/load/score shape. This module holds exactly
the parts that are genuinely identical between them -- a stratified,
round-robin sampling helper and a labels-YAML loader/validator -- so each
caller can be a thin wrapper around shared logic instead of a from-scratch
reimplementation. What's genuinely different (kappa vs plain aggregation,
rejoining a separate judged file vs a self-contained per-item fingerprint)
stays in each caller.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import yaml

T = TypeVar("T")

# Shared across both callers: ~30 items is enough for a human to hand-check
# in one sitting while still catching a systemic failure mode, without
# either caller inventing its own default for what is structurally the same
# kind of "export a sample for blind labeling" task.
DEFAULT_SAMPLE_SIZE = 30


def bucket_sample(
    items: Sequence[T],
    n: int,
    *,
    bucket_key: Callable[[T], Hashable],
    sort_key: Callable[[T], object],
    order_key: Callable[[Hashable], object] = lambda key: key,
    seed: int = 0,
    distinct_key: Callable[[T], Hashable] | None = None,
) -> list[T]:
    """Stratified round-robin sample of up to `n` items.

    Groups `items` into buckets by `bucket_key` (after sorting them by
    `sort_key`, so bucket order is deterministic), shuffles each bucket with
    a seeded RNG, then round-robins across buckets -- ordered by
    `order_key(bucket_key_value)` -- taking one item per bucket per lap
    until `n` items are chosen or every bucket is exhausted. This spreads
    the sample across every kind of bucket instead of letting one dominate.

    When `distinct_key` is given, a first pass only takes items whose
    `distinct_key` value hasn't been used yet (e.g. "prefer a different
    question each time"); a second pass then allows repeats if `n` still
    isn't met. Without `distinct_key`, there is only one pass.
    """
    rng = random.Random(seed)
    buckets: dict[Hashable, list[T]] = {}
    for item in sorted(items, key=sort_key):
        buckets.setdefault(bucket_key(item), []).append(item)
    order = sorted(buckets, key=order_key)
    for key in order:
        rng.shuffle(buckets[key])

    chosen: list[T] = []
    used: set[Hashable] = set()
    passes = (False, True) if distinct_key is not None else (True,)
    for allow_repeats in passes:
        progress = True
        while len(chosen) < n and progress:
            progress = False
            for key in order:
                if len(chosen) >= n:
                    break
                bucket = buckets[key]
                for i, item in enumerate(bucket):
                    marker = distinct_key(item) if distinct_key else None
                    if allow_repeats or marker is None or marker not in used:
                        chosen.append(bucket.pop(i))
                        if marker is not None:
                            used.add(marker)
                        progress = True
                        break
    return chosen


def load_labels_yaml(
    path: Path,
    *,
    error_cls: type[Exception],
    duplicate_label: str = "label",
    validate_item: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Load and structurally validate a labels YAML file.

    Must decode to a dict with an 'items' list; every item needs a unique
    'id' and, if present, a boolean 'correct'. Raises `error_cls` (each
    caller's own exception type, so error semantics stay whatever that
    module already promises its callers) on any violation.

    `validate_item`, if given, runs on each item after the structural
    checks above pass, for caller-specific validation -- e.g. formula
    review's per-item tamper/fingerprint check, which calibration has no
    equivalent of (calibration instead rejoins against a separate judged
    file at score time; see `eval.calibration.score_labels`).
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise error_cls(f"cannot read labels file {path}: {exc}") from exc
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise error_cls(f"labels file {path} must have an 'items' list")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            raise error_cls(f"labels file {path}: every item needs an id")
        if item["id"] in seen:
            raise error_cls(f"duplicate {duplicate_label}: {item['id']}")
        seen.add(item["id"])
        if item.get("correct") is not None and not isinstance(item["correct"], bool):
            raise error_cls(f"item {item['id']}: 'correct' must be true or false, got {item['correct']!r}")
        if validate_item is not None:
            validate_item(item)
    return items


@dataclass
class ReviewResult:
    """Shared shape both `eval.calibration.CalibrationResult` and
    `eval.formula_review.FormulaReviewResult` build on: how many items
    got a human label, how many are still waiting, and what fraction the
    human marked correct. What "agreement" is measured against differs per
    caller (an automatic judge verdict for calibration; nothing, for pure
    aggregation, in formula review) -- see each caller's own result type
    for its extra fields."""

    labeled: int
    unlabeled: int
    agreement: float
