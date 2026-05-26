"""
Tests for the TTL forecast cache in forecast._fetch_ensemble_vars.

Pins the behaviour that prevents Open-Meteo quota exhaustion: repeated fetches
of the same (lat, lon, date, model, variable) within the TTL hit the cache, not
the network; failures are not cached; expiry forces a refetch.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import src.forecast as fc
from src.forecast import _fetch_ensemble_vars


def _mock_httpx(member_value=20.0, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"daily": {"temperature_2m_max_member01": [member_value]}}
    client = MagicMock()
    client.get.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    return ctx, client


def setup_function() -> None:
    with fc._forecast_cache_lock:
        fc._forecast_cache.clear()


def test_second_call_hits_cache_not_network() -> None:
    ctx, client = _mock_httpx()
    with patch("src.forecast.httpx.Client", return_value=ctx):
        a = _fetch_ensemble_vars(40.0, -73.0, date.today(), "gfs_seamless", "temperature_2m_max")
        b = _fetch_ensemble_vars(40.0, -73.0, date.today(), "gfs_seamless", "temperature_2m_max")
    assert a == b and a is not None
    assert client.get.call_count == 1  # second served from cache


def test_expired_entry_refetches() -> None:
    ctx, client = _mock_httpx()
    with patch("src.forecast.httpx.Client", return_value=ctx):
        _fetch_ensemble_vars(40.0, -73.0, date.today(), "gfs_seamless", "temperature_2m_max")
        # Force the cached entry to look stale.
        with fc._forecast_cache_lock:
            (k, (_, v)), = fc._forecast_cache.items()
            fc._forecast_cache[k] = (0.0, v)
        _fetch_ensemble_vars(40.0, -73.0, date.today(), "gfs_seamless", "temperature_2m_max")
    assert client.get.call_count == 2  # stale entry forced a refetch


def test_distinct_keys_not_shared() -> None:
    ctx, client = _mock_httpx()
    with patch("src.forecast.httpx.Client", return_value=ctx):
        _fetch_ensemble_vars(40.0, -73.0, date.today(), "gfs_seamless", "temperature_2m_max")
        _fetch_ensemble_vars(51.5, -0.1, date.today(), "gfs_seamless", "temperature_2m_max")
    assert client.get.call_count == 2  # different city → separate fetch
