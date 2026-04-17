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
    model_prob: float
    market_price: float         # ask price (0–1)
    ev: float
    confidence: float
    kelly_fraction: float
    suggested_size_usdc: float
    hours_to_resolution: Optional[float] = None

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
            f"[{self.bucket.outcome_label}]: "
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

    effective_min_conf = _effective_min_confidence(market.market_type, min_confidence)
    if adj_confidence < effective_min_conf:
        logger.debug(
            f"{market.market_type.value} {market.city} {market.target_date}: "
            f"conf {adj_confidence:.2f} < {effective_min_conf:.2f}, skip"
        )
        return []

    probs = normalize_bucket_probs(forecast, market.buckets)

    opportunities: List[Opportunity] = []
    for bucket in market.buckets:
        ask = bucket.best_ask
        if ask <= 0.01 or ask >= 0.99:
            continue

        model_prob = probs.get(bucket.token_id, 0.0)
        if model_prob < 0.01:
            continue

        ev = compute_ev(model_prob, ask)
        if ev < min_ev:
            logger.debug(
                f"  [{market.market_type.value}] {bucket.outcome_label}: "
                f"model={model_prob:.3f} ask={ask:.3f} EV={ev:.3f} < {min_ev}"
            )
            continue

        size = suggested_position_size(model_prob, ask, bankroll, kelly_mult, max_usdc)
        kf = kelly_fraction(model_prob, ask)

        opp = Opportunity(
            market=market,
            bucket=bucket,
            forecast=forecast,
            model_prob=model_prob,
            market_price=ask,
            ev=ev,
            confidence=adj_confidence,
            kelly_fraction=kf,
            suggested_size_usdc=size,
            hours_to_resolution=hours_left,
        )
        opportunities.append(opp)
        logger.info(
            f"  EDGE [{market.market_type.emoji}]: {bucket.outcome_label} "
            f"model={model_prob:.1%} ask={ask:.1%} EV={ev:.1%} "
            f"conf={adj_confidence:.1%} size=${size:.2f}"
        )

    return opportunities


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
