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
    # Use loguru's own time-templating ({time:YYYYMMDD}) so the date is resolved
    # at WRITE time, not at process start. Without this the path is frozen at
    # startup — a process started today keeps writing to today's file forever,
    # and at midnight loguru rotates it but reopens the SAME frozen path,
    # leaving a wrong-day filename and a discontinuous log for post-mortems.
    log_file = str(LOGS_DIR / "bot_{time:YYYYMMDD}.log")
    logger.add(
        log_file,
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
            ("shadow",        "INTEGER DEFAULT 0"),
            ("resolved_at",   "TEXT"),
            ("market_type",   "TEXT"),
            ("target_date",   "TEXT"),
            ("forecast_mean", "REAL"),     # mean of forecast variable at trade time
            ("condition_id",  "TEXT"),     # Polymarket conditionId for resolution lookup
            ("contrarian",    "INTEGER DEFAULT 0"),  # 1 if side was flipped from YES→NO by Option F
            ("yes_price_24h_ago", "REAL"),  # YES price ~24h before entry (momentum logging)
            ("model_means", "TEXT"),        # JSON {model: mean_f} at trade time (per-model bias/BMA)
            ("ensemble_spread", "REAL"),    # combined ensemble std_f at trade time (full-EMOS study)
            ("book_bid_depth", "REAL"),     # traded token's top-of-book bid depth in USDC at entry
            ("book_ask_depth", "REAL"),     # traded token's top-of-book ask depth in USDC at entry
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")

    def record(self, trade: dict) -> None:
        trade.setdefault("timestamp", now_utc().isoformat())
        trade.setdefault("outcome", None)
        trade.setdefault("pnl", None)
        trade.setdefault("shadow", 0)
        trade.setdefault("resolved_at", None)
        trade.setdefault("market_type", "")
        trade.setdefault("target_date", "")
        trade.setdefault("forecast_mean", None)
        trade.setdefault("condition_id", "")
        trade.setdefault("contrarian", 0)
        trade.setdefault("yes_price_24h_ago", None)
        trade.setdefault("model_means", None)
        trade.setdefault("ensemble_spread", None)
        trade.setdefault("book_bid_depth", None)
        trade.setdefault("book_ask_depth", None)
        with sqlite3.connect(self._db) as conn:
            # NOTE: contrarian was in the migration + setdefault but missing
            # from this INSERT until 2026-07-10, so it silently persisted as
            # the column default 0 (harmless while contrarian_yes_inversion
            # stayed off, but a live bug the moment it's enabled).
            conn.execute(
                """
                INSERT INTO trades
                    (market_id, token_id, city, bucket_label, model_prob, market_price, ev,
                     confidence, size_usdc, side, dry_run, shadow, outcome, pnl, timestamp,
                     resolved_at, market_type, target_date, forecast_mean, condition_id,
                     contrarian, yes_price_24h_ago, model_means, ensemble_spread,
                     book_bid_depth, book_ask_depth)
                VALUES (:market_id,:token_id,:city,:bucket_label,:model_prob,:market_price,:ev,
                        :confidence,:size_usdc,:side,:dry_run,:shadow,:outcome,:pnl,:timestamp,
                        :resolved_at,:market_type,:target_date,:forecast_mean,:condition_id,
                        :contrarian,:yes_price_24h_ago,:model_means,:ensemble_spread,
                        :book_bid_depth,:book_ask_depth)
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

    def trades_today_by_city(self) -> dict[str, int]:
        """Count of LIVE trades placed today, grouped by city."""
        today = now_utc().date().isoformat()
        with sqlite3.connect(self._db) as conn:
            rows = conn.execute(
                "SELECT city, COUNT(*) FROM trades "
                "WHERE DATE(timestamp)=? AND dry_run=0 AND shadow=0 "
                "GROUP BY city",
                (today,),
            ).fetchall()
        return {r[0] or "": int(r[1]) for r in rows}

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

    def traded_bucket_keys(self, shadow: bool | None = None) -> set[tuple]:
        """
        Return the set of (city, target_date, bucket_label, side, contrarian) already
        traded.

        Args:
            shadow: If given, restrict to shadow rows (True) or non-shadow rows
                (False). None (default) keeps the historical behavior of one
                shared namespace. Live and parallel-shadow dedup MUST use
                separate namespaces — otherwise a shadow record of a
                budget-skipped opportunity would block the real trade next cycle.

        Used to enforce one bet per (bucket, strategy) across cycles: the trader
        re-scans every interval, so without this guard the same bucket is re-entered
        each cycle — inflating the shadow sample and over-concentrating live capital.

        The ``contrarian`` flag is part of the key because contrarian NO and natural
        NO bets on the same bucket are *different* strategies that should be allowed
        to coexist: natural NO follows the model's NO conviction; contrarian NO is a
        deliberate mirror of a YES pick. They share an outcome direction but have
        different mathematical justification, so a prior natural NO should not block
        a fresh contrarian on the same bucket (and vice versa).
        """
        with sqlite3.connect(self._db) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
            has_contrarian = "contrarian" in cols
            sel = "city, target_date, bucket_label, side"
            sel += ", contrarian" if has_contrarian else ""
            where = ""
            params: tuple = ()
            if shadow is not None:
                where = " WHERE shadow=?"
                params = (int(shadow),)
            rows = conn.execute(f"SELECT {sel} FROM trades{where}", params).fetchall()
        return {
            (
                (r[0] or ""),
                (r[1] or ""),
                (r[2] or ""),
                (r[3] or "yes").lower(),
                int(r[4]) if has_contrarian and r[4] is not None else 0,
            )
            for r in rows
        }

    def open_unresolved_trades(self) -> list[dict]:
        """
        Trades that still need resolution against the CLOB — both SHADOW rows
        (paper trades for validation) and LIVE rows (real money on Polymarket).

        Both need their ``outcome`` + ``pnl`` columns populated so analytics
        (``side-pnl``, ``slice-dash``, ``contrarian-pnl``) can see them as
        "resolved". For live trades the on-chain settlement happens via
        Polymarket's auto_redeem_operator independent of this; we just need the
        local DB to reflect ground truth.

        Excludes ``dry_run=1`` rows (legacy log-only trades the bot never
        actually placed — nothing to resolve there).
        """
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades "
                "WHERE outcome IS NULL "
                "  AND (shadow=1 OR (shadow=0 AND dry_run=0)) "
                "ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # Backward-compat shim — old name still callable so external scripts /
    # cron jobs don't break mid-deploy. New code should call
    # ``open_unresolved_trades`` directly.
    def open_shadow_trades(self) -> list[dict]:
        return self.open_unresolved_trades()

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
        # A trade is a win when the side we bought matches the resolution.
        def _won(t: dict) -> bool:
            side = (t.get("side") or "yes").lower()
            outcome = (t.get("outcome") or "").lower()
            return (side == "yes" and outcome == "yes") or (side == "no" and outcome == "no")
        wins = [t for t in resolved if _won(t)]
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
