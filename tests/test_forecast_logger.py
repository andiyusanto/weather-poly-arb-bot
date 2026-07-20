"""
Tests for the daily forecast logger (bias/sigma growth without trades).

Snapshots are first-write-wins per (city, target); resolution scores only
fully-elapsed target days, writes ensemble + station rows, and never
overwrites trade-recorded bias rows.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.bias_recorder as br
from config.settings import settings
from src.forecast import BiasStore


def _geo_stub(key):
    return {"lat": 14.6, "lon": 121.0, "timezone": "Asia/Manila", "display_name": key.title()}


def test_snapshot_first_write_wins(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(settings, "city_allowlist", "manila"), \
         patch("src.forecast.get_ensemble_forecast",
               return_value=SimpleNamespace(mean_f=90.5)):
        assert br.snapshot_daily_forecasts() == 1
    # second snapshot with a different mean must NOT overwrite
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(settings, "city_allowlist", "manila"), \
         patch("src.forecast.get_ensemble_forecast",
               return_value=SimpleNamespace(mean_f=95.0)):
        assert br.snapshot_daily_forecasts() == 0
    with sqlite3.connect(store._db) as c:
        rows = c.execute("SELECT city, forecast_mean FROM forecast_log").fetchall()
    assert rows == [("Manila", 90.5)]


def _log_past_snapshot(store: BiasStore, days_ago: int = 3, mean: float = 90.0) -> str:
    target = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date()
    store.log_forecast("Manila", "temperature", target, mean)
    return str(target)


def test_resolve_writes_ensemble_and_station_rows(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    target = _log_past_snapshot(store)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=32.6), \
         patch("src.station_obs.station_for_city", return_value="RPLL"), \
         patch("src.station_obs.fetch_station_daily_max_f", return_value=91.4):
        assert br.resolve_forecast_logs() == 1
    with sqlite3.connect(store._db) as c:
        rows = {m: o for m, o in c.execute(
            "SELECT model, observed FROM bias WHERE target_date=?", (target,))}
    assert abs(rows["ensemble"] - 90.68) < 0.01  # 32.6C -> F
    assert rows["station"] == 91.4


def test_resolve_skips_future_and_recent_targets(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    # target = today (local day not fully elapsed everywhere) -> must be skipped
    store.log_forecast("Manila", "temperature",
                       datetime.now(timezone.utc).date(), 90.0)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=32.6):
        assert br.resolve_forecast_logs() == 0


def test_resolve_never_overwrites_trade_rows(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    target = _log_past_snapshot(store, mean=90.0)
    # a trade already recorded ground truth for this (city, date)
    store.record(city="Manila", model="ensemble", variable="temperature",
                 target_date=date.fromisoformat(target),
                 forecast_mean=88.0, observed=91.0)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=32.6):
        assert br.resolve_forecast_logs() == 0  # nothing pending
    with sqlite3.connect(store._db) as c:
        fm = c.execute("SELECT forecast_mean FROM bias WHERE model='ensemble'").fetchone()[0]
    assert fm == 88.0  # trade row untouched


def test_resolve_tolerates_missing_observation(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    _log_past_snapshot(store)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=None):
        assert br.resolve_forecast_logs() == 0  # stays pending for a later pass
