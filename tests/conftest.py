"""
Shared test fixtures.

Tests must be deterministic regardless of the production ``.env`` on the box
they run on (the VPS `.env` sets NO_SIDE_ONLY, MIN_NO_ASK, CALIBRATION_HAIRCUT,
etc., which silently change strategy behavior under test). Pin every setting
the strategy/calibration tests depend on back to its code default; individual
tests override with ``patch.object(settings, ...)`` as needed.
"""

import pytest

from config.settings import settings

_PINNED_DEFAULTS = {
    # Strategy gates / side selection
    "no_side_only": False,
    "min_no_ask": 0.0,
    "max_no_ask": 0.80,
    "min_model_prob": 0.55,
    "min_ev_threshold": 0.20,
    "contrarian_yes_inversion": False,
    "mode_bucket_no_min_prob": 0.0,  # off: legacy fixtures lack forecast.mean_f
    "slippage_tax": 0.02,
    # Sizing
    "kelly_fraction": 0.25,
    "max_trade_usdc": 50.0,
    # Calibration: tests exercise the isotonic-curve path, not the raw bypass
    "use_raw_calibration": False,
    "calibration_haircut": 0.7,
    # Universe filters must not hide test fixtures
    "city_allowlist": "",
    "city_blacklist": "",
    # Forecast engine: legacy KDE unless a test opts into EMOS explicitly
    "forecast_engine": "kde",
    "ground_truth_source": "om",
    # Logger scope: tests pin allowlist-only for determinism
    "forecast_log_all_cities": False,
}


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _PINNED_DEFAULTS.items():
        monkeypatch.setattr(settings, name, value)
