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
        assert opps[0].contrarian is True  # marker propagates for analytics


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
        assert opps[0].contrarian is False  # natural NO — not flipped


def test_gates_still_apply_before_inversion() -> None:
    # prob 0.10 at ask_yes 0.05 — passes EV but fails min_model_prob gate.
    # Inversion must NOT rescue this; gates are checked before the flip.
    with patch.object(settings, "contrarian_yes_inversion", True):
        assert _evaluate(0.10, 0.05, 0.90) == []


def test_inversion_wins_against_competing_natural_no_in_same_event() -> None:
    # Regression for the bug found on 2026-06-22: when the flip happened per-bucket
    # *inside* the loop, the flipped NO opportunity got a strongly negative ev and
    # lost the cross-bucket EV sort to any other bucket's natural NO. The correct
    # behaviour is: pick the best-EV opp per event first (which is the YES one
    # here), THEN flip its side. The contrarian must survive the sort.
    from src.polymarket_client import MarketType, WeatherBucket, WeatherMarket

    bucket_strong_yes = WeatherBucket(
        token_id="yes-A", outcome_label="72°F or higher", lower=72.0, upper=999.0,
        no_token_id="no-A", best_ask=0.30, best_ask_no=0.70,
    )
    bucket_weak_no = WeatherBucket(
        token_id="yes-B", outcome_label="60°F or lower", lower=-999.0, upper=60.0,
        no_token_id="no-B", best_ask=0.85, best_ask_no=0.50,
    )
    market = WeatherMarket(
        market_id="m-multi", question="q", city="Testville",
        target_date=(now_utc() + timedelta(hours=24)).date(),
        resolution_datetime=now_utc() + timedelta(hours=24),
        market_type=MarketType.TEMPERATURE,
        buckets=[bucket_strong_yes, bucket_weak_no],
    )

    # Forecast: 0.70 prob for bucket A (strong YES at 0.30 ask, ev=+1.33), but
    # 0.40 prob for bucket B (natural NO at 0.50 ask, p_no=0.60, ev=+0.20).
    # The forecast contract is keyed by (lower, upper) tuples, not bucket objects
    # — WeatherBucket isn't hashable. See normalize_bucket_probs in strategy.py.
    class _Fcast:
        confidence = 0.95
        def all_bucket_probabilities(self, buckets):
            return {(72.0, 999.0): 0.70, (-999.0, 60.0): 0.40}

    with patch("src.calibration.calibrate_probability", side_effect=lambda p, t: p), \
         patch("src.calibration.city_skill_factor", return_value=1.0), \
         patch.object(settings, "contrarian_yes_inversion", True):
        opps = evaluate_market(market, _Fcast(), min_ev=0.20)

    assert len(opps) == 1
    opp = opps[0]
    # The contrarian flip MUST have landed on bucket A (the original YES winner).
    # Pre-fix, the natural NO at bucket B would have stolen the event because
    # the per-bucket flip gave bucket A's contrarian a negative ev.
    assert opp.contrarian is True, "contrarian flip lost to a natural NO — pre-fix bug"
    assert opp.bucket is bucket_strong_yes
    assert opp.side == "no"
    assert opp.trade_token_id == "no-A"
    assert abs(opp.market_price - 0.70) < 1e-9   # bucket A's no_ask
    assert abs(opp.model_prob - 0.30) < 1e-9     # 1 − 0.70
