"""
Trader — executes opportunities, manages daily limits, sends Telegram alerts,
records all trades to SQLite.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from loguru import logger

from config.settings import settings
from src.polymarket_client import place_market_order
from src.scanner import ScanResult, display_opportunities, run_scan
from src.strategy import Opportunity, apply_daily_limit
from src.utils import TradeStore, fmt_pct, fmt_usdc, now_utc
from config.settings import TRADES_DB

_trade_store = TradeStore(TRADES_DB)


# ── Telegram ──────────────────────────────────────────────────────────────────

async def _send_telegram(text: str) -> None:
    if not settings.has_telegram:
        return
    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def send_telegram(text: str) -> None:
    """Sync wrapper for async Telegram send."""
    try:
        asyncio.get_event_loop().run_until_complete(_send_telegram(text))
    except RuntimeError:
        asyncio.run(_send_telegram(text))


def _opportunity_alert(opp: Opportunity, order_result: Optional[dict] = None) -> str:
    """Format a Telegram alert for a single opportunity."""
    dry = settings.dry_run or (order_result and order_result.get("status") == "dry_run")
    tag = "🔵 DRY RUN" if dry else "🟢 TRADE EXECUTED"
    hrs = f"{opp.hours_to_resolution:.0f}h" if opp.hours_to_resolution is not None else "?"

    lines = [
        f"{tag}",
        f"",
        f"<b>{opp.market.question}</b>",
        f"",
        f"🌡️ Bucket:  <code>{opp.bucket.outcome_label}</code>",
        f"📊 Model:   <b>{fmt_pct(opp.model_prob)}</b>",
        f"💰 Market:  {fmt_pct(opp.market_price)} ask",
        f"📈 EV:      <b>{fmt_pct(opp.ev)}</b>",
        f"🎯 Conf:    {fmt_pct(opp.confidence)}",
        f"💵 Size:    {fmt_usdc(opp.suggested_size_usdc)}",
        f"⏱️ Resolution: {hrs}",
        f"",
        f"🔗 <a href='https://polymarket.com/event/{opp.market.market_id}'>View on Polymarket</a>",
    ]
    return "\n".join(lines)


def _daily_summary_alert(result: ScanResult, executed: List[Opportunity]) -> str:
    total_size = sum(o.suggested_size_usdc for o in executed)
    avg_ev = sum(o.ev for o in executed) / max(len(executed), 1)
    lines = [
        f"📅 <b>Daily Scan Summary</b> — {now_utc().strftime('%Y-%m-%d %H:%M UTC')}",
        f"",
        f"Markets scanned: {result.markets_scanned}",
        f"Opportunities:   {len(result.opportunities)}",
        f"Trades taken:    {len(executed)}",
        f"Total deployed:  {fmt_usdc(total_size)}",
        f"Avg EV:          {fmt_pct(avg_ev)}",
        f"Errors:          {result.errors}",
    ]
    return "\n".join(lines)


# ── Trade execution ───────────────────────────────────────────────────────────

def execute_opportunity(opp: Opportunity, dry_run: bool = True) -> Optional[dict]:
    """Place order and record to DB. Returns order result dict."""
    result = place_market_order(
        token_id=opp.bucket.token_id,
        side="BUY",
        size_usdc=opp.suggested_size_usdc,
        dry_run=dry_run,
    )

    trade_record = dict(
        market_id=opp.market.market_id,
        token_id=opp.bucket.token_id,
        city=opp.market.city,
        bucket_label=opp.bucket.outcome_label,
        model_prob=opp.model_prob,
        market_price=opp.market_price,
        ev=opp.ev,
        confidence=opp.confidence,
        size_usdc=opp.suggested_size_usdc,
        side="BUY",
        dry_run=int(dry_run or settings.dry_run),
    )
    _trade_store.record(trade_record)

    alert = _opportunity_alert(opp, result)
    send_telegram(alert)

    return result


def run_trading_cycle(
    min_ev: Optional[float] = None,
    min_confidence: Optional[float] = None,
    max_hours: Optional[float] = None,
    dry_run: bool = True,
    top_n: int = 10,
    bankroll: float = 1000.0,
) -> ScanResult:
    """
    Full trading cycle: scan → filter → size → execute → alert.
    """
    logger.info(f"Starting trading cycle [dry_run={dry_run}]")

    result = run_scan(
        min_ev=min_ev,
        min_confidence=min_confidence,
        max_hours=max_hours,
        dry_run=dry_run,
    )

    display_opportunities(result)

    already_spent = _trade_store.today_spent()
    actionable = apply_daily_limit(
        result.opportunities[:top_n],
        already_spent_today=already_spent,
        daily_max=settings.daily_max_usdc,
    )

    if not actionable:
        logger.info("No actionable opportunities after daily limit check")
        send_telegram(
            f"🔍 Scan complete — no actionable opportunities\n"
            f"Markets: {result.markets_scanned} | Opps: {len(result.opportunities)}"
        )
        return result

    executed: List[Opportunity] = []
    for opp in actionable:
        logger.info(f"Executing: {opp.summary()}")
        execute_opportunity(opp, dry_run=dry_run)
        executed.append(opp)

    # Daily summary (only send if we executed something)
    if executed:
        send_telegram(_daily_summary_alert(result, executed))

    logger.success(
        f"Trading cycle done: {len(executed)} trades, "
        f"${sum(o.suggested_size_usdc for o in executed):.2f} deployed"
    )
    return result
