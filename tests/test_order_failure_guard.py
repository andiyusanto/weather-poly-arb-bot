"""
Tests for the failed-order recording guard in trader.execute_opportunity.

A live order that does not actually fill (SDK error, exchange reject, killed
FOK) must NOT be persisted to the trade store: a phantom row later "resolves"
against the market outcome and corrupts the live P&L tape. Only
status == "placed" results may be recorded in live mode.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.polymarket_client import MarketType, WeatherBucket, WeatherMarket
from src.strategy import Opportunity
from src.trader import execute_opportunity
from src.utils import TradeStore, now_utc


class _FakeForecast:
    confidence = 0.7
    mean_f = 90.0


def _opportunity() -> Opportunity:
    bucket = WeatherBucket(
        token_id="yes-tok", outcome_label="31°C", lower=87.0, upper=89.0,
        no_token_id="no-tok", best_ask=0.40, best_ask_no=0.55,
    )
    market = WeatherMarket(
        market_id="m1", question="q", city="Testville",
        target_date=(now_utc() + timedelta(hours=12)).date(),
        resolution_datetime=now_utc() + timedelta(hours=12),
        market_type=MarketType.TEMPERATURE, buckets=[bucket],
    )
    return Opportunity(
        market=market, bucket=bucket, forecast=_FakeForecast(),
        model_prob=0.75, market_price=0.55, ev=0.36, confidence=0.7,
        kelly_fraction=0.25, suggested_size_usdc=1.0, side="no",
        trade_token_id="no-tok",
    )


def _run_live(tmp_path: Path, order_result: dict) -> tuple[dict, int]:
    """Execute one live opportunity against a fresh store; return (result, n_rows)."""
    store = TradeStore(tmp_path / "trades.db")
    with patch("src.trader._trade_store", store), \
         patch("src.trader.place_market_order", return_value=order_result), \
         patch("src.trader.send_telegram"), \
         patch("src.trader.fetch_yes_price_at", return_value=None), \
         patch("src.trader.fetch_book_depth", return_value=None), \
         patch("config.settings.settings.dry_run", False):
        result = execute_opportunity(_opportunity(), dry_run=False, quiet=True)
    n_rows = len(store.traded_bucket_keys())
    return result, n_rows


def test_error_result_is_not_recorded(tmp_path: Path) -> None:
    # The insufficient-balance phantom: SDK returns error → no DB row.
    result, n_rows = _run_live(
        tmp_path, {"status": "error", "kind": "insufficient_balance", "error": "not enough balance"}
    )
    assert result["status"] == "error"
    assert n_rows == 0


def test_rejected_result_is_not_recorded(tmp_path: Path) -> None:
    # The killed-FOK phantom: exchange rejects → no DB row.
    result, n_rows = _run_live(tmp_path, {"status": "rejected", "error": "FOK killed"})
    assert result["status"] == "rejected"
    assert n_rows == 0


def test_placed_result_is_recorded_with_fill(tmp_path: Path) -> None:
    # A real fill must still be recorded, at the SDK-reported fill price/size.
    result, n_rows = _run_live(
        tmp_path,
        {"status": "placed", "order_id": "0xabc", "fill_price": 0.5359, "size_usdc": 2.73},
    )
    assert result["status"] == "placed"
    assert n_rows == 1


def test_slip_abort_is_not_recorded(tmp_path: Path) -> None:
    # Pre-order slippage abort was already guarded; keep it that way.
    result, n_rows = _run_live(
        tmp_path, {"status": "slip_abort", "estimate": 0.60, "slip_cents": 5.0}
    )
    assert result["status"] == "slip_abort"
    assert n_rows == 0
