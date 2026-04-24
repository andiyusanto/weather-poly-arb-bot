"""
Unit tests for wind speed forecast logic.

Covers:
  - WindForecast.bucket_probability (range, open-ended, zero lower bound)
  - _wind_confidence (spread → confidence mapping)
  - _parse_wind_bucket_from_question (mph, km/h, open-ended phrases)
  - get_wind_forecast (mocked Open-Meteo response, unit conversion, bias)
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.forecast import (
    KPH_TO_MPH,
    WindForecast,
    _fit_kde,
    _wind_confidence,
    get_wind_forecast,
)
from src.polymarket_client import _parse_wind_bucket_from_question


# ── WindForecast.bucket_probability ──────────────────────────────────────────

def _make_wind_forecast(members_mph: list[float]) -> WindForecast:
    arr = members_mph
    fc = WindForecast(
        city="TestCity",
        target_date=date(2026, 5, 1),
        combined_members_mph=arr,
        mean_f=float(np.mean(arr)),
        std_f=float(np.std(arr)),
        combined_kde=_fit_kde(arr),
        confidence=0.8,
    )
    return fc


def test_bucket_probability_in_range():
    """Members tightly clustered in [10, 15) → prob for that bucket ≈ 1."""
    members = [12.0] * 50  # all members at 12 mph
    fc = _make_wind_forecast(members)
    prob = fc.bucket_probability(10.0, 15.0)
    assert prob > 0.90, f"Expected >0.90, got {prob:.3f}"


def test_bucket_probability_outside_range():
    """Members all at 12 mph → prob for [20, 30) bucket ≈ 0."""
    members = [12.0] * 50
    fc = _make_wind_forecast(members)
    prob = fc.bucket_probability(20.0, 30.0)
    assert prob < 0.05, f"Expected <0.05, got {prob:.3f}"


def test_bucket_probability_open_ended_upper():
    """Members at 25 mph → open-ended [20, ∞) bucket should have high prob."""
    members = [25.0] * 50
    fc = _make_wind_forecast(members)
    prob = fc.bucket_probability(20.0, 9999.0)
    assert prob > 0.80, f"Expected >0.80, got {prob:.3f}"


def test_bucket_probability_zero_lower():
    """Lower bound of 0 is valid (less-than bucket like [0, 10))."""
    members = [5.0] * 50
    fc = _make_wind_forecast(members)
    prob = fc.bucket_probability(0.0, 10.0)
    assert prob > 0.85, f"Expected >0.85, got {prob:.3f}"


def test_bucket_probability_no_kde_returns_zero():
    """WindForecast with no KDE (too few members) returns 0."""
    fc = WindForecast(city="X", target_date=date(2026, 5, 1))
    assert fc.bucket_probability(10.0, 20.0) == 0.0


def test_all_bucket_probabilities_sum_to_one():
    """Normalized probabilities across covering buckets should sum to ~1."""
    members = list(np.random.default_rng(42).normal(15, 3, 200))
    fc = _make_wind_forecast(members)
    buckets = [(0.0, 10.0), (10.0, 15.0), (15.0, 20.0), (20.0, 9999.0)]
    probs = fc.all_bucket_probabilities(buckets)
    total = sum(probs.values())
    assert abs(total - 1.0) < 0.01, f"Bucket probs sum={total:.4f}, expected ~1.0"


# ── _wind_confidence ──────────────────────────────────────────────────────────

def test_wind_confidence_low_spread():
    """Low spread (5 mph std) + multiple models → high confidence."""
    conf = _wind_confidence(std_mph=5.0, n_models=3)
    assert conf >= 0.85, f"Expected >=0.85, got {conf:.3f}"


def test_wind_confidence_high_spread():
    """High spread (25 mph std) → low confidence."""
    conf = _wind_confidence(std_mph=25.0, n_models=1)
    assert conf <= 0.25, f"Expected <=0.25, got {conf:.3f}"


def test_wind_confidence_decreases_with_spread():
    """Confidence is strictly decreasing as spread grows."""
    confs = [_wind_confidence(s, n_models=2) for s in [5, 10, 15, 20, 25]]
    assert confs == sorted(confs, reverse=True), f"Not monotone decreasing: {confs}"


def test_wind_confidence_clamped_to_one():
    """Very low spread + many models should not exceed 1.0."""
    conf = _wind_confidence(std_mph=0.1, n_models=10)
    assert conf <= 1.0


# ── _parse_wind_bucket_from_question ─────────────────────────────────────────

@pytest.mark.parametrize("bucket_str,expected", [
    ("10-15 mph",           (10.0, 15.0)),
    ("15-20 mph",           (15.0, 20.0)),
    ("less than 10 mph",    (0.0, 10.0)),
    ("more than 25 mph",    (25.0, 9999.0)),
    ("25 mph or higher",    (25.0, 9999.0)),
    ("below 5 mph",         (0.0, 5.0)),
    ("between 10 and 20 mph", (10.0, 20.0)),
])
def test_parse_wind_bucket_mph(bucket_str: str, expected: tuple[float, float]):
    result = _parse_wind_bucket_from_question(bucket_str)
    assert result is not None, f"Parser returned None for: {bucket_str!r}"
    lo, hi = result
    assert abs(lo - expected[0]) < 0.01, f"Lower: {lo} != {expected[0]}"
    assert abs(hi - expected[1]) < 0.01, f"Upper: {hi} != {expected[1]}"


def test_parse_wind_bucket_kmh_conversion():
    """km/h values should be converted to mph."""
    result = _parse_wind_bucket_from_question("30-50 km/h")
    assert result is not None
    lo, hi = result
    assert abs(lo - 30 * KPH_TO_MPH) < 0.01
    assert abs(hi - 50 * KPH_TO_MPH) < 0.01


def test_parse_wind_bucket_kph_label():
    """kph label also triggers km/h conversion."""
    result = _parse_wind_bucket_from_question("less than 40 kph")
    assert result is not None
    lo, hi = result
    assert lo == 0.0
    assert abs(hi - 40 * KPH_TO_MPH) < 0.01


# ── get_wind_forecast (mocked Open-Meteo) ────────────────────────────────────

def _mock_open_meteo_response(kph_values: list[float]) -> dict:
    """Build a fake Open-Meteo daily response for wind_speed_10m_max."""
    daily: dict = {"time": ["2026-05-01"]}
    for i, v in enumerate(kph_values):
        daily[f"wind_speed_10m_max_member{i:02d}"] = [v]
    return {"daily": daily}


@patch("src.forecast._fetch_ensemble_vars")
def test_get_wind_forecast_unit_conversion(mock_fetch):
    """Open-Meteo returns kph; get_wind_forecast must convert to mph."""
    # Mock returns raw kph values (as _fetch_ensemble_vars would return from API)
    kph_vals = [40.0] * 20
    mock_fetch.return_value = {"wind_speed_10m_max": kph_vals}

    fc = get_wind_forecast(
        city="TestCity", lat=35.0, lon=139.0,
        target_date=date(2026, 5, 1),
        models=["icon_seamless"],
        use_bias_correction=False,
    )

    assert fc is not None
    expected_mph = 40.0 * KPH_TO_MPH  # 24.85 mph
    assert abs(fc.mean_f - expected_mph) < 0.5, f"mean_f={fc.mean_f:.2f} expected≈{expected_mph:.2f}"


@patch("src.forecast._fetch_ensemble_vars")
def test_get_wind_forecast_returns_none_on_empty(mock_fetch):
    """Returns None if all models return no data."""
    mock_fetch.return_value = None

    fc = get_wind_forecast(
        city="NoDataCity", lat=0.0, lon=0.0,
        target_date=date(2026, 5, 1),
        models=["icon_seamless"],
        use_bias_correction=False,
    )
    assert fc is None


@patch("src.forecast._fetch_ensemble_vars")
def test_get_wind_forecast_confidence_populated(mock_fetch):
    """Returned WindForecast has a valid confidence score in [0, 1]."""
    mph_vals = [15.0 + i * 0.1 for i in range(50)]
    mock_fetch.return_value = {"wind_speed_10m_max": mph_vals}

    fc = get_wind_forecast(
        city="TestCity", lat=35.0, lon=139.0,
        target_date=date(2026, 5, 1),
        models=["icon_seamless"],
        use_bias_correction=False,
    )

    assert fc is not None
    assert 0.0 <= fc.confidence <= 1.0
    assert fc.combined_kde is not None
