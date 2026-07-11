"""
Tests for momentum logging (yes_price_24h_ago on trade records) and the
event-overround alert helper. Both are additive instrumentation from the
2026-07-10 historical deep-dive; neither may affect trade selection.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.polymarket_client import MarketType, WeatherBucket, WeatherMarket
from src.scanner import event_overround
from src.strategy import Opportunity
from src.trader import _momentum_cache, execute_opportunity
from src.utils import TradeStore, now_utc


class _FakeForecast:
    confidence = 0.7
    mean_f = 90.0


def _market(bids: list[float]) -> WeatherMarket:
    buckets = [
        WeatherBucket(token_id=f"y{i}", outcome_label=f"{30 + i}°C", lower=i, upper=i + 1,
                      no_token_id=f"n{i}", best_ask=min(0.99, b + 0.03), best_bid=b,
                      best_ask_no=0.5)
        for i, b in enumerate(bids)
    ]
    return WeatherMarket(
        market_id="m1", question="q", city="Testville",
        target_date=(now_utc() + timedelta(hours=12)).date(),
        resolution_datetime=now_utc() + timedelta(hours=12),
        market_type=MarketType.TEMPERATURE, buckets=buckets,
    )


def _opportunity(m: WeatherMarket) -> Opportunity:
    return Opportunity(
        market=m, bucket=m.buckets[0], forecast=_FakeForecast(),
        model_prob=0.75, market_price=0.55, ev=0.36, confidence=0.7,
        kelly_fraction=0.25, suggested_size_usdc=1.0, side="no",
        trade_token_id=m.buckets[0].no_token_id,
    )


# ── event_overround ───────────────────────────────────────────────────────────
# The arb condition is on YES BIDS (buying NO executes at ~1 - YES_bid):
# summing asks would flag ordinary wide-spread books as guaranteed profit.

def test_overround_sums_priced_bids() -> None:
    assert abs(event_overround(_market([0.3, 0.4, 0.5])) - 1.2) < 1e-9


def test_overround_ignores_unpriced_buckets() -> None:
    # 0.0 and 1.0 bids are unpriced/degenerate — excluded from the sum.
    assert abs(event_overround(_market([0.3, 0.4, 0.5, 0.0, 1.0])) - 1.2) < 1e-9


def test_overround_needs_min_buckets() -> None:
    assert event_overround(_market([0.6, 0.6])) is None


def test_overround_wide_spread_book_is_not_an_arb() -> None:
    # Dust bids with fat asks (ask=bid+0.03 in the fixture, but bids near 0):
    # sum of ASKS would be ~0.4 here and sum of bids ~0.25 — neither trips the
    # 1.10 threshold. The regression this guards: an ask-based sum on a book
    # like bid 0.05/ask 0.30 across 8 buckets (asks 2.4, bids 0.4) must NOT
    # report an arb. With the bid-based helper the sum is 0.4 → no alert.
    m = _market([0.05] * 8)
    assert event_overround(m) < 1.0


# ── momentum logging on trade records ─────────────────────────────────────────

def _record_shadow(tmp_path: Path, momentum_price,
                   depth={"bid_depth_usdc": 42.5, "ask_depth_usdc": 17.0}) -> sqlite3.Row:
    _momentum_cache.clear()
    store = TradeStore(tmp_path / "trades.db")
    with patch("src.trader._trade_store", store), \
         patch("src.trader.send_telegram"), \
         patch("src.trader.fetch_book_depth", return_value=depth) as fbd, \
         patch("src.trader.fetch_yes_price_at", return_value=momentum_price) as fyp:
        execute_opportunity(_opportunity(_market([0.55, 0.3, 0.1])),
                            dry_run=False, shadow=True, quiet=True)
        fyp.assert_called_once()  # called with the YES token, once
        assert fyp.call_args[0][0] == "y0"
        fbd.assert_called_once_with("n0")  # depth on the TRADED (NO) token
    with sqlite3.connect(store._db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM trades").fetchone()


def test_momentum_price_recorded(tmp_path: Path) -> None:
    row = _record_shadow(tmp_path, 0.48)
    assert row["yes_price_24h_ago"] == 0.48
    assert row["shadow"] == 1


def test_momentum_lookup_failure_records_null(tmp_path: Path) -> None:
    row = _record_shadow(tmp_path, None)
    assert row["yes_price_24h_ago"] is None  # best-effort: NULL, trade still recorded


def test_book_depth_recorded(tmp_path: Path) -> None:
    row = _record_shadow(tmp_path, 0.48)
    assert row["book_bid_depth"] == 42.5
    assert row["book_ask_depth"] == 17.0


def test_book_depth_failure_records_null(tmp_path: Path) -> None:
    row = _record_shadow(tmp_path, 0.48, depth=None)
    assert row["book_bid_depth"] is None  # best-effort: NULL, trade still recorded
    assert row["book_ask_depth"] is None


def test_momentum_cached_across_shadow_and_live_legs(tmp_path: Path) -> None:
    _momentum_cache.clear()
    store = TradeStore(tmp_path / "trades.db")
    m = _market([0.55, 0.3, 0.1])
    with patch("src.trader._trade_store", store), \
         patch("src.trader.send_telegram"), \
         patch("src.trader.place_market_order",
               return_value={"status": "placed", "order_id": "0x1",
                             "fill_price": 0.56, "size_usdc": 2.8}), \
         patch("config.settings.settings.dry_run", False), \
         patch("src.trader.fetch_book_depth", return_value=None) as fbd, \
         patch("src.trader.fetch_yes_price_at", return_value=0.48) as fyp:
        execute_opportunity(_opportunity(m), dry_run=False, shadow=True, quiet=True)
        execute_opportunity(_opportunity(m), dry_run=False, quiet=True)
        fyp.assert_called_once()  # second leg served from cache
        assert fbd.call_count == 2  # depth is per-leg on purpose (book moves)
    with sqlite3.connect(store._db) as conn:
        vals = [r[0] for r in conn.execute("SELECT yes_price_24h_ago FROM trades")]
    assert vals == [0.48, 0.48]


def test_contrarian_flag_now_persists(tmp_path: Path) -> None:
    # Regression for the latent bug fixed 2026-07-10: contrarian was missing
    # from the INSERT column list and always persisted as 0.
    store = TradeStore(tmp_path / "trades.db")
    store.record(dict(city="X", target_date="2026-07-10", bucket_label="31°C",
                      side="no", contrarian=1, shadow=1, size_usdc=1, model_prob=0.7,
                      market_price=0.6, ev=0.3, market_id="m", token_id="t",
                      confidence=0.7, dry_run=0))
    with sqlite3.connect(store._db) as conn:
        assert conn.execute("SELECT contrarian FROM trades").fetchone()[0] == 1
