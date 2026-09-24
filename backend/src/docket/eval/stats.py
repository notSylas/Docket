"""Small statistics helpers for the eval report (stdlib only)."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Hashable, Mapping, Sequence


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (95% by default).
    With n == 0 there is no information, so the interval is (0, 1)."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float:
    """Cohen's kappa for two raters' labels over the same items."""
    if len(a) != len(b):
        raise ValueError("label sequences must have the same length")
    if not a:
        raise ValueError("cannot compute kappa on zero items")
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    count_a, count_b = Counter(a), Counter(b)
    expected = sum(count_a[label] * count_b[label] for label in count_a) / (n * n)
    if expected == 1.0:  # both raters used one identical label throughout
        return 1.0
    return (observed - expected) / (1 - expected)


def mcnemar_exact(a_only: int, b_only: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts: the number
    of items only A passed and only B passed. Binomial(a_only + b_only, 0.5)."""
    n = a_only + b_only
    if n == 0:
        return 1.0
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_flips(a: Mapping[str, bool], b: Mapping[str, bool]) -> dict[str, int]:
    """Cross-tab of per-question pass/fail for two runs over the same ids."""
    if set(a) != set(b):
        raise ValueError("both runs must cover the same question ids")
    flips = {"both": 0, "a_only": 0, "b_only": 0, "neither": 0}
    for qid, a_pass in a.items():
        b_pass = b[qid]
        key = "both" if a_pass and b_pass else "a_only" if a_pass else "b_only" if b_pass else "neither"
        flips[key] += 1
    return flips


def pass_majority(outcomes: Sequence[bool]) -> bool:
    """Passes if more than half the repeats pass (>=2 of 3)."""
    return bool(outcomes) and sum(outcomes) * 2 > len(outcomes)


def pass_all(outcomes: Sequence[bool]) -> bool:
    return bool(outcomes) and all(outcomes)
