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

        # ── YES side ──
        ask_yes = bucket.best_ask
        ev_yes = -1.0
        if min_ask <= ask_yes <= max_ask:
            ev_yes = compute_ev(p_yes, ask_yes)

        # ── NO side ──
        ask_no = bucket.no_ask
        ev_no = -1.0
        if min_ask <= ask_no <= max_ask:
            ev_no = compute_ev(p_no, ask_no)

        # Pick the better side, if any.
        if ev_yes >= ev_no:
            side, side_prob, side_ask, side_ev = "yes", p_yes, ask_yes, ev_yes
            trade_token = bucket.token_id
        else:
            side, side_prob, side_ask, side_ev = "no", p_no, ask_no, ev_no
            trade_token = bucket.no_token_id or bucket.token_id

        if side_ev < min_ev:
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

        # ── Contrarian YES inversion (Option F) ─────────────────────────────
        # The model's YES picks have a stable structural overconfidence bias.
        # When this flag is set, every market that EV-passes for YES is bought
        # as NO on the same bucket instead. The decision logic (which markets,
        # which buckets) is unchanged — only the SIDE flips. No re-gating on
        # min_ev or min_model_prob: the inversion is a strategic mirror, not a
        # fresh evaluation. NO-side originals pass through untouched.
        if settings.contrarian_yes_inversion and side == "yes":
            side = "no"
            side_prob = p_no
            side_ask = ask_no
            side_ev = ev_no if ev_no > -1.0 else compute_ev(p_no, ask_no)
            trade_token = bucket.no_token_id or bucket.token_id
            logger.info(
                f"  CONTRARIAN: YES→NO on {bucket.outcome_label} "
                f"(yes_prob={p_yes:.2f} ask_yes={ask_yes:.3f} → "
                f"no_prob={p_no:.2f} ask_no={side_ask:.3f})"
            )

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
        ))

    if not candidate_opps:
        return []

    # Joint Kelly: buckets within an event are mutually exclusive, so allocate
    # to a SINGLE position per event — the highest-EV side/bucket.
    candidate_opps.sort(key=lambda o: o.ev, reverse=True)
    best = candidate_opps[0]

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
