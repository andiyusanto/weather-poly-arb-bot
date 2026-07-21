"""
Tests for the observation-only intraday book capture (#3 peak-passed edge).

Covers the real-time lock heuristic in ``fetch_station_intraday_state`` and the
capture writer: flag gating, row shape, locked-state propagation, and the
running-max→bucket containment flag. No network, no orders.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

import src.intraday_capture as ic
from config.settings import settings
from src.polymarket_client import MarketType, WeatherBucket, WeatherMarket
from src.station_obs import fetch_station_intraday_state

_MANILA = ZoneInfo("Asia/Manila")


def _manila_today() -> date:
    # The gate compares target_date to the city's real local date, so fixtures
    # must anchor to "now" the same way the code does.
    return datetime.now(timezone.utc).astimezone(_MANILA).date()


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


def _csv(day: str, hourly: list[tuple[int, float]]) -> str:
    lines = ["station,valid,tmpf"]
    for hh, t in hourly:
        lines.append(f"RPLL,{day} {hh:02d}:00,{t}")
    return "\n".join(lines)


def _fetch_state(csv_text: str):
    with patch("src.station_obs.httpx.Client") as cli:
        cli.return_value.__enter__.return_value.get.return_value = _FakeResp(csv_text)
        return fetch_station_intraday_state("RPLL", date(2026, 7, 7), "Asia/Manila")


# ── lock heuristic ────────────────────────────────────────────────────────────

def test_locked_after_peak_passed() -> None:
    # peak 91.4 at 14:00, temps fell for 4h and sit well below → locked
    st = _fetch_state(_csv("2026-07-07",
                           [(10, 80), (12, 86), (14, 91.4), (15, 90), (16, 88), (18, 83)]))
    assert st is not None
    assert st["running_max_f"] == 91.4
    assert st["peak_hour_local"] == 14.0
    assert st["locked"] is True


def test_not_locked_while_still_near_peak() -> None:
    # newest reading is the max (still climbing) → not locked
    st = _fetch_state(_csv("2026-07-07",
                           [(10, 80), (12, 86), (13, 90), (14, 91.4)]))
    assert st is not None and st["locked"] is False


def test_not_locked_morning_blip() -> None:
    # a morning high, still morning: afternoon could still exceed it → defer
    st = _fetch_state(_csv("2026-07-07",
                           [(6, 88), (8, 86), (9, 84), (10, 83)]))
    assert st is not None and st["locked"] is False


def test_pre_noon_max_locks_in_afternoon() -> None:
    # cold-front day: true max is pre-noon and temps fall all afternoon. Once
    # we're past mid-afternoon and still falling, it IS locked (the old
    # peak_hour>=12 guard wrongly refused this; last_hour>=15 fixes it).
    st = _fetch_state(_csv("2026-07-07",
                           [(8, 90), (10, 88), (12, 85), (14, 82), (16, 79), (18, 77)]))
    assert st is not None
    assert st["running_max_f"] == 90.0 and st["peak_hour_local"] == 8.0
    assert st["locked"] is True


def test_intraday_state_none_on_empty() -> None:
    assert _fetch_state("station,valid,tmpf") is None


def test_iem_fetch_retries_then_succeeds() -> None:
    # a transient IEM error (e.g. 429) must be retried, not lost — the bias
    # recorder and the capture both depend on this. sleep patched → instant.
    good = _csv("2026-07-07", [(10, 80), (14, 91.4), (16, 88), (18, 83)])
    with patch("src.station_obs.httpx.Client") as cli, \
         patch("src.station_obs.time.sleep") as slept:
        cli.return_value.__enter__.return_value.get.side_effect = [
            httpx.ConnectError("boom"),   # first attempt fails
            _FakeResp(good),              # retry succeeds
        ]
        st = fetch_station_intraday_state("RPLL", date(2026, 7, 7), "Asia/Manila")
    assert st is not None and st["running_max_f"] == 91.4
    assert slept.call_count == 1          # backed off exactly once before the retry


def test_one_bad_row_does_not_discard_the_day() -> None:
    # a single malformed timestamp must be skipped, not nuke the whole city
    csv_text = "\n".join([
        "station,valid,tmpf",
        "RPLL,2026-07-07 10:00,80",
        "RPLL,2026-07-07 BADTS,999",   # unparseable timestamp
        "RPLL,2026-07-07 14:00,91.4",
        "RPLL,2026-07-07 16:00,88",
        "RPLL,2026-07-07 18:00,83",
    ])
    st = _fetch_state(csv_text)
    assert st is not None
    assert st["running_max_f"] == 91.4   # computed from the good rows
    assert st["n_reports"] == 4          # the bad row excluded, not fatal


# ── capture writer ──────────────────────────────────────────────────────────

def _market(target: date | None = None, suffix: str = "") -> WeatherMarket:
    if target is None:
        target = _manila_today()  # the city's local today → passes the gate
    buckets = [
        WeatherBucket(token_id=f"tok_lo{suffix}", outcome_label="85-89°F", lower=85.0, upper=90.0,
                      best_ask=0.20, best_bid=0.16, volume_usdc=800.0),
        WeatherBucket(token_id=f"tok_hi{suffix}", outcome_label="90-94°F", lower=90.0, upper=95.0,
                      best_ask=0.55, best_bid=0.50, volume_usdc=1200.0),
    ]
    return WeatherMarket(
        market_id=f"mkt{suffix or '1'}",
        question=f"Will the highest temperature in Manila be 90-94°F on {target.isoformat()}?",
        city="Manila", target_date=target,
        resolution_datetime=datetime.now(timezone.utc).replace(hour=12, minute=0),
        market_type=MarketType.TEMPERATURE, buckets=buckets, total_volume_usdc=2000.0,
    )


def test_capture_disabled_returns_zero() -> None:
    with patch.object(settings, "intraday_capture", False):
        assert ic.capture_intraday_books() == 0


def test_capture_writes_rows_and_flags(tmp_path: Path) -> None:
    db = tmp_path / "intraday.db"
    state = {"running_max_f": 91.4, "peak_hour_local": 14.0, "last_hour_local": 18.0,
             "n_reports": 12, "hours_since_peak": 4.0, "locked": True}
    with patch.object(settings, "intraday_capture", True), \
         patch.object(settings, "city_allowlist", "Manila"), \
         patch.object(ic, "INTRADAY_DB", db), \
         patch.object(ic, "fetch_weather_markets", return_value=[_market()]), \
         patch.object(ic, "station_for_city", return_value="RPLL"), \
         patch.object(ic._geo, "get", return_value={"timezone": "Asia/Manila"}), \
         patch.object(ic, "fetch_station_intraday_state", return_value=state), \
         patch.object(ic, "fetch_book_depth",
                      return_value={"bid_depth_usdc": 40.0, "ask_depth_usdc": 25.0}):
        n = ic.capture_intraday_books()

    assert n == 2
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT bucket_label, running_max_f, locked, contains_running_max, "
        "ask_depth_usdc, implied_prob FROM intraday_snapshots ORDER BY bucket_lower"
    ).fetchall()
    assert len(rows) == 2
    # running max 91.4 lands in the 90-94 bucket, not the 85-89 one
    lo, hi = rows
    assert lo[0] == "85-89°F" and lo[3] == 0
    assert hi[0] == "90-94°F" and hi[3] == 1
    assert all(r[1] == 91.4 and r[2] == 1 and r[4] == 25.0 for r in rows)


def test_capture_survives_missing_station_obs(tmp_path: Path) -> None:
    # station fetch fails → rows still written with null obs, no crash
    db = tmp_path / "intraday.db"
    with patch.object(settings, "intraday_capture", True), \
         patch.object(settings, "city_allowlist", "Manila"), \
         patch.object(ic, "INTRADAY_DB", db), \
         patch.object(ic, "fetch_weather_markets", return_value=[_market()]), \
         patch.object(ic, "station_for_city", return_value="RPLL"), \
         patch.object(ic._geo, "get", return_value={"timezone": "Asia/Manila"}), \
         patch.object(ic, "fetch_station_intraday_state", return_value=None), \
         patch.object(ic, "fetch_book_depth", return_value={}):
        n = ic.capture_intraday_books()

    assert n == 2
    conn = sqlite3.connect(db)
    running, locked, contains = conn.execute(
        "SELECT running_max_f, locked, contains_running_max FROM intraday_snapshots LIMIT 1"
    ).fetchone()
    assert running is None and locked is None and contains is None


def test_obs_cache_keyed_by_city_and_date(tmp_path: Path) -> None:
    # same city, two qualifying dates (local today + yesterday): each date's
    # rows must carry ITS OWN running max, not the first-fetched date's (the
    # (city,date) cache-key fix).
    db = tmp_path / "intraday.db"
    today = _manila_today()
    yesterday = today - timedelta(days=1)
    m_today = _market(today, suffix="_t")
    m_yday = _market(yesterday, suffix="_y")
    per_date = {
        today: {"running_max_f": 91.4, "peak_hour_local": 14.0,
                "last_hour_local": 18.0, "n_reports": 12,
                "hours_since_peak": 4.0, "locked": True},
        yesterday: {"running_max_f": 85.0, "peak_hour_local": 13.0,
                    "last_hour_local": 18.0, "n_reports": 12,
                    "hours_since_peak": 5.0, "locked": False},
    }
    with patch.object(settings, "intraday_capture", True), \
         patch.object(settings, "city_allowlist", "Manila"), \
         patch.object(ic, "INTRADAY_DB", db), \
         patch("src.intraday_capture.time.sleep"), \
         patch.object(ic, "fetch_weather_markets", return_value=[m_today, m_yday]), \
         patch.object(ic, "station_for_city", return_value="RPLL"), \
         patch.object(ic._geo, "get", return_value={"timezone": "Asia/Manila"}), \
         patch.object(ic, "fetch_station_intraday_state",
                      side_effect=lambda icao, target, tz: per_date[target]), \
         patch.object(ic, "fetch_book_depth", return_value={}):
        n = ic.capture_intraday_books()

    assert n == 4  # 2 dates × 2 buckets
    conn = sqlite3.connect(db)
    got = dict(conn.execute(
        "SELECT target_date, running_max_f FROM intraday_snapshots GROUP BY target_date"
    ).fetchall())
    assert got == {today.isoformat(): 91.4, yesterday.isoformat(): 85.0}


def test_future_dated_market_excluded(tmp_path: Path) -> None:
    # a market for a future local day (weather hasn't happened) must be dropped
    db = tmp_path / "intraday.db"
    future = _market(_manila_today() + timedelta(days=3), suffix="_fut")
    with patch.object(settings, "intraday_capture", True), \
         patch.object(settings, "city_allowlist", "Manila"), \
         patch.object(ic, "INTRADAY_DB", db), \
         patch.object(ic, "fetch_weather_markets", return_value=[future]), \
         patch.object(ic, "station_for_city", return_value="RPLL"), \
         patch.object(ic._geo, "get", return_value={"timezone": "Asia/Manila"}), \
         patch.object(ic, "fetch_station_intraday_state", return_value=None), \
         patch.object(ic, "fetch_book_depth", return_value={}):
        n = ic.capture_intraday_books()
    assert n == 0
