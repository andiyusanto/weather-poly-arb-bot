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
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from loguru import logger
from rich.console import Console
from rich.table import Table

from config.settings import TRADES_DB, settings
from src.polymarket_client import (
    fetch_market_resolutions,
    fetch_yes_price_at,
    place_market_order,
)
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

# One momentum lookup per (token, hour): the parallel-shadow control and the
# live trade record the same bucket seconds apart — don't fetch twice.
_momentum_cache: dict = {}

# Buckets whose live order recently FAILED (error/rejected — not slip-aborts):
# no trade row exists for them, so dedup can't block re-entry. Process-local
# by design: a restart retrying once is acceptable; hammering every 30-min
# cycle on a persistent failure (insufficient balance) is not.
_order_failure_cooldown: dict = {}
_FAILURE_COOLDOWN_S = 3 * 3600.0


def _yes_price_24h_ago(yes_token_id: str) -> Optional[float]:
    """Best-effort YES price ~24h ago for momentum logging (None on failure)."""
    hour_bucket = int(time.time() // 3600)
    key = (yes_token_id, hour_bucket)
    if key in _momentum_cache:
        return _momentum_cache[key]
    # Drop entries from previous hours; keep this hour's (shadow + live
    # legs of several opportunities interleave within one cycle).
    for stale in [k for k in _momentum_cache if k[1] != hour_bucket]:
        del _momentum_cache[stale]
    price = fetch_yes_price_at(yes_token_id, int(time.time()) - 24 * 3600)
    if price is not None:  # don't cache failures — the other leg may succeed
        _momentum_cache[key] = price
    return price


def execute_opportunity(
    opp: Opportunity,
    dry_run: bool = True,
    shadow: bool = False,
    quiet: bool = False,
) -> Optional[dict]:
    """
    Place or simulate an order and record to the DB.

    Args:
        opp: The evaluated opportunity to act on.
        dry_run: If True, skip order submission (no DB record for outcomes).
        shadow: If True, skip order submission but record to DB for outcome tracking.
                Mutually exclusive with dry_run=False.
        quiet: If True, skip the per-trade Telegram alert (used by the
               parallel-shadow control recorder to avoid spamming).

    Returns:
        Order result dict with status key.
    """
    # Pick the actual token id we are buying (YES or NO).
    trade_token = opp.trade_token_id or opp.bucket.token_id

    # Momentum lookup BEFORE any order goes out: this is a network call, and
    # the fill→record window must stay I/O-free — a crash/preemption between
    # a live fill and the DB insert leaves an untracked position (the inverse
    # of the phantom-trade bug). Usually a cache hit from the shadow leg.
    yes_price_24h_ago = _yes_price_24h_ago(opp.bucket.token_id)

    if shadow:
        # Shadow: full record, no real order — treated as dry_run at the CLOB layer
        result = {"status": "shadow", "token_id": trade_token,
                  "size_usdc": opp.suggested_size_usdc, "order_id": "SHADOW"}
        mode = "shadow"
    elif dry_run or settings.dry_run:
        result = place_market_order(
            token_id=trade_token,
            side=opp.side.upper(),
            size_usdc=opp.suggested_size_usdc,
            dry_run=True,
        )
        mode = "dry_run"
    else:
        result = place_market_order(
            token_id=trade_token,
            side=opp.side.upper(),
            size_usdc=opp.suggested_size_usdc,
            dry_run=False,
            # Give the SDK executor the quote+prob it needs to run the
            # pre-order slippage abort (Opt 1). If the real-time estimate
            # is materially worse than what the strategy priced against,
            # the order is skipped before any funds move.
            expected_quote=opp.market_price,
            model_prob=opp.model_prob,
            min_ev=settings.pre_order_min_ev,
        )
        mode = "live"

        # A pre-order slippage abort is not a placed trade; don't persist a
        # trade row for it, don't alert as a fill. Log-and-return keeps the
        # trading loop clean.
        if isinstance(result, dict) and result.get("status") == "slip_abort":
            # slip_cents is only populated on the raw-cents branch; the
            # reprice-EV branch reports reprice_ev instead. Compute a display
            # value from whichever we have so the log line is always readable.
            est = result.get("estimate") or 0.0
            slip_c = result.get("slip_cents")
            if slip_c is None and est:
                slip_c = round((est - opp.market_price) * 100, 1)
            rev = result.get("reprice_ev")
            extra = f" reprice_ev={rev*100:.1f}%" if rev is not None else ""
            logger.info(
                f"[SLIP-ABORT] {opp.market.city} {opp.bucket.outcome_label} {opp.side.upper()} "
                f"quote={fmt_pct(opp.market_price)} est={fmt_pct(est)} slip={slip_c:+.1f}¢{extra}"
            )
            return result

        # Any live result that is not an actual fill (error / rejected / FOK
        # killed / unexpected shape) must NOT be persisted: recording it would
        # create a phantom position that later "resolves" and corrupts the
        # live P&L tape (this happened twice: a killed FOK and an
        # insufficient-balance reject were both recorded as wins).
        if not (isinstance(result, dict) and result.get("status") == "placed"):
            kind = result.get("kind") or result.get("status", "?") if isinstance(result, dict) else "?"
            err = result.get("error", "no detail") if isinstance(result, dict) else repr(result)
            logger.error(
                f"[ORDER-FAILED] {opp.market.city} {opp.bucket.outcome_label} {opp.side.upper()} "
                f"size={fmt_usdc(opp.suggested_size_usdc)} kind={kind}: {err} — trade NOT recorded"
            )
            if not quiet:
                send_telegram(
                    f"❌ LIVE order failed ({kind})\n"
                    f"{opp.market.city} {opp.bucket.outcome_label} {opp.side.upper()} "
                    f"size={fmt_usdc(opp.suggested_size_usdc)}\n{err}"
                )
            return result

    # Prefer the actual SDK fill over the pre-order quote so downstream P&L,
    # calibration, and analytics train on ground truth. sdk_executor returns
    # ``fill_price`` (post-fee entry) and ``size_usdc`` (post-fee spend, incl.
    # the min-share bump). Fall back to the pre-order values on shadow / dry
    # runs and on the legacy py-clob path (which doesn't report a fill price).
    recorded_price = opp.market_price
    recorded_size = opp.suggested_size_usdc
    if mode == "live" and isinstance(result, dict) and result.get("status") == "placed":
        fp = result.get("fill_price")
        rs = result.get("size_usdc")
        if fp and fp > 0:
            recorded_price = float(fp)
        if rs and rs > 0:
            recorded_size = float(rs)

    forecast_mean = getattr(opp.forecast, "mean_f", None)
    # Per-model means + combined spread let the bias recorder attribute error
    # to individual models (BMA weights) and the full-EMOS study use spread.
    model_results = getattr(opp.forecast, "model_results", None) or []
    model_means_json = json.dumps(
        {r.model_name: round(r.mean_f, 2) for r in model_results}
    ) if model_results else None
    trade_record = dict(
        yes_price_24h_ago=yes_price_24h_ago,
        model_means=model_means_json,
        ensemble_spread=getattr(opp.forecast, "std_f", None),
        market_id=opp.market.market_id,
        condition_id=opp.bucket.condition_id,
        token_id=trade_token,
        city=opp.market.city,
        market_type=opp.market.market_type.value,
        target_date=opp.market.target_date.isoformat() if opp.market.target_date else "",
        bucket_label=opp.bucket.outcome_label,
        model_prob=opp.model_prob,
        market_price=recorded_price,
        ev=opp.ev,
        confidence=opp.confidence,
        size_usdc=recorded_size,
        side=opp.side,
        forecast_mean=forecast_mean,
        dry_run=int((dry_run or settings.dry_run) and not shadow),
        shadow=int(shadow),
        contrarian=int(getattr(opp, "contrarian", False)),
    )
    _trade_store.record(trade_record)

    if not quiet:
        alert = _opportunity_alert(opp, mode=mode)
        send_telegram(alert)

    logger.info(
        f"[{mode.upper()}] {opp.market.city} {opp.bucket.outcome_label} | "
        f"EV={fmt_pct(opp.ev)} conf={fmt_pct(opp.confidence)} "
        f"quote={fmt_pct(opp.market_price)} fill={fmt_pct(recorded_price)} "
        f"size={fmt_usdc(recorded_size)}"
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

    # Drop buckets we've already bet (any prior cycle/day). The scan re-discovers
    # the same buckets every interval; without this we re-enter the same position
    # each cycle, biasing the shadow sample and over-concentrating live capital.
    # Live/dry and shadow use separate dedup namespaces: parallel-shadow rows
    # must never block a real trade on the same bucket (and vice versa).
    traded = _trade_store.traded_bucket_keys(shadow=shadow if mode != "live" else False)
    def _key(opp: Opportunity) -> tuple:
        td = opp.market.target_date.isoformat() if opp.market.target_date else ""
        return (
            opp.market.city or "",
            td,
            opp.bucket.outcome_label or "",
            (opp.side or "yes").lower(),
            int(getattr(opp, "contrarian", False)),
        )
    fresh = [o for o in result.opportunities if _key(o) not in traded]
    n_dup = len(result.opportunities) - len(fresh)
    if n_dup:
        logger.info(f"Skipped {n_dup} already-traded buckets (one bet per bucket)")

    # Failed live orders leave NO trade row (by design — no phantoms), which
    # means the dedup above cannot see them: without a cooldown a persistent
    # failure (e.g. insufficient balance) re-orders and re-alerts every cycle.
    # Slip-aborts are exempt — the book can genuinely improve next cycle.
    now_ts = time.time()
    cooled = [o for o in fresh
              if now_ts - _order_failure_cooldown.get(_key(o), 0.0) < _FAILURE_COOLDOWN_S]
    if cooled:
        fresh = [o for o in fresh if o not in cooled]
        logger.info(f"Skipped {len(cooled)} buckets in order-failure cooldown "
                    f"({_FAILURE_COOLDOWN_S / 3600:.0f}h)")

    # Per-city daily cap — prevents concentration on a single city when the
    # scanner surfaces multiple correlated buckets in one pass. Applied before
    # the USDC quota so refused trades don't eat the daily budget.
    city_cap = int(settings.max_trades_per_city_per_day or 0)
    if city_cap > 0:
        already_by_city = _trade_store.trades_today_by_city()
        session_by_city: dict[str, int] = {}
        after_city_filter: List[Opportunity] = []
        for opp in fresh:
            city = opp.market.city or ""
            already = already_by_city.get(city, 0) + session_by_city.get(city, 0)
            if already >= city_cap:
                logger.info(
                    f"  [{opp.market.market_type.value}] {city} {opp.bucket.outcome_label} "
                    f"{opp.side.upper()}: per-city cap {city_cap} reached — skipped"
                )
                continue
            session_by_city[city] = session_by_city.get(city, 0) + 1
            after_city_filter.append(opp)
        n_capped = len(fresh) - len(after_city_filter)
        if n_capped:
            logger.info(f"Per-city cap dropped {n_capped} opportunities")
        fresh = after_city_filter

    # Parallel shadow control group: in live mode, record EVERY qualified
    # opportunity as a shadow row too — including those later dropped by the
    # city cap or daily budget. The shadow tape then measures the model at
    # quoted prices while live measures model + execution; the gap is the true
    # slippage/fee cost. Separate dedup namespace (shadow=True). Runs before
    # the daily-limit early-return so no-trade cycles still build the control.
    if mode == "live" and settings.parallel_shadow:
        shadow_seen = _trade_store.traded_bucket_keys(shadow=True)
        n_shadowed = 0
        for opp in result.opportunities:
            if _key(opp) in shadow_seen:
                continue
            try:
                execute_opportunity(opp, dry_run=False, shadow=True, quiet=True)
                n_shadowed += 1
            except Exception as e:
                logger.warning(f"parallel-shadow record failed for {opp.summary()}: {e}")
        if n_shadowed:
            logger.info(f"Parallel shadow: recorded {n_shadowed} control trades")

    already_spent = _trade_store.today_spent()
    actionable = apply_daily_limit(
        fresh[:top_n],
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
        res = execute_opportunity(opp, dry_run=dry_run, shadow=shadow)
        status = res.get("status") if isinstance(res, dict) else None
        if status in ("placed", "shadow", "dry_run"):
            executed.append(opp)
        elif status != "slip_abort":
            # error / rejected / unknown: no trade row exists, so start the
            # re-entry cooldown; counting it as "deployed" would make the
            # Telegram summary claim capital that never left the wallet.
            _order_failure_cooldown[_key(opp)] = time.time()

    if executed:
        send_telegram(_daily_summary_alert(result, executed, mode=mode))

    logger.success(
        f"Trading cycle done [{mode}]: {len(executed)} trades, "
        f"${sum(o.suggested_size_usdc for o in executed):.2f} deployed"
    )
    return result


# ── Shadow resolution ─────────────────────────────────────────────────────────

def _compute_pnl(
    size_usdc: float, market_price: float, outcome: str, side: str = "yes"
) -> float:
    """
    Compute realised P&L for a binary BUY position.

    side="yes" — we bought YES at market_price; win on outcome=yes.
    side="no"  — we bought NO  at market_price; win on outcome=no.

    Profit on the winning side: size_usdc * (1/market_price - 1).
    Loss on the losing side:    -size_usdc.
    """
    side = (side or "yes").lower()
    won = (side == "yes" and outcome == "yes") or (side == "no" and outcome == "no")
    if won:
        if market_price and market_price > 0:
            return round(size_usdc * (1.0 / market_price - 1.0), 4)
        return 0.0
    return -round(size_usdc, 4)


def resolve_open_trades(verbose: bool = False) -> List[dict]:
    """
    Resolve every open trade (shadow OR live) against the CLOB and update the DB.

    Live trades settle on-chain via Polymarket's auto_redeem_operator
    automatically; this function just brings the local DB into sync with that
    ground truth so analytics dashboards (``side-pnl``, ``slice-dash``,
    ``contrarian-pnl``) can see live outcomes alongside shadow ones.

    Returns:
        List of newly resolved trade dicts (with outcome and pnl filled in).
    """
    open_trades = _trade_store.open_unresolved_trades()
    if not open_trades:
        logger.info("No open trades to resolve (shadow + live).")
        return []

    # Many trades bet the same bucket, so resolutions are deduped by conditionId
    # and fetched concurrently over a keep-alive client (CLOB is slow per call).
    def _cid(t: dict) -> str:
        # New rows store the conditionId in `condition_id`; legacy rows in `market_id`.
        return t.get("condition_id") or t.get("market_id", "")

    cond_ids = [_cid(t) for t in open_trades]
    n_unique = len({c for c in cond_ids if c})
    n_live = sum(1 for t in open_trades if not t.get("shadow") and not t.get("dry_run"))
    n_shadow = len(open_trades) - n_live
    logger.info(
        f"Checking resolution for {len(open_trades)} open trades "
        f"({n_shadow} shadow + {n_live} live, {n_unique} unique markets)…"
    )
    resolutions = fetch_market_resolutions(cond_ids)

    newly_resolved: List[dict] = []
    still_pending = 0

    for trade in open_trades:
        outcome = resolutions.get(_cid(trade))

        if outcome is None:
            still_pending += 1
            if verbose:
                logger.debug(f"Trade #{trade['id']} still open ({trade.get('city')} {trade.get('bucket_label')})")
            continue

        side = (trade.get("side") or "yes").lower()
        pnl = _compute_pnl(
            size_usdc=float(trade.get("size_usdc") or 0.0),
            market_price=float(trade.get("market_price") or 0.5),
            outcome=outcome,
            side=side,
        )
        _trade_store.update_outcome(trade["id"], outcome=outcome, pnl=pnl)
        trade["outcome"] = outcome
        trade["pnl"] = pnl
        newly_resolved.append(trade)

        # Record the bias so future forecasts can apply rolling correction.
        try:
            from src.bias_recorder import record_bias_for_resolved_trade
            record_bias_for_resolved_trade(trade)
        except Exception as e:
            logger.debug(f"bias record failed for trade #{trade['id']}: {e}")

        sign = "✅" if (side == "yes" and outcome == "yes") or (side == "no" and outcome == "no") else "❌"
        # Tag the log line with the actual trade type so live trades stand out.
        is_live = not trade.get("shadow") and not trade.get("dry_run")
        kind = "LIVE" if is_live else "Shadow"
        logger.info(
            f"{sign} {kind} #{trade['id']} {trade.get('city')} {trade.get('bucket_label')} "
            f"[{side.upper()}] → {outcome.upper()} | P&L={fmt_usdc(pnl)}"
        )

        # Per-resolution Telegram — live trades only, gated by settings flag.
        # Keeps capital drift visible without opening the DB.
        if is_live and settings.notify_every_resolution and settings.has_telegram:
            try:
                send_telegram(_per_resolution_alert(trade, side, outcome, pnl))
            except Exception as e:
                logger.debug(f"per-resolution telegram failed: {e}")

    # Refresh empirical calibration after a batch of resolutions.
    if newly_resolved:
        try:
            from src.calibration import rebuild_calibration
            rebuild_calibration()
        except Exception as e:
            logger.debug(f"calibration rebuild failed: {e}")

    # Consecutive-loss Telegram alert. Only fires when THIS batch just added a
    # fresh live loss AND the running live-loss streak has reached the
    # configured threshold — the "fresh loss" gate is what prevents cron-repeat
    # spam (streak stays high across many empty cron runs until a win lands).
    threshold = int(settings.consecutive_loss_alert or 0)
    if threshold > 0 and newly_resolved:
        new_live_loss = any(
            (t.get("pnl") or 0) < 0
            and not t.get("shadow")
            and not t.get("dry_run")
            for t in newly_resolved
        )
        if new_live_loss:
            streak, last_losses = _live_loss_streak()
            if streak >= threshold:
                send_telegram(_consecutive_loss_alert(streak, threshold, last_losses))
                logger.warning(
                    f"⚠️ Live loss streak = {streak} (≥ {threshold}) — Telegram alert sent"
                )

    logger.success(
        f"Resolved {len(newly_resolved)} trades this run "
        f"({still_pending} still pending finalization)."
    )
    return newly_resolved


def _live_loss_streak() -> tuple[int, list[dict]]:
    """Count consecutive losses in the most-recent resolved LIVE trades.

    Walks trades newest → oldest and counts contiguous losses until the first
    win (or a non-live trade) breaks the run. Returns the streak length and
    the streak's trades themselves for the alert body.
    """
    streak = 0
    losses: list[dict] = []
    for t in _trade_store.recent_trades(n=50):
        if t.get("shadow") or t.get("dry_run"):
            continue
        if t.get("outcome") is None:
            # Still open — doesn't break the streak but doesn't extend it either.
            continue
        if (t.get("pnl") or 0) < 0:
            streak += 1
            losses.append(t)
        else:
            break
    return streak, losses


def _consecutive_loss_alert(streak: int, threshold: int, losses: list[dict]) -> str:
    """Compose a Telegram body listing the streak and the individual losers."""
    lines = [
        f"⚠️ *LIVE LOSS STREAK: {streak}* (threshold {threshold})",
        "",
        "Most recent losses (newest first):",
    ]
    for t in losses[:5]:
        lines.append(
            f"  • #{t['id']} {t.get('city','?')} {t.get('bucket_label','?')} "
            f"{(t.get('side') or '').upper()} → {t.get('outcome','?').upper()}  "
            f"P&L=${(t.get('pnl') or 0):+.2f}"
        )
    if streak > 5:
        lines.append(f"  … and {streak - 5} more.")
    lines.append("")
    lines.append("Consider pausing the bot for review.")
    return "\n".join(lines)


def _per_resolution_alert(trade: dict, side: str, outcome: str, pnl: float) -> str:
    """Short Telegram body for one resolved live trade with running live P&L.

    Running total lets the user eyeball capital drift without opening the DB.
    """
    is_win = (side == "yes" and outcome == "yes") or (side == "no" and outcome == "no")
    header = "✅ *WIN*" if is_win else "❌ *LOSS*"

    running = 0.0
    n_resolved = 0
    for t in _trade_store.recent_trades(n=200):
        if t.get("shadow") or t.get("dry_run"):
            continue
        if t.get("outcome") is None:
            continue
        running += float(t.get("pnl") or 0.0)
        n_resolved += 1

    size = float(trade.get("size_usdc") or 0.0)
    ask = float(trade.get("market_price") or 0.0)
    return (
        f"{header}  #{trade['id']}  {trade.get('city','?')} {trade.get('bucket_label','?')}\n"
        f"side={side.upper()} → outcome={outcome.upper()}\n"
        f"stake=${size:.2f} @ ask={ask:.3f}  P&L=${pnl:+.2f}\n"
        f"running live P&L (n={n_resolved}) = ${running:+.2f}"
    )


# Backward-compat alias so external scripts that import the old name still work.
# Old call: ``from src.trader import resolve_shadow_trades``
resolve_shadow_trades = resolve_open_trades


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

    def _won(t: dict) -> bool:
        side = (t.get("side") or "yes").lower()
        outcome = (t.get("outcome") or "").lower()
        return (side == "yes" and outcome == "yes") or (side == "no" and outcome == "no")

    # Per-city breakdown
    by_city: Dict[str, dict] = defaultdict(lambda: dict(n=0, wins=0, pnl=0.0, resolved=0))
    for t in stats["trades"]:
        city = t.get("city") or "unknown"
        by_city[city]["n"] += 1
        if t.get("outcome") is not None:
            by_city[city]["resolved"] += 1
            by_city[city]["pnl"] += t.get("pnl") or 0.0
            if _won(t):
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
