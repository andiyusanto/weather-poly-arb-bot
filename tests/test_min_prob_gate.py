"""
Tests for the min_model_prob gate in strategy.evaluate_market.

EV-only triggering buys cheap longshots (low prob, even-lower ask) where the
model is empirically anti-predictive. The gate requires the bet side's
probability to clear settings.min_model_prob.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from config.settings import settings
from src.polymarket_client import MarketType, WeatherBucket, WeatherMarket
from src.strategy import evaluate_market
from src.utils import now_utc


class _FakeForecast:
    def __init__(self, prob: float, conf: float = 0.95) -> None:
        self._prob = prob
        self.confidence = conf

    def all_bucket_probabilities(self, buckets):
        return {b: self._prob for b in buckets}


def _market(best_ask: float) -> WeatherMarket:
    bucket = WeatherBucket(
        token_id="yes", outcome_label="71°F", lower=70.0, upper=71.0,
        no_token_id="no", best_ask=best_ask, best_ask_no=0.0,
    )
    return WeatherMarket(
        market_id="m1", question="q", city="Testville",
        target_date=(now_utc() + timedelta(hours=24)).date(),
        resolution_datetime=now_utc() + timedelta(hours=24),
        market_type=MarketType.TEMPERATURE, buckets=[bucket],
    )


def _evaluate(prob: float, best_ask: float):
    # Force calibration to identity + neutral skill so we test the gate directly.
    # Also force the contrarian flag OFF so it doesn't interfere with gate-only
    # assertions — those tests are about whether the YES gate passes/fails the
    # bet, not whether a passing YES then gets mirrored to NO. The fixture
    # bucket has best_ask_no=0 which would (correctly) cause the contrarian
    # flip to refuse and return [] when the flag is on globally via env var.
    with patch("src.calibration.calibrate_probability", side_effect=lambda p, t: p), \
         patch("src.calibration.city_skill_factor", return_value=1.0), \
         patch.object(settings, "contrarian_yes_inversion", False):
        return evaluate_market(_market(best_ask), _FakeForecast(prob), min_ev=0.20)


def test_low_prob_longshot_is_filtered() -> None:
    # prob 0.10 at ask 0.05 → EV +1.0 (passes EV) but prob < 0.55 → must be skipped.
    assert _evaluate(0.10, 0.05) == []


def test_high_prob_bet_passes() -> None:
    # prob 0.80 at ask 0.50 → EV +0.6 and prob >= 0.55 → an opportunity.
    opps = _evaluate(0.80, 0.50)
    assert len(opps) == 1
    assert opps[0].side == "yes"
    assert opps[0].model_prob >= settings.min_model_prob


def test_boundary_just_below_floor() -> None:
    floor = settings.min_model_prob
    # ask 0.40 keeps EV within [min_ev, ev_cap] so only the prob gate decides.
    assert _evaluate(floor - 0.01, 0.40) == []          # just under → filtered
    assert len(_evaluate(floor + 0.01, 0.40)) == 1       # just over → kept
