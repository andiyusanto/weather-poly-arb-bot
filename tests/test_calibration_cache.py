"""
Tests for the calibration cache-coherence fix.

The long-running trader caches the curve in-memory; the resolve cron rebuilds it
in a separate process. Without reset_cache() the trader serves a stale curve and
bets on uncalibrated probabilities. These tests pin that behaviour.
"""

from __future__ import annotations

from unittest.mock import patch

import src.calibration as cal
from src.calibration import calibrate_probability, reset_cache

# A curve that maps everything >0.5 down to 0.25 (the overconfidence correction).
CURVE = ([0.5, 1.0], [0.25, 0.25])
IDENTITY: tuple = ()  # _load_curve returns falsy when no curve is fit


def setup_function() -> None:
    reset_cache()


def test_uses_curve_once_loaded() -> None:
    with patch("src.calibration._load_curve", return_value=CURVE):
        assert calibrate_probability(0.95, "temperature") == 0.25


def test_stale_cache_without_reset_keeps_old_curve() -> None:
    # First lookup caches the identity (warm-up) curve.
    with patch("src.calibration._load_curve", return_value=IDENTITY):
        assert calibrate_probability(0.95, "temperature") == 0.95  # passthrough

    # DB now has a real curve, but WITHOUT a reset the cached identity persists.
    with patch("src.calibration._load_curve", return_value=CURVE):
        assert calibrate_probability(0.95, "temperature") == 0.95  # still stale


def test_reset_cache_picks_up_new_curve() -> None:
    with patch("src.calibration._load_curve", return_value=IDENTITY):
        assert calibrate_probability(0.95, "temperature") == 0.95

    reset_cache()  # the per-scan reload

    with patch("src.calibration._load_curve", return_value=CURVE):
        assert calibrate_probability(0.95, "temperature") == 0.25  # fresh curve applied


def test_rebuild_calibration_clears_cache() -> None:
    # Prime the cache with a stale curve.
    with patch("src.calibration._load_curve", return_value=IDENTITY):
        calibrate_probability(0.95, "temperature")
    assert cal._curve_cache  # something is cached

    # rebuild_calibration must end by clearing the cache (no trades → no fit, but
    # the final reset_cache() still runs).
    with patch("src.calibration._load_resolved_trades", return_value=[]):
        cal.rebuild_calibration()
    assert not cal._curve_cache
