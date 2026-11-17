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


# -- bootstrap and paired comparison -----------------------------------------
#
# Percentile bootstrap with a private, seeded `random.Random` (deterministic,
# never touches global state). Questions are the resampling unit ("clusters");
# repeats of one question stay together, so repeats are never treated as
# independent evidence.

CI_METHOD = "percentile bootstrap, questions resampled as clusters (repeats nested), 95%"

# Explicit thresholds for `classify_difference`.
IMPROVEMENT_ALPHA = 0.05  # McNemar exact p must be below this
MIN_DISCORDANT = 5  # fewer discordant questions than this is never conclusive


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _percentile_interval(samples: list[float], alpha: float) -> tuple[float, float]:
    samples.sort()
    n = len(samples)
    lo = samples[min(n - 1, max(0, int((alpha / 2) * n)))]
    hi = samples[min(n - 1, max(0, int((1 - alpha / 2) * n) - 1))]
    return (lo, hi)


def bootstrap_ci(
    values: Sequence[float], *, stat=_mean, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """Percentile bootstrap interval of `stat` over i.i.d. `values`."""
    import random

    if not values:
        return (0.0, 1.0)
    rng = random.Random(seed)
    n = len(values)
    samples = [stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot)]
    return _percentile_interval(samples, alpha)


def cluster_bootstrap_ci(
    outcomes_by_question: Mapping[str, Sequence[bool]],
    *, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0,
) -> tuple[float, float]:
    """Interval for the question-level pass rate (majority of repeats passes),
    resampling whole questions with their repeats."""
    import random

    ids = sorted(q for q, runs in outcomes_by_question.items() if runs)
    if not ids:
        return (0.0, 1.0)
    passed = [1.0 if pass_majority(outcomes_by_question[q]) else 0.0 for q in ids]
    rng = random.Random(seed)
    n = len(ids)
    samples = [sum(passed[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot)]
    return _percentile_interval(samples, alpha)


def paired_bootstrap_diff(
    a: Mapping[str, Sequence[bool]], b: Mapping[str, Sequence[bool]],
    *, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0,
) -> tuple[float, tuple[float, float]]:
    """Accuracy(b) - accuracy(a) over the same questions (question-level
    majority pass), with a paired question-cluster bootstrap interval."""
    import random

    if set(a) != set(b):
        raise ValueError("both runs must cover the same question ids")
    ids = sorted(q for q in a if a[q] and b[q])
    if not ids:
        return (0.0, (-1.0, 1.0))
    delta = [float(pass_majority(b[q])) - float(pass_majority(a[q])) for q in ids]
    rng = random.Random(seed)
    n = len(ids)
    samples = [sum(delta[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot)]
    return (sum(delta) / n, _percentile_interval(samples, alpha))


def kappa_ci(
    a: Sequence[Hashable], b: Sequence[Hashable], *, n_boot: int = 5000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, tuple[float, float]]:
    """Cohen's kappa with a percentile bootstrap interval over items."""
    import random

    point = cohen_kappa(a, b)
    rng = random.Random(seed)
    n = len(a)
    samples = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        samples.append(cohen_kappa([a[i] for i in idx], [b[i] for i in idx]))
    return (point, _percentile_interval(samples, alpha))


MEASURED_IMPROVEMENT = "measured improvement"
MEASURED_REGRESSION = "measured regression"
INCONCLUSIVE = "statistically inconclusive"


def classify_difference(
    diff: float, diff_ci: tuple[float, float], a_only: int, b_only: int,
    *, alpha: float = IMPROVEMENT_ALPHA, min_discordant: int = MIN_DISCORDANT,
) -> str:
    """Label a paired difference (B minus A). A direction is only called when
    ALL hold: the bootstrap interval excludes 0 on that side, exact McNemar
    p < alpha, and at least `min_discordant` questions flipped."""
    p = mcnemar_exact(a_only, b_only)
    if a_only + b_only < min_discordant or p >= alpha:
        return INCONCLUSIVE
    lo, hi = diff_ci
    if diff > 0 and lo > 0 and b_only > a_only:
        return MEASURED_IMPROVEMENT
    if diff < 0 and hi < 0 and a_only > b_only:
        return MEASURED_REGRESSION
    return INCONCLUSIVE


def improvement_supported(
    diff: float, diff_ci: tuple[float, float], a_only: int, b_only: int, alpha: float = IMPROVEMENT_ALPHA
) -> bool:
    return classify_difference(diff, diff_ci, a_only, b_only, alpha=alpha) == MEASURED_IMPROVEMENT
