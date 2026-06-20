"""
Tests for the contrarian YES inversion flag (Option F).

When ``settings.contrarian_yes_inversion = True``, every market the strategy
would buy YES on must be mirrored to a NO bet on the same bucket. Decision
logic (which buckets pass EV/min_prob gates) is unchanged — only the bet side
flips. NO-side originals must pass through untouched.
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


def _market(best_ask_yes: float, best_ask_no: float) -> WeatherMarket:
    bucket = WeatherBucket(
        token_id="yes-tok", outcome_label="71°F", lower=70.0, upper=71.0,
        no_token_id="no-tok", best_ask=best_ask_yes, best_ask_no=best_ask_no,
    )
    return WeatherMarket(
        market_id="m1", question="q", city="Testville",
        target_date=(now_utc() + timedelta(hours=24)).date(),
        resolution_datetime=now_utc() + timedelta(hours=24),
        market_type=MarketType.TEMPERATURE, buckets=[bucket],
    )


def _evaluate(prob: float, ask_yes: float, ask_no: float):
    with patch("src.calibration.calibrate_probability", side_effect=lambda p, t: p), \
         patch("src.calibration.city_skill_factor", return_value=1.0):
        return evaluate_market(_market(ask_yes, ask_no), _FakeForecast(prob), min_ev=0.20)


def test_yes_pick_inverts_to_no_when_flag_set() -> None:
    # Without the flag: prob 0.80 at ask_yes 0.50 picks YES (EV +0.6).
    with patch.object(settings, "contrarian_yes_inversion", False):
        opps = _evaluate(0.80, 0.50, 0.45)
        assert len(opps) == 1 and opps[0].side == "yes"
        assert opps[0].market_price == 0.50
        assert opps[0].trade_token_id == "yes-tok"

    # With the flag: same market — the YES pick is mirrored to NO at ask_no.
    with patch.object(settings, "contrarian_yes_inversion", True):
        opps = _evaluate(0.80, 0.50, 0.45)
        assert len(opps) == 1 and opps[0].side == "no"
        assert abs(opps[0].model_prob - 0.20) < 1e-9  # 1 − 0.80
        assert opps[0].market_price == 0.45
        assert opps[0].trade_token_id == "no-tok"


def test_no_pick_unaffected_by_flag() -> None:
    # prob 0.20 at ask_yes 0.50 → YES EV −0.6, but NO has p=0.80, ask=0.15 → EV +4.3.
    # Strategy picks NO. The flag must NOT touch it (only YES picks are inverted).
    # NB: EV 4.3 exceeds the configured max_ev_cap, which would skip the bet, so we
    # use a slightly higher NO ask to land inside the cap.
    with patch.object(settings, "contrarian_yes_inversion", True):
        opps = _evaluate(0.20, 0.90, 0.50)
        assert len(opps) == 1 and opps[0].side == "no"
        assert opps[0].market_price == 0.50
        assert opps[0].trade_token_id == "no-tok"


def test_gates_still_apply_before_inversion() -> None:
    # prob 0.10 at ask_yes 0.05 — passes EV but fails min_model_prob gate.
    # Inversion must NOT rescue this; gates are checked before the flip.
    with patch.object(settings, "contrarian_yes_inversion", True):
        assert _evaluate(0.10, 0.05, 0.90) == []
