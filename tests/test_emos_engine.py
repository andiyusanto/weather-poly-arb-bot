"""
Tests for the EMOS-lite forecast engine and the per-model bias recorder fix.

EMOS-lite: bucket probability = Gaussian mass at the bias-corrected combined
mean with per-city climatological error sigma (BiasStore.city_error_sigma).
Selected via settings.forecast_engine="emos"; None emos_sigma_f keeps the KDE
path byte-identical.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.forecast import BiasStore, EnsembleForecast


def _gauss_mass(mu: float, sd: float, lo: float, hi: float) -> float:
    a = (lo - mu) / (sd * math.sqrt(2))
    b = (hi - mu) / (sd * math.sqrt(2))
    return 0.5 * (math.erf(b) - math.erf(a))


# ── EMOS bucket probability ───────────────────────────────────────────────────

def test_emos_bucket_probability_is_gaussian_mass() -> None:
    f = EnsembleForecast(city="X", target_date=date(2026, 7, 10),
                         mean_f=90.0, emos_sigma_f=1.5)
    got = f.bucket_probability(89.0, 91.0)
    assert abs(got - _gauss_mass(90.0, 1.5, 89.0, 91.0)) < 1e-9


def test_emos_probabilities_normalize_over_bucket_set() -> None:
    f = EnsembleForecast(city="X", target_date=date(2026, 7, 10),
                         mean_f=90.0, emos_sigma_f=2.0)
    buckets = [(84.0 + 2 * i, 86.0 + 2 * i) for i in range(6)]  # 84..96 covers ±3σ
    probs = f.all_bucket_probabilities(buckets)
    assert abs(sum(probs.values()) - 1.0) < 1e-6
    # modal bucket is the one containing the mean
    assert max(probs, key=probs.get) == (88.0, 90.0) or max(probs, key=probs.get) == (90.0, 92.0)


def test_no_emos_sigma_falls_back_to_kde_path() -> None:
    # Without emos_sigma_f and without a KDE, probability is 0 (KDE path).
    f = EnsembleForecast(city="X", target_date=date(2026, 7, 10), mean_f=90.0)
    assert f.bucket_probability(89.0, 91.0) == 0.0


# ── BiasStore.city_error_sigma ────────────────────────────────────────────────

def _seed(store: BiasStore, city: str, errors: list[float], start_day: int = 1) -> None:
    for i, e in enumerate(errors):
        store.record(city=city, model="ensemble", variable="temperature",
                     target_date=date(2026, 6, start_day + i),
                     forecast_mean=90.0, observed=90.0 + e)


def test_city_sigma_shrinks_toward_global(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "bias.db")
    # Global: two cities, one tight (±0.5), one wide (±4.0), 20 days each.
    _seed(store, "Tight", [0.5, -0.5] * 10)
    _seed(store, "Wide", [4.0, -4.0] * 10)
    tight = store.city_error_sigma("Tight", min_global=10)
    wide = store.city_error_sigma("Wide", min_global=10)
    # Shrinkage keeps them strictly between own-std and global-std
    assert 0.5 < tight < wide < 4.0
    # And ordering is preserved: tight city gets a sharper sigma.
    assert tight < wide


def test_city_sigma_none_when_history_thin(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "bias.db")
    _seed(store, "OnlyCity", [1.0, -1.0, 2.0])
    assert store.city_error_sigma("OnlyCity", min_global=30) is None


def test_unknown_city_gets_global_sigma(tmp_path: Path) -> None:
    store = BiasStore(tmp_path / "bias.db")
    _seed(store, "A", [2.0, -2.0] * 10)
    sig = store.city_error_sigma("NeverSeen", min_global=10)
    assert sig is not None and 1.5 < sig < 2.5


def test_legacy_duplicated_rows_dedupe_via_avg(tmp_path: Path) -> None:
    # Pre-fix rows: same combined mean under 3 model names. _combined_errors
    # must count each (city, date) once, not three times.
    store = BiasStore(tmp_path / "bias.db")
    for i in range(12):
        for model in ("icon_seamless", "gfs_seamless", "ecmwf_ifs025"):
            store.record(city="L", model=model, variable="temperature",
                         target_date=date(2026, 6, 1 + i),
                         forecast_mean=90.0, observed=90.0 + (1.0 if i % 2 else -1.0))
    errs = store._combined_errors("temperature", city="L")
    assert len(errs) == 12


# ── bias recorder: per-model rows from model_means JSON ───────────────────────

def test_recorder_writes_per_model_and_ensemble_rows(tmp_path: Path) -> None:
    import src.bias_recorder as br
    store = BiasStore(tmp_path / "bias.db")
    trade = dict(city="Testville", target_date="2026-07-09", market_type="temperature",
                 forecast_mean=90.0,
                 model_means=json.dumps({"icon_seamless": 91.0, "gfs_seamless": 89.0}))
    with patch.object(br, "_bias_store", store), \
         patch.object(br._geo, "get", return_value={"lat": 1.0, "lon": 2.0}), \
         patch.object(br, "_fetch_observed", return_value=32.0):  # 32°C → 89.6°F
        assert br.record_bias_for_resolved_trade(trade) is True

    import sqlite3
    with sqlite3.connect(store._db) as conn:
        rows = {m: (f, o) for m, f, o in conn.execute(
            "SELECT model, forecast_mean, observed FROM bias")}
    assert set(rows) == {"ensemble", "icon_seamless", "gfs_seamless"}
    assert rows["ensemble"][0] == 90.0          # combined mean
    assert rows["icon_seamless"][0] == 91.0     # model's OWN mean (the fix)
    assert rows["gfs_seamless"][0] == 89.0
    assert abs(rows["ensemble"][1] - 89.6) < 0.01  # observed °C→°F conversion
