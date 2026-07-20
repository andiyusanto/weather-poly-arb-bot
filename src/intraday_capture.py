"""
Intraday book capture — the market half of the #3 "peak-passed" edge.

RESEARCH / OBSERVATION ONLY. This module never places an order and is not part
of the scan→trade cycle. It exists to collect the one thing the 2026-07-20
ceiling backtest could not: what the market was pricing during the deterministic
window, i.e. after the daily max is meteorologically locked but while the market
is still open. Pairing that book snapshot with the (near-certain) station
outcome is what will later prove or kill a same-day intraday trading mode.

For each allowlist TEMPERATURE market resolving today, on each capture tick, we
snapshot:
  - the station's running daily max + a real-time "locked" flag
    (:func:`station_obs.fetch_station_intraday_state`)
  - every bucket's best ask/bid, NO ask, top-of-book depth, volume, and whether
    the running max currently falls inside that bucket

Rows land in a SEPARATE database (``intraday_book.db``) so this can never
perturb ``trades.db`` or the frozen verdict funnel. Run it from its own cron,
NOT from the trading process:

    */30 * * * *  cd /opt/weather-poly-arb-bot && INTRADAY_CAPTURE=true \\
                  python run.py capture-intraday >> logs/intraday.log 2>&1

Everything is best-effort: any per-market/per-bucket failure is logged and
skipped; the capture never raises.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from config.settings import CITIES_CACHE_DB, INTRADAY_DB, settings
from src.polymarket_client import (
    MarketType,
    WeatherBucket,
    WeatherMarket,
    fetch_book_depth,
    fetch_weather_markets,
)
from src.station_obs import fetch_station_intraday_state, station_for_city
from src.utils import GeoCache, hours_until

_geo = GeoCache(CITIES_CACHE_DB)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at          TEXT NOT NULL,   -- UTC ISO, one value per capture run
    city                 TEXT NOT NULL,
    target_date          TEXT NOT NULL,
    icao                 TEXT,
    -- station intraday state (same for every bucket of a city in one run)
    running_max_f        REAL,
    peak_hour_local      REAL,
    last_hour_local      REAL,
    n_reports            INTEGER,
    hours_since_peak     REAL,
    locked               INTEGER,         -- 1 = daily max meteorologically locked
    -- market / bucket
    market_id            TEXT,
    resolution_dt        TEXT,
    hours_to_resolution  REAL,
    bucket_label         TEXT,
    bucket_lower         REAL,
    bucket_upper         REAL,
    contains_running_max INTEGER,         -- 1 = running max in [lower, upper)
    token_id             TEXT,
    best_ask             REAL,            -- YES ask
    best_bid             REAL,            -- YES bid
    no_ask               REAL,
    implied_prob         REAL,            -- bucket mid as implied probability
    bid_depth_usdc       REAL,
    ask_depth_usdc       REAL,
    volume_usdc          REAL
);
-- Idempotent re-runs: a (run timestamp, token) pair is unique.
CREATE UNIQUE INDEX IF NOT EXISTS ix_intraday_run_token
    ON intraday_snapshots (captured_at, token_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(INTRADAY_DB)
    conn.executescript(_SCHEMA)
    return conn


def _contains(bucket: WeatherBucket, value: Optional[float]) -> Optional[int]:
    """1 if value falls in [lower, upper) (sentinels handled), else 0/None."""
    if value is None:
        return None
    return int(bucket.lower <= value < bucket.upper)


def _qualifying_markets(markets: list[WeatherMarket]) -> list[WeatherMarket]:
    """Allowlist temperature markets resolving within the capture window."""
    allow = settings.city_allowlist_set
    out: list[WeatherMarket] = []
    for m in markets:
        if m.market_type != MarketType.TEMPERATURE:
            continue
        if allow and m.city.strip().lower() not in allow:
            continue
        if not m.resolution_datetime:
            continue
        h = hours_until(m.resolution_datetime)
        if 0 < h <= settings.intraday_capture_max_hours:
            out.append(m)
    return out


def capture_intraday_books() -> int:
    """
    Snapshot books + station state for today's allowlist temperature markets.

    Returns:
        Number of bucket rows written this run (0 if the flag is off, nothing
        qualifies, or every leg failed). Never raises.
    """
    if not settings.intraday_capture:
        logger.debug("intraday_capture disabled — skipping")
        return 0

    captured_at = datetime.now(timezone.utc).isoformat()
    try:
        # enabled_types is matched against MarketType.value (a string) inside
        # fetch_weather_markets, so pass the string, not the enum member.
        markets = fetch_weather_markets(enabled_types={MarketType.TEMPERATURE.value})
    except Exception as e:  # market discovery is entirely external — never fatal here
        logger.error(f"intraday capture: market fetch failed: {e}")
        return 0

    qualifying = _qualifying_markets(markets)
    if not qualifying:
        logger.info("intraday capture: no allowlist temperature markets in window")
        return 0

    # Fetch each (city, date) station state once (IEM is rate-limited; many
    # buckets share a city-day). Keyed by (city, target_date) — NOT city alone —
    # so a city with two dates in the window never reuses the wrong day's max.
    # None on failure — we still log the book with null obs.
    obs_by_key: dict[tuple[str, object], Optional[dict]] = {}
    for m in qualifying:
        key = (m.city.strip().lower(), m.target_date)
        if key in obs_by_key:
            continue
        icao = station_for_city(m.city.strip())
        geo = _geo.get(m.city.strip())
        tz = geo.get("timezone") if geo else None
        obs_by_key[key] = (
            fetch_station_intraday_state(icao, m.target_date, tz)
            if (icao and tz) else None
        )

    # Collect every (market, bucket) leg, fetch depth concurrently (bounded).
    legs = [(m, b) for m in qualifying for b in m.buckets if b.token_id]

    def _depth(leg: tuple[WeatherMarket, WeatherBucket]) -> dict:
        return fetch_book_depth(leg[1].token_id) or {}

    depths: list[dict] = []
    if legs:
        workers = min(settings.max_concurrency, len(legs), 10)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                depths = list(pool.map(_depth, legs))
        except Exception as e:
            logger.warning(f"intraday capture: depth fetch pool error: {e}")
            depths = [{} for _ in legs]

    rows = []
    for (m, b), depth in zip(legs, depths):
        icao = station_for_city(m.city.strip())
        obs = obs_by_key.get((m.city.strip().lower(), m.target_date)) or {}
        rows.append((
            captured_at, m.city.strip(), m.target_date.isoformat(), icao,
            obs.get("running_max_f"), obs.get("peak_hour_local"),
            obs.get("last_hour_local"), obs.get("n_reports"),
            obs.get("hours_since_peak"),
            int(obs["locked"]) if "locked" in obs else None,
            m.market_id,
            m.resolution_datetime.isoformat() if m.resolution_datetime else None,
            round(hours_until(m.resolution_datetime), 2) if m.resolution_datetime else None,
            b.outcome_label, b.lower, b.upper,
            _contains(b, obs.get("running_max_f")),
            b.token_id, b.best_ask, b.best_bid, b.no_ask, b.mid_price,
            depth.get("bid_depth_usdc"), depth.get("ask_depth_usdc"),
            b.volume_usdc,
        ))

    try:
        with _connect() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO intraday_snapshots (
                    captured_at, city, target_date, icao,
                    running_max_f, peak_hour_local, last_hour_local, n_reports,
                    hours_since_peak, locked,
                    market_id, resolution_dt, hours_to_resolution,
                    bucket_label, bucket_lower, bucket_upper, contains_running_max,
                    token_id, best_ask, best_bid, no_ask, implied_prob,
                    bid_depth_usdc, ask_depth_usdc, volume_usdc
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
    except sqlite3.Error as e:
        logger.error(f"intraday capture: db write failed: {e}")
        return 0

    n_locked = sum(1 for (m, _), _ in zip(legs, depths)
                   if (obs_by_key.get((m.city.strip().lower(), m.target_date)) or {}).get("locked"))
    logger.info(
        f"intraday capture: {len(rows)} bucket rows across "
        f"{len(qualifying)} markets ({len({m.city for m in qualifying})} cities); "
        f"{n_locked} rows in locked (peak-passed) state"
    )
    return len(rows)
