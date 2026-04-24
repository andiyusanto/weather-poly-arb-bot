"""Shared utilities: logging setup, retry helpers, formatting, geocoding cache."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import pytz
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import LOGS_DIR, settings

T = TypeVar("T")

# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
        colorize=True,
    )
    log_file = LOGS_DIR / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
    logger.add(
        str(log_file),
        level="DEBUG",
        rotation="00:00",
        retention="14 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} — {message}",
    )


# ── Retry decorator ───────────────────────────────────────────────────────────

def http_retry(func: Callable[..., T]) -> Callable[..., T]:
    """Retry on transient HTTP / network failures with exponential backoff."""
    import requests
    import httpx

    return retry(
        retry=retry_if_exception_type((requests.RequestException, httpx.HTTPError, TimeoutError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )(func)


# ── Temperature conversions ───────────────────────────────────────────────────

def celsius_to_fahrenheit(c: float) -> float:
    return c * 9 / 5 + 32


def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32) * 5 / 9


# ── Timezone helpers ──────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def localize(dt: datetime, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    if dt.tzinfo is None:
        return tz.localize(dt)
    return dt.astimezone(tz)


def hours_until(dt: datetime) -> float:
    """Hours from now (UTC) until dt (must be tz-aware)."""
    delta = dt.astimezone(timezone.utc) - now_utc()
    return delta.total_seconds() / 3600


# ── Geocoding cache (SQLite) ──────────────────────────────────────────────────

class GeoCache:
    """Persist lat/lon/timezone lookups so we don't hammer Nominatim."""

    def __init__(self, db_path: Path) -> None:
        self._db = str(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geocache (
                    city_key TEXT PRIMARY KEY,
                    lat REAL,
                    lon REAL,
                    timezone TEXT,
                    country TEXT,
                    display_name TEXT,
                    priority INTEGER DEFAULT 0,
                    updated_at TEXT
                )
                """
            )
            conn.commit()

    def get(self, city_key: str) -> Optional[dict]:
        with sqlite3.connect(self._db) as conn:
            row = conn.execute(
                "SELECT lat, lon, timezone, country, display_name FROM geocache WHERE city_key=?",
                (city_key.lower(),),
            ).fetchone()
        if row:
            return dict(lat=row[0], lon=row[1], timezone=row[2], country=row[3], display_name=row[4])
        return None

    def set(self, city_key: str, lat: float, lon: float, timezone: str,
            country: str = "", display_name: str = "", priority: bool = False) -> None:
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO geocache
                    (city_key, lat, lon, timezone, country, display_name, priority, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (city_key.lower(), lat, lon, timezone, country, display_name, int(priority), now_utc().isoformat()),
            )
            conn.commit()

    def all_cities(self) -> list[dict]:
        with sqlite3.connect(self._db) as conn:
            rows = conn.execute(
                "SELECT city_key, lat, lon, timezone, country FROM geocache ORDER BY priority DESC"
            ).fetchall()
        return [dict(city_key=r[0], lat=r[1], lon=r[2], timezone=r[3], country=r[4]) for r in rows]


# ── Trade history DB ──────────────────────────────────────────────────────────

class TradeStore:
    """Lightweight SQLite store for executed/simulated trades."""

    def __init__(self, db_path: Path) -> None:
        self._db = str(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    token_id TEXT,
                    city TEXT,
                    bucket_label TEXT,
                    model_prob REAL,
                    market_price REAL,
                    ev REAL,
                    confidence REAL,
                    size_usdc REAL,
                    side TEXT,
                    dry_run INTEGER,
                    shadow INTEGER DEFAULT 0,
                    outcome TEXT,
                    pnl REAL,
                    timestamp TEXT,
                    resolved_at TEXT
                )
                """
            )
            self._migrate(conn)
            conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after initial schema without dropping data."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        for col, defn in [
            ("shadow",      "INTEGER DEFAULT 0"),
            ("resolved_at", "TEXT"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")

    def record(self, trade: dict) -> None:
        trade.setdefault("timestamp", now_utc().isoformat())
        trade.setdefault("outcome", None)
        trade.setdefault("pnl", None)
        trade.setdefault("shadow", 0)
        trade.setdefault("resolved_at", None)
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO trades
                    (market_id, token_id, city, bucket_label, model_prob, market_price, ev,
                     confidence, size_usdc, side, dry_run, shadow, outcome, pnl, timestamp, resolved_at)
                VALUES (:market_id,:token_id,:city,:bucket_label,:model_prob,:market_price,:ev,
                        :confidence,:size_usdc,:side,:dry_run,:shadow,:outcome,:pnl,:timestamp,:resolved_at)
                """,
                trade,
            )
            conn.commit()

    def today_spent(self) -> float:
        """Total USDC deployed today (live trades only, excludes shadow/dry-run)."""
        today = now_utc().date().isoformat()
        with sqlite3.connect(self._db) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_usdc),0) FROM trades WHERE DATE(timestamp)=? AND dry_run=0 AND shadow=0",
                (today,),
            ).fetchone()
        return float(row[0])

    def recent_trades(self, n: int = 100) -> list[dict]:
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_outcome(self, trade_id: int, outcome: str, pnl: float) -> None:
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                "UPDATE trades SET outcome=?, pnl=?, resolved_at=? WHERE id=?",
                (outcome, pnl, now_utc().isoformat(), trade_id),
            )
            conn.commit()

    def open_shadow_trades(self) -> list[dict]:
        """Shadow trades that have not yet been resolved."""
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE shadow=1 AND outcome IS NULL ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def shadow_stats(self) -> dict:
        """Aggregate performance stats for all resolved shadow trades."""
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE shadow=1 ORDER BY id"
            ).fetchall()
        trades = [dict(r) for r in rows]

        total = len(trades)
        resolved = [t for t in trades if t.get("outcome") is not None]
        open_count = total - len(resolved)
        wins = [t for t in resolved if t.get("outcome", "").lower() == "yes"]
        total_pnl = sum(t.get("pnl") or 0.0 for t in resolved)
        avg_ev = sum(t.get("ev") or 0.0 for t in trades) / max(total, 1)
        avg_conf = sum(t.get("confidence") or 0.0 for t in trades) / max(total, 1)

        return dict(
            total=total,
            resolved=len(resolved),
            open=open_count,
            wins=len(wins),
            win_rate=len(wins) / max(len(resolved), 1),
            total_pnl=total_pnl,
            avg_ev=avg_ev,
            avg_conf=avg_conf,
            trades=trades,
        )


# ── Misc formatting ───────────────────────────────────────────────────────────

def fmt_pct(v: float, decimals: int = 1) -> str:
    return f"{v*100:.{decimals}f}%"


def fmt_usdc(v: float) -> str:
    return f"${v:.2f}"


def bucket_label(lower: float, upper: float, unit: str = "°F") -> str:
    return f"{lower:.0f}–{upper:.0f}{unit}"


def safe_json(obj: Any) -> str:
    return json.dumps(obj, default=str, indent=2)


def rate_limited_sleep(seconds: float = 1.1) -> None:
    """Polite pause to respect public API rate limits."""
    time.sleep(seconds)
