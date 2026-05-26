"""
Tests for variance inflation (under-dispersion correction) in forecast.py.

Pins: never narrows, widens under-dispersed ensembles to the floor, preserves the
mean, and actually deflates single-degree bucket probabilities.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from src.forecast import EnsembleForecast, _fit_kde, _inflate_dispersion


def test_widens_underdispersed_to_floor() -> None:
    members = [70.0, 70.5, 71.0, 70.2, 70.8]  # std ~0.36°F, very tight
    out = _inflate_dispersion(members, target_std=2.0)
    assert abs(np.std(out) - 2.0) < 1e-6          # spread lifted to the floor
    assert abs(np.mean(out) - np.mean(members)) < 1e-6  # mean preserved


def test_never_narrows_already_dispersed() -> None:
    members = [60.0, 65.0, 70.0, 75.0, 80.0]  # std ~7°F, already wide
    out = _inflate_dispersion(members, target_std=2.0)
    assert out == members  # untouched


def test_degenerate_inputs() -> None:
    assert _inflate_dispersion([70.0], 2.0) == [70.0]      # too few members
    assert _inflate_dispersion([70.0, 70.0], 2.0) == [70.0, 70.0]  # zero spread
    assert _inflate_dispersion([1.0, 2.0, 3.0], 0.0) == [1.0, 2.0, 3.0]  # no floor


def test_inflation_deflates_single_bucket_probability() -> None:
    # A tight ensemble around 71°F assigns most mass to the 70-71 bucket.
    rng = np.random.default_rng(0)
    tight = (71.0 + rng.normal(0, 0.4, 200)).tolist()

    def _prob(members, lo, hi):
        f = EnsembleForecast(city="X", target_date=date.today())
        f.combined_members_f = members
        f.combined_kde = _fit_kde(members)
        return f.bucket_probability(lo, hi)

    p_tight = _prob(tight, 70.0, 71.0)
    p_wide = _prob(_inflate_dispersion(tight, 2.34), 70.0, 71.0)
    assert p_wide < p_tight  # honest spread → less mass on the single degree
