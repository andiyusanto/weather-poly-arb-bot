"""
Unit tests for the Open-Meteo archive observation fetch in bias_recorder.

Focus: the per-process memoization that collapses duplicate (city+date)
observation requests during a backfill into a single HTTP call.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import src.bias_recorder as br
from src.bias_recorder import _fetch_observed


def _mock_client(status: int, value=12.3) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"daily": {"temperature_2m_max": [value]}}
    client = MagicMock()
    client.get.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    return ctx, client


def setup_function() -> None:
    br._observed_cache.clear()


def test_successful_fetch_is_cached() -> None:
    ctx, client = _mock_client(200, value=21.5)
    with patch("src.bias_recorder.httpx.Client", return_value=ctx):
        a = _fetch_observed(52.0, 13.0, date(2026, 5, 18), "temperature")
        b = _fetch_observed(52.0, 13.0, date(2026, 5, 18), "temperature")
    assert a == 21.5 and b == 21.5
    # Second identical call served from cache — network hit exactly once.
    assert client.get.call_count == 1


def test_failure_is_not_cached() -> None:
    ctx, client = _mock_client(404)
    with patch("src.bias_recorder.httpx.Client", return_value=ctx):
        assert _fetch_observed(52.0, 13.0, date(2026, 5, 18), "temperature") is None
        assert _fetch_observed(52.0, 13.0, date(2026, 5, 18), "temperature") is None
    # None is not cached, so it retries on the next call.
    assert client.get.call_count == 2


def test_429_backs_off_then_succeeds() -> None:
    resp429 = MagicMock(status_code=429)
    resp200 = MagicMock(status_code=200)
    resp200.json.return_value = {"daily": {"temperature_2m_max": [30.0]}}
    client = MagicMock()
    client.get.side_effect = [resp429, resp200]
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    with patch("src.bias_recorder.httpx.Client", return_value=ctx), \
         patch("src.bias_recorder.time.sleep"):  # don't actually sleep
        assert _fetch_observed(1.0, 1.0, date(2026, 5, 18), "temperature") == 30.0
    assert client.get.call_count == 2


def test_unknown_variable_returns_none() -> None:
    assert _fetch_observed(1.0, 1.0, date(2026, 5, 18), "humidity") is None
