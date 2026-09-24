"""Stats checked against hand-computed / textbook values."""

from __future__ import annotations

import pytest

from docket.eval.stats import (
    cohen_kappa,
    mcnemar_exact,
    paired_flips,
    pass_all,
    pass_majority,
    wilson_interval,
)


def test_wilson_9_of_10():
    lo, hi = wilson_interval(9, 10)
    assert lo == pytest.approx(0.5958, abs=1e-3)
    assert hi == pytest.approx(0.9821, abs=1e-3)


def test_wilson_92_of_100():
    lo, hi = wilson_interval(92, 100)
    assert lo == pytest.approx(0.8500, abs=1e-3)
    assert hi == pytest.approx(0.9589, abs=1e-3)


def test_wilson_edges():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = wilson_interval(10, 10)
    assert hi == pytest.approx(1.0)
    assert lo == pytest.approx(0.7225, abs=1e-3)
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert hi == pytest.approx(0.2775, abs=1e-3)


def test_kappa_hand_computed():
    # 2x2: both yes 20, A yes/B no 5, A no/B yes 10, both no 15 -> po=.7, pe=.5 -> .4
    a = [1] * 20 + [1] * 5 + [0] * 10 + [0] * 15
    b = [1] * 20 + [0] * 5 + [1] * 10 + [0] * 15
    assert cohen_kappa(a, b) == pytest.approx(0.4)


def test_kappa_perfect_chance_and_degenerate():
    assert cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == pytest.approx(1.0)
    assert cohen_kappa([1, 1, 0, 0], [1, 0, 1, 0]) == pytest.approx(0.0)
    assert cohen_kappa([1, 1, 1], [1, 1, 1]) == 1.0


def test_kappa_input_errors():
    with pytest.raises(ValueError):
        cohen_kappa([1], [1, 0])
    with pytest.raises(ValueError):
        cohen_kappa([], [])


def test_mcnemar_exact_values():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(1, 5) == pytest.approx(0.21875)  # 2 * (1+6)/64
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)  # 2 * 1/32
    assert mcnemar_exact(5, 5) == 1.0  # capped
    assert mcnemar_exact(2, 8) == mcnemar_exact(8, 2)  # 2*(1+10+45)/1024 = .109375
    assert mcnemar_exact(2, 8) == pytest.approx(0.109375)


def test_paired_flips():
    a = {"q1": True, "q2": True, "q3": False, "q4": False}
    b = {"q1": True, "q2": False, "q3": True, "q4": False}
    assert paired_flips(a, b) == {"both": 1, "a_only": 1, "b_only": 1, "neither": 1}
    with pytest.raises(ValueError):
        paired_flips(a, {"q1": True})


def test_pass_majority_and_all():
    assert pass_majority([True, True, False])
    assert not pass_majority([True, False, False])
    assert pass_majority([True])
    assert not pass_majority([True, False])
    assert not pass_majority([])
    assert pass_all([True, True, True])
    assert not pass_all([True, True, False])
    assert not pass_all([])
