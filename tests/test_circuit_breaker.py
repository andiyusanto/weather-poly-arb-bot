"""
Tests for the Open-Meteo rate-limit circuit breaker in forecast.py.

Verifies it trips after a threshold of consecutive rate-limit failures, then
short-circuits fetches (no network) until reset/cooldown, and that a success
closes it.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import src.forecast as fc
from src.forecast import _fetch_ensemble_vars, reset_circuit


def setup_function() -> None:
    reset_circuit()
    with fc._forecast_cache_lock:
        fc._forecast_cache.clear()


def _rate_limited_client():
    """A client whose every GET returns 429."""
    resp = MagicMock(status_code=429)
    client = MagicMock()
    client.get.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    return ctx, client


def test_breaker_trips_and_short_circuits() -> None:
    ctx, client = _rate_limited_client()
    with patch("src.forecast.httpx.Client", return_value=ctx), \
         patch("src.forecast.time.sleep"):  # skip the real backoff
        # Drive THRESHOLD consecutive failures (distinct cache keys so none cache).
        for i in range(fc._CB_FAIL_THRESHOLD):
            assert _fetch_ensemble_vars(float(i), 0.0, date.today(), "gfs_seamless", "temperature_2m_max") is None
        assert fc._circuit_is_open()

        calls_before = client.get.call_count
        # Further fetches must short-circuit — no new network calls.
        assert _fetch_ensemble_vars(999.0, 0.0, date.today(), "gfs_seamless", "temperature_2m_max") is None
        assert client.get.call_count == calls_before


def test_success_closes_breaker() -> None:
    # Below-threshold failures, then a success resets the counter.
    ok = MagicMock(status_code=200)
    ok.raise_for_status.return_value = None
    ok.json.return_value = {"daily": {"temperature_2m_max_member01": [20.0]}}
    client = MagicMock()
    client.get.return_value = ok
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    with patch("src.forecast.httpx.Client", return_value=ctx):
        assert _fetch_ensemble_vars(1.0, 0.0, date.today(), "gfs_seamless", "temperature_2m_max") is not None
    assert not fc._circuit_is_open()
    assert fc._cb_consecutive_fails == 0


def test_reset_circuit_clears_state() -> None:
    fc._cb_consecutive_fails = 99
    fc._cb_open_until = 1e18
    reset_circuit()
    assert not fc._circuit_is_open()
    assert fc._cb_consecutive_fails == 0
