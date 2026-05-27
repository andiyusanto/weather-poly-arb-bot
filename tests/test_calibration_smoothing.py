"""
Tests for Laplace smoothing in _fit_isotonic — no calibration bin may assert
0.0 or 1.0 off a tiny sample (which would make Kelly size to the cap).
"""

from __future__ import annotations

from src.calibration import _fit_isotonic


def test_no_hard_zero_or_one_small_sample() -> None:
    # 12 rows: bottom all-lose, top all-win. Raw proportions would give 0.0/1.0.
    rows = [(0.05, 0)] * 6 + [(0.95, 1)] * 6
    edges, rates = _fit_isotonic(rows, n_bins=2)
    assert all(0.0 < r < 1.0 for r in rates), rates
    assert rates[0] < 0.3 and rates[-1] > 0.7   # still well-separated


def test_smoothing_relaxes_with_more_data() -> None:
    # A fully-winning high bin: 3 samples -> 0.80, 30 samples -> ~0.97.
    _, small = _fit_isotonic([(0.9, 1)] * 3, n_bins=1)
    _, big = _fit_isotonic([(0.9, 1)] * 30, n_bins=1)
    assert abs(small[0] - 4 / 5) < 1e-9
    assert abs(big[0] - 31 / 32) < 1e-9
    assert big[0] > small[0]


def test_monotonicity_preserved() -> None:
    rows = [(0.1, 0)] * 4 + [(0.4, 0)] * 4 + [(0.7, 1)] * 4 + [(0.95, 1)] * 4
    _, rates = _fit_isotonic(rows, n_bins=4)
    assert rates == sorted(rates)
