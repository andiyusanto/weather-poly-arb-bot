"""
Trader — executes opportunities, manages daily limits, sends Telegram alerts,
records all trades to SQLite.

Modes
-----
dry_run=True   : log what would happen, no order submitted, not tracked for outcome.
shadow=True    : log, no order submitted, BUT record to DB and later resolve against
                 actual market outcomes to validate the edge before going live.
live           : dry_run=False, shadow=False — real orders placed via CLOB.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from loguru import logger
from rich.console import Console
from rich.table import Table

from config.settings import TRADES_DB, settings
from src.polymarket_client import fetch_market_resolution, place_market_order
from src.scanner import ScanResult, display_opportunities, run_scan
from src.strategy import Opportunity, apply_daily_limit
from src.utils import TradeStore, fmt_pct, fmt_usdc, now_utc

_trade_store = TradeStore(TRADES_DB)
_console = Console()


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


def _opportunity_alert(opp: Opportunity, mode: str = "dry_run") -> str:
    """
    Format a Telegram alert for a single opportunity.

    Args:
        opp: The evaluated opportunity.
        mode: 'dry_run' | 'shadow' | 'live'
    """
    tag = {"dry_run": "🔵 DRY RUN", "shadow": "🟡 SHADOW", "live": "🟢 TRADE EXECUTED"}.get(mode, "🔵 DRY RUN")
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


def _daily_summary_alert(result: ScanResult, executed: List[Opportunity], mode: str) -> str:
    total_size = sum(o.suggested_size_usdc for o in executed)
    avg_ev = sum(o.ev for o in executed) / max(len(executed), 1)
    tag = {"dry_run": "🔵 DRY RUN", "shadow": "🟡 SHADOW", "live": "🟢 LIVE"}.get(mode, "🔵")
    lines = [
        f"📅 <b>Daily Scan Summary</b> {tag} — {now_utc().strftime('%Y-%m-%d %H:%M UTC')}",
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

def execute_opportunity(
    opp: Opportunity,
    dry_run: bool = True,
    shadow: bool = False,
) -> Optional[dict]:
    """
    Place or simulate an order and record to the DB.

    Args:
        opp: The evaluated opportunity to act on.
        dry_run: If True, skip order submission (no DB record for outcomes).
        shadow: If True, skip order submission but record to DB for outcome tracking.
                Mutually exclusive with dry_run=False.

    Returns:
        Order result dict with status key.
    """
    if shadow:
        # Shadow: full record, no real order — treated as dry_run at the CLOB layer
        result = {"status": "shadow", "token_id": opp.bucket.token_id,
                  "size_usdc": opp.suggested_size_usdc, "order_id": "SHADOW"}
        mode = "shadow"
    elif dry_run or settings.dry_run:
        result = place_market_order(
            token_id=opp.bucket.token_id,
            side="BUY",
            size_usdc=opp.suggested_size_usdc,
            dry_run=True,
        )
        mode = "dry_run"
    else:
        result = place_market_order(
            token_id=opp.bucket.token_id,
            side="BUY",
            size_usdc=opp.suggested_size_usdc,
            dry_run=False,
        )
        mode = "live"

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
        dry_run=int((dry_run or settings.dry_run) and not shadow),
        shadow=int(shadow),
    )
    _trade_store.record(trade_record)

    alert = _opportunity_alert(opp, mode=mode)
    send_telegram(alert)

    logger.info(
        f"[{mode.upper()}] {opp.market.city} {opp.bucket.outcome_label} | "
        f"EV={fmt_pct(opp.ev)} conf={fmt_pct(opp.confidence)} size={fmt_usdc(opp.suggested_size_usdc)}"
    )
    return result


def run_trading_cycle(
    min_ev: Optional[float] = None,
    min_confidence: Optional[float] = None,
    max_hours: Optional[float] = None,
    dry_run: bool = True,
    shadow: bool = False,
    top_n: int = 10,
    bankroll: float = 1000.0,
) -> ScanResult:
    """
    Full trading cycle: scan → filter → size → execute → alert.

    Args:
        dry_run: Log only, no DB record for outcomes.
        shadow: Record to DB for outcome tracking, no real order.
    """
    mode = "shadow" if shadow else ("dry_run" if (dry_run or settings.dry_run) else "live")
    logger.info(f"Starting trading cycle [mode={mode}]")

    result = run_scan(
        min_ev=min_ev,
        min_confidence=min_confidence,
        max_hours=max_hours,
        dry_run=dry_run or shadow,
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
        logger.info(f"Executing [{mode}]: {opp.summary()}")
        execute_opportunity(opp, dry_run=dry_run, shadow=shadow)
        executed.append(opp)

    if executed:
        send_telegram(_daily_summary_alert(result, executed, mode=mode))

    logger.success(
        f"Trading cycle done [{mode}]: {len(executed)} trades, "
        f"${sum(o.suggested_size_usdc for o in executed):.2f} deployed"
    )
    return result


# ── Shadow resolution ─────────────────────────────────────────────────────────

def _compute_pnl(size_usdc: float, market_price: float, outcome: str) -> float:
    """
    Compute realised P&L for a BUY-YES position.

    On YES: we paid market_price per share, collect $1.00 per share.
            profit = size_usdc * (1.0 / market_price - 1.0)
    On NO:  lose the entire stake.
            profit = -size_usdc
    """
    if outcome == "yes":
        if market_price and market_price > 0:
            return round(size_usdc * (1.0 / market_price - 1.0), 4)
        return 0.0
    return -round(size_usdc, 4)


def resolve_shadow_trades(verbose: bool = False) -> List[dict]:
    """
    Check Gamma API for resolution of every open shadow trade and update the DB.

    Returns:
        List of newly resolved trade dicts (with outcome and pnl filled in).
    """
    open_trades = _trade_store.open_shadow_trades()
    if not open_trades:
        logger.info("No open shadow trades to resolve.")
        return []

    logger.info(f"Checking resolution for {len(open_trades)} open shadow trades…")
    newly_resolved: List[dict] = []

    for trade in open_trades:
        condition_id = trade.get("market_id", "")
        outcome = fetch_market_resolution(condition_id)

        if outcome is None:
            if verbose:
                logger.debug(f"Trade #{trade['id']} still open ({trade.get('city')} {trade.get('bucket_label')})")
            continue

        pnl = _compute_pnl(
            size_usdc=float(trade.get("size_usdc") or 0.0),
            market_price=float(trade.get("market_price") or 0.5),
            outcome=outcome,
        )
        _trade_store.update_outcome(trade["id"], outcome=outcome, pnl=pnl)
        trade["outcome"] = outcome
        trade["pnl"] = pnl
        newly_resolved.append(trade)

        sign = "✅" if outcome == "yes" else "❌"
        logger.info(
            f"{sign} Shadow #{trade['id']} {trade.get('city')} {trade.get('bucket_label')} "
            f"→ {outcome.upper()} | P&L={fmt_usdc(pnl)}"
        )

    logger.success(f"Resolved {len(newly_resolved)} shadow trades this run.")
    return newly_resolved


# ── Shadow P&L report ─────────────────────────────────────────────────────────

def shadow_performance_report() -> None:
    """Print a rich summary table of all shadow trades and their outcomes."""
    stats = _trade_store.shadow_stats()

    if stats["total"] == 0:
        _console.print("[yellow]No shadow trades recorded yet. Run: python run.py trade --shadow[/yellow]")
        return

    # Header stats
    pnl_color = "green" if stats["total_pnl"] >= 0 else "red"
    _console.print("\n[bold cyan]Shadow Mode Performance Report[/bold cyan]")
    _console.print(f"  Total trades   : [bold]{stats['total']}[/bold]  (resolved={stats['resolved']}  open={stats['open']})")
    _console.print(f"  Win rate       : [bold]{fmt_pct(stats['win_rate'])}[/bold]  ({stats['wins']}/{stats['resolved']} resolved YES)")
    _console.print(f"  Total P&L      : [{pnl_color}][bold]{fmt_usdc(stats['total_pnl'])}[/bold][/{pnl_color}]")
    _console.print(f"  Avg EV         : {fmt_pct(stats['avg_ev'])}")
    _console.print(f"  Avg Confidence : {fmt_pct(stats['avg_conf'])}\n")

    # Per-city breakdown
    by_city: Dict[str, dict] = defaultdict(lambda: dict(n=0, wins=0, pnl=0.0, resolved=0))
    for t in stats["trades"]:
        city = t.get("city") or "unknown"
        by_city[city]["n"] += 1
        if t.get("outcome") is not None:
            by_city[city]["resolved"] += 1
            by_city[city]["pnl"] += t.get("pnl") or 0.0
            if (t.get("outcome") or "").lower() == "yes":
                by_city[city]["wins"] += 1

    table = Table(title="By City", header_style="bold magenta", border_style="dim")
    for col in ["City", "Trades", "Resolved", "Win Rate", "P&L (USDC)"]:
        table.add_column(col, no_wrap=True)

    for city, d in sorted(by_city.items(), key=lambda x: -x[1]["pnl"]):
        wr = d["wins"] / max(d["resolved"], 1)
        pnl_str = fmt_usdc(d["pnl"])
        pnl_styled = f"[green]{pnl_str}[/green]" if d["pnl"] >= 0 else f"[red]{pnl_str}[/red]"
        table.add_row(city, str(d["n"]), str(d["resolved"]), fmt_pct(wr), pnl_styled)

    _console.print(table)

    # Recent trades detail table
    detail = Table(title="Recent Shadow Trades", header_style="bold blue", border_style="dim")
    for col in ["#", "City", "Bucket", "Model", "Ask", "EV", "Size", "Outcome", "P&L"]:
        detail.add_column(col, no_wrap=True)

    for t in sorted(stats["trades"], key=lambda x: -x["id"])[:20]:
        outcome = t.get("outcome")
        out_str = (
            "[green]YES[/green]" if outcome == "yes"
            else "[red]NO[/red]" if outcome == "no"
            else "[dim]open[/dim]"
        )
        pnl = t.get("pnl")
        pnl_str = (
            f"[green]{fmt_usdc(pnl)}[/green]" if pnl and pnl >= 0
            else f"[red]{fmt_usdc(pnl)}[/red]" if pnl
            else "-"
        )
        detail.add_row(
            str(t["id"]),
            t.get("city", ""),
            t.get("bucket_label", ""),
            fmt_pct(t.get("model_prob") or 0.0),
            fmt_pct(t.get("market_price") or 0.0),
            fmt_pct(t.get("ev") or 0.0),
            fmt_usdc(t.get("size_usdc") or 0.0),
            out_str,
            pnl_str,
        )

    _console.print(detail)
