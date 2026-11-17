"""Bootstrap intervals, paired comparison and the difference classifier."""

from __future__ import annotations

import random

import pytest

from docket.eval.compare import Comparison, compare_outcomes, format_comparison
from docket.eval.stats import (
    INCONCLUSIVE,
    MEASURED_IMPROVEMENT,
    MEASURED_REGRESSION,
    bootstrap_ci,
    classify_difference,
    cluster_bootstrap_ci,
    improvement_supported,
    kappa_ci,
    paired_bootstrap_diff,
)


def test_bootstrap_constant_data_is_degenerate():
    assert bootstrap_ci([1.0] * 20, n_boot=200) == (1.0, 1.0)
    assert cluster_bootstrap_ci({f"q{i}": [True, True, True] for i in range(10)}, n_boot=200) == (1.0, 1.0)


def test_bootstrap_is_seed_deterministic_and_seed_sensitive():
    data = [i / 7.0 for i in range(23)]
    a = bootstrap_ci(data, n_boot=500, seed=3)
    assert a == bootstrap_ci(data, n_boot=500, seed=3)
    assert a != bootstrap_ci(data, n_boot=500, seed=4)
    lo, hi = a
    assert lo < sum(data) / len(data) < hi


def test_bootstrap_empty_is_uninformative():
    assert bootstrap_ci([]) == (0.0, 1.0)
    assert cluster_bootstrap_ci({}) == (0.0, 1.0)


def test_cluster_bootstrap_resamples_questions_not_repeats():
    # 4 questions pass in all 5 repeats, 4 fail in all 5: resampling questions gives a wide interval
    outcomes = {f"p{i}": [True] * 5 for i in range(4)} | {f"f{i}": [False] * 5 for i in range(4)}
    lo, hi = cluster_bootstrap_ci(outcomes, n_boot=2000, seed=1)
    assert lo <= 0.25 and hi >= 0.75  # treating 40 repeats as independent would give about 0.34-0.66


def test_paired_diff_identical_runs_is_zero_and_contains_zero():
    runs = {f"q{i}": [i % 2 == 0] * 3 for i in range(10)}
    diff, (lo, hi) = paired_bootstrap_diff(runs, runs, n_boot=500)
    assert diff == 0.0 and lo <= 0.0 <= hi


def test_paired_diff_clear_gain():
    a = {f"q{i}": [False] * 3 for i in range(20)}
    b = {f"q{i}": [True] * 3 for i in range(20)}
    diff, (lo, hi) = paired_bootstrap_diff(a, b, n_boot=500)
    assert diff == 1.0 and lo == 1.0 == hi


def test_paired_diff_requires_same_ids():
    with pytest.raises(ValueError):
        paired_bootstrap_diff({"a": [True]}, {"b": [True]})


def test_kappa_ci_perfect_and_bounds():
    labels = [True, False] * 15
    k, (lo, hi) = kappa_ci(labels, labels, n_boot=300)
    assert k == 1.0 and hi == 1.0
    noisy = labels[:]
    for i in range(0, 30, 5):
        noisy[i] = not noisy[i]
    k2, (lo2, hi2) = kappa_ci(labels, noisy, n_boot=300)
    assert lo2 <= k2 <= hi2 and k2 < 1.0


def test_coverage_sanity_on_simulated_binomial():
    rng = random.Random(7)
    covered = 0
    trials = 60
    for t in range(trials):
        sample = {f"q{i}": [rng.random() < 0.7] for i in range(40)}
        lo, hi = cluster_bootstrap_ci(sample, n_boot=300, seed=t)
        covered += lo <= 0.7 <= hi
    assert covered / trials >= 0.85  # nominal 95%; loose bound for the small simulation


def test_classifier_thresholds():
    ci_pos, ci_neg, ci_zero = (0.05, 0.4), (-0.4, -0.05), (-0.1, 0.3)
    assert classify_difference(0.2, ci_pos, 0, 12) == MEASURED_IMPROVEMENT
    assert classify_difference(-0.2, ci_neg, 12, 0) == MEASURED_REGRESSION
    assert classify_difference(0.2, ci_zero, 0, 12) == INCONCLUSIVE  # CI crosses 0
    assert classify_difference(0.2, ci_pos, 3, 4) == INCONCLUSIVE  # too few flips
    assert classify_difference(0.2, ci_pos, 5, 9) == INCONCLUSIVE  # McNemar p >= 0.05
    assert classify_difference(0.2, ci_pos, 12, 0) == INCONCLUSIVE  # direction disagrees with flips
    assert improvement_supported(0.2, ci_pos, 0, 12) and not improvement_supported(-0.2, ci_neg, 12, 0)


def _runs(passes: list[int], reps=3):
    return {f"q{i}": [bool(p)] * reps for i, p in enumerate(passes)}


def test_compare_supported_improvement_prints_measured_improvement():
    a, b = _runs([0] * 14 + [1] * 10), _runs([1] * 24)
    c = compare_outcomes(a, b, n_boot=500)
    assert c.label == MEASURED_IMPROVEMENT and c.runs_a == 72
    text = format_comparison(c, "off", "on")
    assert "measured improvement" in text and "n = 24 questions" in text and "CI method" in text


def test_compare_small_gain_refuses_improvement_wording():
    a, b = _runs([0, 0, 1, 1] * 6), _runs([1, 0, 1, 1] + [0, 0, 1, 1] * 5)  # one question flips
    c = compare_outcomes(a, b, n_boot=500)
    assert c.label == INCONCLUSIVE
    text = format_comparison(c)
    assert "measured improvement" not in text and "statistically inconclusive" in text
    assert "does not support calling it an improvement" in text


def test_compare_accepts_plain_bools_for_old_callers():
    c = compare_outcomes({"a": True, "b": False}, {"a": True, "b": True}, n_boot=50)
    assert isinstance(c, Comparison) and c.n == 2 and c.label == INCONCLUSIVE


def test_compare_prints_self_judged_warning():
    c = compare_outcomes(_runs([1, 0]), _runs([1, 1]), n_boot=50)
    c.judging_a = {"judged": True, "judges": ["qwen3:14b"], "self_judged": True}
    c.judging_b = {"judged": False, "judges": [], "self_judged": False}
    text = format_comparison(c, "A", "B")
    assert "self-judged" in text and "qwen3:14b" in text and "deterministic scoring only" in text
