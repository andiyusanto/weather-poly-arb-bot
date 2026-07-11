"""
Tests for the settlement-station ground-truth pipeline (2026-07-11 audit fix).

Station rows (model='station') are recorded in parallel with OM rows and must
stay isolated: excluded from source='om' error queries (live behavior
byte-identical), selectable via source='station', with automatic om-fallback
while station history is thin.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import src.bias_recorder as br
from src.forecast import BiasStore
from src.station_obs import fetch_station_daily_max_f


# ── source separation in BiasStore ───────────────────────────────────────────

def _seed(store: BiasStore, model: str, errs: list[float], city: str = "S") -> None:
    for i, e in enumerate(errs):
        store.record(city=city, model=model, variable="temperature",
                     target_date=date(2026, 6, 1 + i),
                     forecast_mean=90.0, observed=90.0 + e)


def test_station_rows_excluded_from_om_source(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    _seed(store, "ensemble", [1.0] * 5)
    _seed(store, "station", [5.0] * 5)  # big station errors must not leak into om
    om = store._combined_errors("temperature", source="om")
    assert om == [1.0] * 5
    st = store._combined_errors("temperature", source="station")
    assert st == [5.0] * 5


def test_station_sigma_falls_back_to_om_when_thin(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    _seed(store, "ensemble", [2.0, -2.0] * 12)          # 24 om samples
    _seed(store, "station", [4.0, -4.0])                # only 2 station samples
    sig = store.city_error_sigma("S", min_global=20, source="station")
    assert sig is not None and 1.5 < sig < 2.5          # om-fallback sigma, not 4.0


def test_station_sigma_used_when_history_sufficient(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "b.db")
    _seed(store, "ensemble", [1.0, -1.0] * 12)
    _seed(store, "station", [3.0, -3.0] * 12)
    sig = store.city_error_sigma("S", min_global=20, source="station")
    assert sig is not None and sig > 2.5                # station-scale sigma


# ── IEM CSV parsing ──────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, text: str):
        self.text = text
    def raise_for_status(self) -> None:
        pass


def _iem_csv(day: str, temps: list[float], other_day_temp: float = 60.0) -> str:
    lines = ["station,valid,tmpf"]
    for i, t in enumerate(temps):
        lines.append(f"RPLL,{day} {i:02d}:00,{t}")
    lines.append(f"RPLL,2026-07-08 00:00,{other_day_temp}")  # next local day: excluded
    return "\n".join(lines)


def test_iem_daily_max_filters_local_day(tmp_path: Path) -> None:
    csv_text = _iem_csv("2026-07-07", [80.0, 85.0, 91.4, 88.0, 84.0, 82.0], other_day_temp=99.0)
    with patch("src.station_obs.httpx.Client") as cli:
        cli.return_value.__enter__.return_value.get.return_value = _FakeResp(csv_text)
        v = fetch_station_daily_max_f("RPLL", date(2026, 7, 7), "Asia/Manila")
    assert v == 91.4  # next-day 99.0 excluded


def test_iem_thin_coverage_returns_none() -> None:
    csv_text = _iem_csv("2026-07-07", [80.0, 85.0])  # 2 reports < 6 minimum
    with patch("src.station_obs.httpx.Client") as cli:
        cli.return_value.__enter__.return_value.get.return_value = _FakeResp(csv_text)
        assert fetch_station_daily_max_f("RPLL", date(2026, 7, 7), "Asia/Manila") is None


# ── recorder writes the parallel station row ─────────────────────────────────

def test_recorder_writes_parallel_station_row(tmp_path: Path) -> None:
    import sqlite3
    store = BiasStore(tmp_path / "b.db")
    trade = dict(city="Manila", target_date="2026-07-07", market_type="temperature",
                 forecast_mean=90.3, timestamp="2026-07-06T12:00:00+00:00",
                 model_means=json.dumps({"icon_seamless": 91.0}))
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", return_value={"lat": 14.6, "lon": 121.0,
                                                    "timezone": "Asia/Manila"}), \
         patch.object(br, "_fetch_observed", return_value=32.6), \
         patch("src.station_obs.station_for_city", return_value="RPLL"), \
         patch("src.station_obs.fetch_station_daily_max_f", return_value=91.4):
        assert br.record_bias_for_resolved_trade(trade) is True
    with sqlite3.connect(store._db) as conn:
        rows = {m: o for m, o in conn.execute("SELECT model, observed FROM bias")}
    assert "station" in rows and rows["station"] == 91.4
    assert "ensemble" in rows  # OM row still written alongside


def test_recorder_survives_station_fetch_failure(tmp_path: Path) -> None:
    import sqlite3
    store = BiasStore(tmp_path / "b.db")
    trade = dict(city="Manila", target_date="2026-07-07", market_type="temperature",
                 forecast_mean=90.3, timestamp="2026-07-06T12:00:00+00:00")
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", return_value={"lat": 14.6, "lon": 121.0,
                                                    "timezone": "Asia/Manila"}), \
         patch.object(br, "_fetch_observed", return_value=32.6), \
         patch("src.station_obs.station_for_city", return_value="RPLL"), \
         patch("src.station_obs.fetch_station_daily_max_f", return_value=None):
        assert br.record_bias_for_resolved_trade(trade) is True  # OM path unaffected
    with sqlite3.connect(store._db) as conn:
        models = {r[0] for r in conn.execute("SELECT model FROM bias")}
    assert "station" not in models and "ensemble" in models
