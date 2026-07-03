"""
Strategy — EV calculation, Kelly sizing, opportunity filtering.
Works uniformly across temperature, precipitation, and snowfall markets
via duck-typed forecast objects that all expose `all_bucket_probabilities()`.

Key formulas:
  EV (YES bet) = model_prob * (1/ask - 1) - (1 - model_prob)
               = model_prob / ask - 1          [per $ risked]
  Kelly f* = (p * b - q) / b   where b = 1/ask - 1, q = 1-p
  Fractional Kelly = f* × kelly_fraction (default 0.25)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from loguru import logger

from config.settings import settings
from src.forecast import AnyForecast, EnsembleForecast, PrecipForecast, SnowForecast
from src.polymarket_client import MarketType, WeatherBucket, WeatherMarket
from src.utils import fmt_pct

# Keep old import alias so any direct `from src.strategy import TemperatureBucket` still works
TemperatureBucket = WeatherBucket


@dataclass
class Opportunity:
    market: WeatherMarket
    bucket: WeatherBucket
    forecast: AnyForecast
    model_prob: float           # model probability for the side we are taking (yes or no)
    market_price: float         # ask price for the side we are taking (0–1)
    ev: float
    confidence: float
    kelly_fraction: float
    suggested_size_usdc: float
    side: str = "yes"           # "yes" or "no"
    hours_to_resolution: Optional[float] = None
    # The token that will actually be purchased (yes or no token, depending on side).
    trade_token_id: str = ""
    # True if this NO bet was flipped from YES by the contrarian-inversion flag
    # (Option F). Lets analytics distinguish inverted trades from natural NO picks.
    contrarian: bool = False

    @property
    def edge_pct(self) -> str:
        return fmt_pct(self.ev)

    @property
    def is_high_conviction(self) -> bool:
        return self.ev >= 0.35 and self.confidence >= 0.70

    def summary(self) -> str:
        mtype = self.market.market_type.emoji
        return (
            f"{mtype} {self.market.city} {self.market.target_date} "
            f"[{self.bucket.outcome_label}|{self.side.upper()}]: "
            f"model={fmt_pct(self.model_prob)} mkt={fmt_pct(self.market_price)} "
            f"EV={fmt_pct(self.ev)} conf={fmt_pct(self.confidence)} "
            f"size=${self.suggested_size_usdc:.2f}"
        )


# ── EV / Kelly core ───────────────────────────────────────────────────────────

_BUCKET_NUM_RE = None


def _bucket_center_c(label: str) -> Optional[float]:
    """Extract the first numeric value from a Polymarket temperature bucket label.

    Handles the two live shapes we see: exact-value buckets like ``"29°C"`` and
    open-ended tail buckets like ``"39°C or higher"``. Returns None if the
    label doesn't contain a parseable number (defensive — we skip the gate
    rather than crash on a new market type).
    """
    global _BUCKET_NUM_RE
    if _BUCKET_NUM_RE is None:
        import re
        _BUCKET_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
    if not label:
        return None
    m = _BUCKET_NUM_RE.search(label)
    return float(m.group(1)) if m else None


def compute_ev(model_prob: float, ask_price: float) -> float:
    if ask_price <= 0 or ask_price >= 1:
        return -1.0
    b = (1.0 / ask_price) - 1.0
    q = 1.0 - model_prob
    return model_prob * b - q


def kelly_fraction(model_prob: float, ask_price: float) -> float:
    if ask_price <= 0 or ask_price >= 1:
        return 0.0
    b = (1.0 / ask_price) - 1.0
    q = 1.0 - model_prob
    if b <= 0:
        return 0.0
    return max(0.0, (model_prob * b - q) / b)


def suggested_position_size(
    model_prob: float,
    ask_price: float,
    bankroll: float,
    kelly_mult: float,
    max_usdc: float,
) -> float:
    kf = kelly_fraction(model_prob, ask_price)
    raw = kf * kelly_mult * bankroll
    return float(np.clip(raw, 0.0, max_usdc))


# ── Bucket probability normalisation ─────────────────────────────────────────

def normalize_bucket_probs(
    forecast: AnyForecast,
    buckets: List[WeatherBucket],
) -> Dict[str, float]:
    """
    Map token_ids → normalised model probabilities.
    Works for any forecast type via duck-typed `all_bucket_probabilities()`.
    """
    bucket_tuples = [(b.lower, b.upper) for b in buckets]
    probs = forecast.all_bucket_probabilities(bucket_tuples)
    return {b.token_id: probs.get((b.lower, b.upper), 0.0) for b in buckets}


# ── Time-decay confidence adjustment ─────────────────────────────────────────

def time_confidence_adjustment(hours_to_resolution: Optional[float]) -> float:
    if hours_to_resolution is None:
        return 0.85
    if hours_to_resolution <= 12:
        return 1.0
    if hours_to_resolution <= 24:
        return 0.95
    if hours_to_resolution <= 48:
        return 0.88
    return 0.75


# ── Precip/snow specific minimum confidence ───────────────────────────────────

def _effective_min_confidence(
    market_type: MarketType, base_min_confidence: float
) -> float:
    """
    Precipitation and snowfall markets reward high-confidence trades even more.
    Mispricings are larger when the ensemble strongly agrees, so we can afford
    a slightly lower confidence floor — but never below 0.45.
    """
    if market_type in (MarketType.PRECIPITATION, MarketType.SNOWFALL):
        # Precip/snow have higher natural mispricings; accept marginally lower conf
        return max(0.45, base_min_confidence - 0.05)
    return base_min_confidence


# ── Main evaluation ───────────────────────────────────────────────────────────

def evaluate_market(
    market: WeatherMarket,
    forecast: AnyForecast,
    min_ev: float = 0.20,
    min_confidence: float = 0.55,
    bankroll: float = 1000.0,
    kelly_mult: Optional[float] = None,
    max_usdc: Optional[float] = None,
) -> List[Opportunity]:
    """
    Evaluate every bucket in an event for both YES and NO trading sides.
    Returns at most ONE opportunity per event (the highest-EV bucket/side),
    because buckets within an event are mutually exclusive — we cannot
    independently allocate Kelly to multiple buckets of the same event.
    """
    kelly_mult = kelly_mult if kelly_mult is not None else settings.kelly_fraction
    max_usdc = max_usdc if max_usdc is not None else settings.max_trade_usdc

    hours_left = None
    if market.resolution_datetime:
        from src.utils import hours_until
        hours_left = hours_until(market.resolution_datetime)
        if hours_left < 0:
            return []

    time_adj = time_confidence_adjustment(hours_left)
    adj_confidence = forecast.confidence * time_adj

    # Apply per-city skill weighting if we have history (defaults to 1.0).
    from src.calibration import city_skill_factor, calibrate_probability
    adj_confidence *= city_skill_factor(market.city, market.market_type.value)

    effective_min_conf = _effective_min_confidence(market.market_type, min_confidence)
    if adj_confidence < effective_min_conf:
        logger.debug(
            f"{market.market_type.value} {market.city} {market.target_date}: "
            f"conf {adj_confidence:.2f} < {effective_min_conf:.2f}, skip"
        )
        return []

    # Probability normalization is now meaningful: the bucket set covers the
    # full mutually-exclusive outcome space for this event.
    probs = normalize_bucket_probs(forecast, market.buckets)

    min_ask = settings.min_ask_price
    max_ask = settings.max_ask_price
    ev_cap = settings.max_ev_cap

    candidate_opps: List[Opportunity] = []
    for bucket in market.buckets:
        raw_p_yes = probs.get(bucket.token_id, 0.0)
        if raw_p_yes < 0.001 or raw_p_yes > 0.999:
            # Degenerate prediction — likely a normalization artifact, skip.
            continue

        # Apply empirical calibration learned from shadow history.
        p_yes = calibrate_probability(raw_p_yes, market.market_type.value)
        p_no = 1.0 - p_yes

        # Slippage tax: FOK market orders empirically fill ~1–2¢ above the
        # top-of-book quote. Inflating the ask here filters marginal edges
        # whose true post-fill EV is negative, without touching the winning
        # tail where the model has genuine multi-cent edge.
        slip = max(0.0, settings.slippage_tax)

        # ── YES side ──
        ask_yes = bucket.best_ask
        ev_yes = -1.0
        if min_ask <= ask_yes <= max_ask:
            ev_yes = compute_ev(p_yes, min(ask_yes + slip, max_ask))

        # ── NO side ──
        ask_no = bucket.no_ask
        ev_no = -1.0
        if min_ask <= ask_no <= max_ask:
            ev_no = compute_ev(p_no, min(ask_no + slip, max_ask))

        # Pick the better side, if any.
        if ev_yes >= ev_no:
            side, side_prob, side_ask, side_ev = "yes", p_yes, ask_yes, ev_yes
            trade_token = bucket.token_id
        else:
            side, side_prob, side_ask, side_ev = "no", p_no, ask_no, ev_no
            trade_token = bucket.no_token_id or bucket.token_id

        if side_ev < min_ev:
            continue

        # Filter A — NO-side ask ceiling. Even at 67% observed win-rate,
        # buying NO above ~80¢ pays 15¢ or less on win vs 80¢+ downside;
        # aggregate P&L on the allowlist shadow tape (n=18) was −$0.25/trade.
        if side == "no" and side_ask > settings.max_no_ask:
            logger.debug(
                f"  [{market.market_type.value}] {market.city} {bucket.outcome_label} NO: "
                f"ask {side_ask:.2f} > cap {settings.max_no_ask:.2f} — skipped"
            )
            continue

        # Mode-bucket NO gate: the bucket whose center sits closest to the
        # forecast mean is exactly where the model's ~0.60 probability is
        # empirically a coin flip (true wr ≈ 40%). Betting NO there loses
        # asymmetrically because we're wrong on the most-likely outcome.
        # Live-trade retro (2026-06-29 → 07-01): filter would have blocked
        # 3 of 5 realized losses (Wuhan 29°C, Moscow 26°C, Manila 29°C).
        # Only apply to temperature — precip/snow already zero-inflated.
        if (
            side == "no"
            and market.market_type.value == "temperature"
            and settings.mode_bucket_no_min_prob > 0
        ):
            bucket_c = _bucket_center_c(bucket.outcome_label)
            if bucket_c is not None:
                # forecast.mean_f is Fahrenheit for temperature; convert to °C.
                fm_c = (float(forecast.mean_f) - 32.0) * 5.0 / 9.0
                if (
                    abs(bucket_c - fm_c) < settings.mode_bucket_c_radius
                    and side_prob < settings.mode_bucket_no_min_prob
                ):
                    logger.info(
                        f"  [temperature] {market.city} {bucket.outcome_label} NO: "
                        f"mode-bucket (center {bucket_c:.1f}°C, forecast {fm_c:.1f}°C) "
                        f"prob {side_prob:.2f} < {settings.mode_bucket_no_min_prob:.2f} — skipped"
                    )
                    continue

        # Confidence floor on the side we're actually betting. EV-only triggering
        # buys cheap longshots whose tiny probabilities are tail-noise (and where
        # the model is empirically anti-predictive). Require the bet to be on an
        # outcome we consider reasonably likely.
        if side_prob < settings.min_model_prob:
            logger.debug(
                f"  [{market.market_type.value}] {bucket.outcome_label} {side.upper()}: "
                f"prob {side_prob:.2f} < min {settings.min_model_prob:.2f} — skipped"
            )
            continue

        if side_ev > ev_cap:
            # Suspicious EV — usually means market is near-resolved or priced
            # in a way the model can't actually evaluate. Skip rather than trade.
            logger.debug(
                f"  [{market.market_type.value}] {bucket.outcome_label} {side.upper()}: "
                f"EV={side_ev:.1%} > cap {ev_cap:.0%} — skipped (likely stale/illiquid)"
            )
            continue

        size = suggested_position_size(side_prob, side_ask, bankroll, kelly_mult, max_usdc)
        kf = kelly_fraction(side_prob, side_ask)

        candidate_opps.append(Opportunity(
            market=market,
            bucket=bucket,
            forecast=forecast,
            model_prob=side_prob,
            market_price=side_ask,
            ev=side_ev,
            confidence=adj_confidence,
            kelly_fraction=kf,
            suggested_size_usdc=size,
            side=side,
            trade_token_id=trade_token,
            hours_to_resolution=hours_left,
            contrarian=False,
        ))

    if not candidate_opps:
        return []

    # Joint Kelly: buckets within an event are mutually exclusive, so allocate
    # to a SINGLE position per event — the highest-EV side/bucket.
    candidate_opps.sort(key=lambda o: o.ev, reverse=True)
    best = candidate_opps[0]

    # ── Contrarian YES inversion (Option F) ─────────────────────────────────
    # Applied AFTER the EV sort: if the bot's chosen opportunity is YES, flip
    # it to NO on the same bucket. Doing this before the sort would demote the
    # flipped opportunity (its NO-side ev is strongly negative when YES-side ev
    # is positive) and let an unrelated bucket's natural NO win the event —
    # silently masking the inversion. The post-sort placement preserves the
    # principle: pick the strongest bucket per event using the model's own
    # logic, then mirror its side. Natural NO picks are unaffected.
    #
    # Sizing: the contrarian bet has negative model-EV by construction (we are
    # explicitly betting against our own probability), so Kelly would size it at
    # $0 and apply_daily_limit would drop it. The empirical edge is in the
    # MIRROR direction: when the model says YES, we believe (from the -5.5%
    # win-rate gap) that NO is the better bet at the same dollar conviction.
    # We therefore preserve the YES-side suggested size — same conviction,
    # opposite direction. The recorded `ev` is the actual bet's ev (negative),
    # which is honest accounting; the empirical edge isn't visible in any single
    # bet's ev, only in the realized win rate across the cohort.
    if settings.contrarian_yes_inversion and best.side == "yes":
        b = best.bucket
        new_ask = b.no_ask
        new_token = b.no_token_id

        # Defensive: skip the flip if the NO side of this bucket is unusable.
        # Falling back to the YES token at the NO ask would silently mislabel a
        # YES purchase as a NO bet — wrong settlement, wrong outcome resolution.
        if not new_token:
            logger.warning(
                f"  CONTRARIAN: cannot invert {b.outcome_label} — no_token_id missing; "
                f"skipping bet entirely (would mislabel YES token as NO)"
            )
            return []
        if new_ask <= 0.0 or new_ask >= 1.0:
            logger.warning(
                f"  CONTRARIAN: cannot invert {b.outcome_label} — no_ask={new_ask} "
                f"out of range; no quoted NO market"
            )
            return []

        new_prob = 1.0 - best.model_prob
        new_ev = compute_ev(new_prob, new_ask)
        logger.info(
            f"  CONTRARIAN: YES→NO on {b.outcome_label} "
            f"(yes_prob={best.model_prob:.2f} ask_yes={best.market_price:.3f} → "
            f"no_prob={new_prob:.2f} ask_no={new_ask:.3f}) "
            f"size=${best.suggested_size_usdc:.2f} (preserved from YES Kelly)"
        )
        best = Opportunity(
            market=best.market, bucket=best.bucket, forecast=best.forecast,
            model_prob=new_prob, market_price=new_ask, ev=new_ev,
            confidence=best.confidence,
            # Keep the original YES Kelly fraction for downstream introspection;
            # the size that actually gets used is suggested_size_usdc.
            kelly_fraction=best.kelly_fraction,
            suggested_size_usdc=best.suggested_size_usdc,
            side="no", trade_token_id=new_token,
            hours_to_resolution=best.hours_to_resolution, contrarian=True,
        )

    logger.info(
        f"  EDGE [{market.market_type.emoji}]: {best.bucket.outcome_label} {best.side.upper()} "
        f"model={best.model_prob:.1%} ask={best.market_price:.1%} EV={best.ev:.1%} "
        f"conf={best.confidence:.1%} size=${best.suggested_size_usdc:.2f}"
    )
    return [best]


# ── Portfolio-level risk check ────────────────────────────────────────────────

def apply_daily_limit(
    opportunities: List[Opportunity],
    already_spent_today: float,
    daily_max: Optional[float] = None,
) -> List[Opportunity]:
    daily_max = daily_max if daily_max is not None else settings.daily_max_usdc
    remaining = daily_max - already_spent_today
    if remaining <= 0:
        logger.warning(f"Daily limit ${daily_max:.0f} reached — no new trades")
        return []

    approved: List[Opportunity] = []
    for opp in opportunities:
        if remaining <= 0:
            break
        capped = min(opp.suggested_size_usdc, remaining)
        if capped < 1.0:
            continue
        opp.suggested_size_usdc = capped
        remaining -= capped
        approved.append(opp)

    return approved
