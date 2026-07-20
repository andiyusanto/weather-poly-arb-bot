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


def test_snapshot_first_write_wins_both_leads(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(settings, "city_allowlist", "manila"), \
         patch("src.forecast.get_ensemble_forecast",
               return_value=SimpleNamespace(mean_f=90.5)):
        assert br.snapshot_daily_forecasts() == 2  # day_ahead + same_day
    # second snapshot with a different mean must NOT overwrite either lead
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(settings, "city_allowlist", "manila"), \
         patch("src.forecast.get_ensemble_forecast",
               return_value=SimpleNamespace(mean_f=95.0)):
        assert br.snapshot_daily_forecasts() == 0
    with sqlite3.connect(store._db) as c:
        leads = {lead: fm for lead, fm in c.execute(
            "SELECT lead, forecast_mean FROM forecast_log ORDER BY lead")}
    assert leads == {"day_ahead": 90.5, "same_day": 90.5}  # both kept, neither overwritten


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


def test_resolve_never_overwrites_om_trade_row(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    target = _log_past_snapshot(store, mean=90.0)
    # a trade already recorded BOTH ground-truth rows for this (city, date)
    for model, obs in (("ensemble", 91.0), ("station", 92.0)):
        store.record(city="Manila", model=model, variable="temperature",
                     target_date=date.fromisoformat(target),
                     forecast_mean=88.0, observed=obs)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=32.6), \
         patch("src.station_obs.station_for_city", return_value="RPLL"), \
         patch("src.station_obs.fetch_station_daily_max_f", return_value=99.0):
        assert br.resolve_forecast_logs() == 0  # both legs already present
    with sqlite3.connect(store._db) as c:
        fm = c.execute("SELECT forecast_mean FROM bias WHERE model='ensemble'").fetchone()[0]
        so = c.execute("SELECT observed FROM bias WHERE model='station'").fetchone()[0]
    assert fm == 88.0 and so == 92.0  # trade rows untouched, not 99.0


def test_resolve_retries_only_missing_station_leg(tmp_path: Path) -> None:
    # OM row already written (prior pass), station lost to an IEM failure:
    # the station leg must be retried WITHOUT rewriting the OM row.
    store = BiasStore(tmp_path / "b.db")
    target = _log_past_snapshot(store, mean=90.0)
    store.record(city="Manila", model="ensemble", variable="temperature",
                 target_date=date.fromisoformat(target),
                 forecast_mean=90.0, observed=90.5)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=99.0) as om, \
         patch("src.station_obs.station_for_city", return_value="RPLL"), \
         patch("src.station_obs.fetch_station_daily_max_f", return_value=91.4):
        assert br.resolve_forecast_logs() == 1
        om.assert_not_called()  # OM leg not needed → no refetch
    with sqlite3.connect(store._db) as c:
        rows = {m: o for m, o in c.execute("SELECT model, observed FROM bias")}
    assert rows["ensemble"] == 90.5   # untouched
    assert rows["station"] == 91.4    # newly written


def test_resolve_stays_pending_when_both_legs_fail(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    _log_past_snapshot(store)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=None), \
         patch("src.station_obs.station_for_city", return_value="RPLL"), \
         patch("src.station_obs.fetch_station_daily_max_f", return_value=None):
        assert br.resolve_forecast_logs() == 0  # both legs failed → still pending
    with sqlite3.connect(store._db) as c:
        assert c.execute("SELECT count(*) FROM bias").fetchone()[0] == 0


def test_resolve_abandons_snapshots_past_max_age(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    _log_past_snapshot(store, days_ago=br.RESOLVE_MAX_AGE_DAYS + 5)
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(br, "_fetch_observed", return_value=32.6) as om:
        assert br.resolve_forecast_logs() == 0
        om.assert_not_called()  # too old → never fetched


def test_snapshot_all_cities_includes_station_mapped(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(settings, "city_allowlist", "manila"), \
         patch.object(settings, "forecast_log_all_cities", True), \
         patch("src.station_obs.mapped_cities", return_value={"manila", "tokyo", "paris"}), \
         patch("src.forecast.get_ensemble_forecast",
               return_value=SimpleNamespace(mean_f=90.5)):
        assert br.snapshot_daily_forecasts() == 6  # (union of 3) × 2 leads


def test_snapshot_records_zero_fahrenheit_mean(tmp_path: Path) -> None:
    # 0.0°F is a real winter high (Moscow/Helsinki/Toronto), not "missing".
    store = BiasStore(tmp_path / "b.db")
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(settings, "city_allowlist", "moscow"), \
         patch.object(settings, "forecast_log_all_cities", False), \
         patch("src.forecast.get_ensemble_forecast",
               return_value=SimpleNamespace(mean_f=0.0)):
        assert br.snapshot_daily_forecasts() == 2  # both leads
    with sqlite3.connect(store._db) as c:
        assert c.execute("SELECT forecast_mean FROM forecast_log").fetchone()[0] == 0.0


def test_snapshot_passes_allow_intraday_false(tmp_path: Path) -> None:
    # The logger must request a PURE day-ahead mean (no intraday clamp), else
    # a tz-ahead box permanently contaminates first-write-wins snapshots.
    store = BiasStore(tmp_path / "b.db")
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", side_effect=_geo_stub), \
         patch.object(settings, "city_allowlist", "manila"), \
         patch.object(settings, "forecast_log_all_cities", False), \
         patch("src.forecast.get_ensemble_forecast",
               return_value=SimpleNamespace(mean_f=90.0)) as gef:
        br.snapshot_daily_forecasts()
        assert gef.call_args.kwargs.get("allow_intraday") is False


def test_lead_stratified_sigma_is_independent(tmp_path: Path) -> None:
    # day_ahead and same_day errors live under separate bias tags and yield
    # separate sigmas — the whole point of lead stratification.
    from src.forecast import (LEAD_DAY_AHEAD, LEAD_SAME_DAY,
                              om_bias_model, station_bias_model)
    store = BiasStore(tmp_path / "b.db")
    for i in range(30):
        d = date(2026, 6, 1) + timedelta(days=i)
        # wide day-ahead errors, tight same-day errors (skill improves w/ lead)
        store.record(city="Manila", model=om_bias_model(LEAD_DAY_AHEAD),
                     variable="temperature", target_date=d,
                     forecast_mean=90.0, observed=90.0 + (3.0 if i % 2 else -3.0))
        store.record(city="Manila", model=om_bias_model(LEAD_SAME_DAY),
                     variable="temperature", target_date=d,
                     forecast_mean=90.0, observed=90.0 + (0.8 if i % 2 else -0.8))
    sig_da = store.city_error_sigma("Manila", min_global=20, lead=LEAD_DAY_AHEAD)
    sig_sd = store.city_error_sigma("Manila", min_global=20, lead=LEAD_SAME_DAY)
    assert sig_da > 2.5 and sig_sd < 1.2   # distinct, same_day much tighter
    # day_ahead tag must not see same_day rows and vice versa
    assert om_bias_model(LEAD_DAY_AHEAD) == "ensemble"
    assert om_bias_model(LEAD_SAME_DAY) == "ensemble@sameday"
    assert station_bias_model(LEAD_SAME_DAY) == "station@sameday"


def test_same_day_sigma_falls_back_to_day_ahead_when_thin(tmp_path: Path) -> None:
    from src.forecast import LEAD_DAY_AHEAD, LEAD_SAME_DAY, om_bias_model
    store = BiasStore(tmp_path / "b.db")
    for i in range(30):  # only day_ahead history exists
        store.record(city="Manila", model=om_bias_model(LEAD_DAY_AHEAD),
                     variable="temperature", target_date=date(2026, 6, 1) + timedelta(days=i),
                     forecast_mean=90.0, observed=90.0 + (2.0 if i % 2 else -2.0))
    # same_day requested but empty → falls back to day_ahead sigma, not None
    sig = store.city_error_sigma("Manila", min_global=20, lead=LEAD_SAME_DAY)
    assert sig is not None and 1.5 < sig < 2.5


def test_legacy_forecast_log_migrates_to_lead_pk(tmp_path: Path) -> None:
    # A pre-lead forecast_log (old PK) must rebuild so same_day rows don't
    # collide with day_ahead on the old (city,variable,target) PK.
    import sqlite3
    db = tmp_path / "b.db"
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE forecast_log (city TEXT, variable TEXT,
                     target_date TEXT, forecast_mean REAL, created_at TEXT,
                     PRIMARY KEY (city, variable, target_date))""")
        c.execute("INSERT INTO forecast_log VALUES ('Manila','temperature','2026-07-19',90.0,'x')")
    store = BiasStore(db)  # triggers migration
    # old row preserved as day_ahead, and a same_day row now coexists
    assert store.log_forecast("Manila", "temperature", date(2026, 7, 19), 88.0,
                              lead="same_day") is True
    with sqlite3.connect(db) as c:
        leads = sorted(r[0] for r in c.execute(
            "SELECT lead FROM forecast_log WHERE target_date='2026-07-19'"))
    assert leads == ["day_ahead", "same_day"]
