"""
Forecast engine — fetches Open-Meteo ensemble data and computes calibrated
bucket probabilities for temperature, precipitation, and snowfall markets.

Temperature uses Gaussian KDE over continuous ensemble spread.
Precipitation/snowfall use empirical bucket counting — better for zero-inflated
distributions where KDE performs poorly.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import httpx
import numpy as np
from loguru import logger
from scipy.stats import gaussian_kde

from config.settings import BIAS_DB, settings
from src.utils import celsius_to_fahrenheit, http_retry, rate_limited_sleep

OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Limit concurrent Open-Meteo requests across all threads
_OPEN_METEO_SEM = threading.Semaphore(4)

DEFAULT_MODEL_WEIGHTS: Dict[str, float] = {
    "icon_seamless": 1.0,
    "gfs_seamless": 0.9,
    "ecmwf_ifs025": 1.1,
    "gem_seamless": 0.8,
    "bom_access_global_ensemble": 0.7,
}

# Sentinel for open-ended buckets
OPEN_END = 9999.0


# ── Shared interface helpers ──────────────────────────────────────────────────

def _empirical_bucket_prob(members: np.ndarray, lower: float, upper: float) -> float:
    """
    Fraction of ensemble members in [lower, upper).
    Handles open-ended sentinels and the special (0, 0.01) "zero" bucket.
    """
    if lower <= 0.0 and upper <= 0.01:
        # Zero/trace bucket: count members that are effectively zero
        return float(np.mean(members <= 0.001))
    lo = lower if lower > -OPEN_END else -np.inf
    hi = upper if upper < OPEN_END else np.inf
    if np.isinf(hi):
        return float(np.mean(members >= lo))
    if np.isinf(lo):
        return float(np.mean(members < hi))
    return float(np.mean((members >= lo) & (members < hi)))


def _normalize_probs(
    raw: Dict[Tuple[float, float], float]
) -> Dict[Tuple[float, float], float]:
    total = sum(raw.values())
    if total > 0:
        return {k: v / total for k, v in raw.items()}
    return raw


# ── Temperature forecast (KDE-based) ─────────────────────────────────────────

@dataclass
class ForecastResult:
    city: str
    target_date: date
    model_name: str
    raw_members_c: List[float]
    raw_members_f: List[float]
    mean_f: float
    std_f: float
    kde: Optional[gaussian_kde] = None
    bias_correction_f: float = 0.0


@dataclass
class EnsembleForecast:
    """Temperature forecast — KDE over °F ensemble members."""
    city: str
    target_date: date
    model_results: List[ForecastResult] = field(default_factory=list)
    combined_members_f: List[float] = field(default_factory=list)
    combined_kde: Optional[gaussian_kde] = None
    mean_f: float = 0.0
    std_f: float = 0.0
    confidence: float = 0.0

    def bucket_probability(self, lower_f: float, upper_f: float) -> float:
        if self.combined_kde is None or not self.combined_members_f:
            return 0.0
        pts = np.linspace(lower_f, upper_f, 200)
        prob = float(np.trapz(self.combined_kde(pts), pts))
        return max(0.0, min(1.0, prob))

    def all_bucket_probabilities(
        self, buckets: List[Tuple[float, float]]
    ) -> Dict[Tuple[float, float], float]:
        raw = {b: self.bucket_probability(b[0], b[1]) for b in buckets}
        return _normalize_probs(raw)


# ── Precipitation forecast (empirical) ───────────────────────────────────────

@dataclass
class PrecipForecast:
    """
    Precipitation forecast — empirical probability counts over mm values.
    Better than KDE for zero-inflated precipitation distributions.
    """
    city: str
    target_date: date
    combined_members_mm: List[float] = field(default_factory=list)
    mean_f: float = 0.0      # mean precip in mm (field named mean_f for duck-typing)
    std_f: float = 0.0       # std in mm
    confidence: float = 0.0
    zero_fraction: float = 0.0   # fraction of members predicting no precip

    def bucket_probability(self, lower_mm: float, upper_mm: float) -> float:
        if not self.combined_members_mm:
            return 0.0
        arr = np.array(self.combined_members_mm)
        return _empirical_bucket_prob(arr, lower_mm, upper_mm)

    def all_bucket_probabilities(
        self, buckets: List[Tuple[float, float]]
    ) -> Dict[Tuple[float, float], float]:
        raw = {b: self.bucket_probability(b[0], b[1]) for b in buckets}
        return _normalize_probs(raw)


# ── Snowfall forecast (empirical) ────────────────────────────────────────────

@dataclass
class SnowForecast:
    """
    Snowfall forecast — empirical probability counts over cm values.
    Same zero-inflation logic as PrecipForecast.
    """
    city: str
    target_date: date
    combined_members_cm: List[float] = field(default_factory=list)
    mean_f: float = 0.0      # mean snow in cm (duck-typed as mean_f)
    std_f: float = 0.0
    confidence: float = 0.0
    zero_fraction: float = 0.0

    def bucket_probability(self, lower_cm: float, upper_cm: float) -> float:
        if not self.combined_members_cm:
            return 0.0
        arr = np.array(self.combined_members_cm)
        return _empirical_bucket_prob(arr, lower_cm, upper_cm)

    def all_bucket_probabilities(
        self, buckets: List[Tuple[float, float]]
    ) -> Dict[Tuple[float, float], float]:
        raw = {b: self.bucket_probability(b[0], b[1]) for b in buckets}
        return _normalize_probs(raw)


# Union type for all forecast variants — use in type hints
AnyForecast = Union[EnsembleForecast, PrecipForecast, SnowForecast]


# ── Bias correction store ─────────────────────────────────────────────────────

class BiasStore:
    """Per-city, per-model, per-variable bias correction."""

    def __init__(self, db_path: Path = BIAS_DB) -> None:
        self._db = str(db_path)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self._db) as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS bias (
                    city TEXT,
                    model TEXT,
                    variable TEXT DEFAULT 'temperature',
                    target_date TEXT,
                    forecast_mean REAL,
                    observed REAL,
                    error REAL,
                    PRIMARY KEY (city, model, variable, target_date)
                )
                """
            )
            # Migrate old table if it lacks `variable` column
            try:
                c.execute("ALTER TABLE bias ADD COLUMN variable TEXT DEFAULT 'temperature'")
            except Exception:
                pass
            c.commit()

    def record(self, city: str, model: str, variable: str, target_date: date,
               forecast_mean: float, observed: float) -> None:
        with sqlite3.connect(self._db) as c:
            c.execute(
                "INSERT OR REPLACE INTO bias VALUES (?,?,?,?,?,?,?)",
                (city, model, variable, str(target_date), forecast_mean, observed, observed - forecast_mean),
            )
            c.commit()

    def get_correction(self, city: str, model: str, variable: str = "temperature",
                       days: int = 30) -> float:
        if days == 0:
            return 0.0
        with sqlite3.connect(self._db) as c:
            rows = c.execute(
                """
                SELECT error FROM bias
                WHERE city=? AND model=? AND variable=?
                ORDER BY target_date DESC LIMIT ?
                """,
                (city, model, variable, days),
            ).fetchall()
        return float(np.mean([r[0] for r in rows])) if rows else 0.0


_bias_store = BiasStore()


# ── Open-Meteo fetch helpers ──────────────────────────────────────────────────

def _fetch_models_parallel(
    lat: float, lon: float, target_date: date, models: List[str], variable: str
) -> List[Tuple[str, Optional[Dict]]]:
    """Fetch one Open-Meteo variable for multiple models concurrently."""
    def _one(model: str) -> Tuple[str, Optional[Dict]]:
        with _OPEN_METEO_SEM:
            time.sleep(0.3)
            return model, _fetch_ensemble_vars(lat, lon, target_date, model, variable)

    with ThreadPoolExecutor(max_workers=min(len(models), 4)) as pool:
        return list(pool.map(_one, models))


@http_retry
def _fetch_ensemble_vars(
    lat: float, lon: float, target_date: date, model: str, variables: str
) -> Optional[Dict[str, List[float]]]:
    """
    Generic Open-Meteo ensemble fetch.
    Returns {variable_key: [member_values]} or None on failure.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": variables,
        "timezone": "UTC",
        "start_date": str(target_date),
        "end_date": str(target_date),
        "models": model,
    }
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(OPEN_METEO_ENSEMBLE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily", {})
        result: Dict[str, List[float]] = {}
        for key, values in daily.items():
            if values:
                val = values[0]
                if val is not None:
                    result.setdefault(key.split("_member")[0], []).append(float(val))

        return result if result else None
    except Exception as e:
        logger.error(f"Open-Meteo fetch failed [{model}/{variables}] ({lat},{lon}) {target_date}: {e}")
        return None


# ── KDE fitting ───────────────────────────────────────────────────────────────

def _fit_kde(samples: List[float]) -> Optional[gaussian_kde]:
    if len(samples) < 5:
        return None
    try:
        arr = np.array(samples)
        std = arr.std()
        if std < 0.001:
            return None
        bw = max(0.5, std * 0.3)
        return gaussian_kde(arr, bw_method=bw / std)
    except Exception as e:
        logger.warning(f"KDE fit failed: {e}")
        return None


# ── Confidence scoring ────────────────────────────────────────────────────────

def _temp_confidence(combined_std_f: float, n_models: int) -> float:
    spread_conf = float(np.clip(1.0 - (combined_std_f - 2.0) / 8.0, 0.1, 1.0))
    model_bonus = min(0.1 * (n_models - 1), 0.2)
    return float(np.clip(spread_conf + model_bonus, 0.0, 1.0))


def _precip_confidence(members: np.ndarray, n_models: int) -> float:
    """
    Confidence is highest when ensemble strongly agrees on wet vs dry.
    Also rewards tight agreement on the accumulation amount.
    """
    zero_frac = float(np.mean(members <= 0.001))
    # How decisive is the ensemble? (0.5 = max uncertainty, 1.0 = full agreement)
    p_majority = max(zero_frac, 1.0 - zero_frac)
    # Map [0.5, 1.0] → [0.2, 0.95]
    decisiveness = float(np.clip((p_majority - 0.5) * 1.5 + 0.2, 0.2, 0.95))

    # For non-zero members, reward tight spread
    nonzero = members[members > 0.001]
    if len(nonzero) >= 5:
        cv = nonzero.std() / (nonzero.mean() + 1e-6)  # coefficient of variation
        spread_bonus = float(np.clip(0.1 - cv * 0.05, 0.0, 0.1))
    else:
        spread_bonus = 0.0

    model_bonus = min(0.05 * (n_models - 1), 0.15)
    return float(np.clip(decisiveness + spread_bonus + model_bonus, 0.0, 1.0))


# ── Temperature forecast ──────────────────────────────────────────────────────

def get_ensemble_forecast(
    city: str,
    lat: float,
    lon: float,
    target_date: date,
    models: Optional[List[str]] = None,
    use_bias_correction: bool = True,
) -> Optional[EnsembleForecast]:
    if models is None:
        models = settings.ensemble_model_list

    result = EnsembleForecast(city=city, target_date=target_date)
    all_weighted: List[float] = []

    for model, data in _fetch_models_parallel(lat, lon, target_date, models, "temperature_2m_max"):
        if not data:
            continue

        raw_c = data.get("temperature_2m_max", [])
        if not raw_c:
            continue

        raw_f = [celsius_to_fahrenheit(c) for c in raw_c]

        bias = 0.0
        if use_bias_correction and settings.bias_correction_days > 0:
            bias = _bias_store.get_correction(city, model, "temperature", settings.bias_correction_days)

        corrected_f = [v + bias for v in raw_f]
        mean_f = float(np.mean(corrected_f))
        std_f = float(np.std(corrected_f))

        result.model_results.append(ForecastResult(
            city=city, target_date=target_date, model_name=model,
            raw_members_c=raw_c, raw_members_f=raw_f,
            mean_f=mean_f, std_f=std_f,
            kde=_fit_kde(corrected_f), bias_correction_f=bias,
        ))

        w = DEFAULT_MODEL_WEIGHTS.get(model, 0.8)
        repeats = max(1, round(w * 10))
        all_weighted.extend(corrected_f * repeats)

        logger.debug(f"  temp/{model}: {len(raw_c)} members, mean={mean_f:.1f}°F, std={std_f:.1f}°F, bias={bias:+.1f}°F")

    if not all_weighted:
        logger.warning(f"No temperature forecast for {city} on {target_date}")
        return None

    result.combined_members_f = all_weighted
    result.combined_kde = _fit_kde(all_weighted)
    result.mean_f = float(np.mean(all_weighted))
    result.std_f = float(np.std(all_weighted))
    result.confidence = _temp_confidence(result.std_f, len(result.model_results))

    logger.info(
        f"Temp forecast {city} {target_date}: mean={result.mean_f:.1f}°F "
        f"std={result.std_f:.1f}°F conf={result.confidence:.2f} "
        f"models={[m.model_name for m in result.model_results]}"
    )
    return result


# ── Precipitation forecast ────────────────────────────────────────────────────

def get_precip_forecast(
    city: str,
    lat: float,
    lon: float,
    target_date: date,
    models: Optional[List[str]] = None,
    use_bias_correction: bool = True,
) -> Optional[PrecipForecast]:
    """
    Fetch precipitation_sum ensemble, apply bias correction, return PrecipForecast.
    All values in mm.
    """
    if models is None:
        models = settings.ensemble_model_list

    result = PrecipForecast(city=city, target_date=target_date)
    all_weighted_mm: List[float] = []

    for model, data in _fetch_models_parallel(lat, lon, target_date, models, "precipitation_sum"):
        if not data:
            continue

        raw_mm = data.get("precipitation_sum", [])
        if not raw_mm:
            continue

        # Clip negatives (numerical artifacts)
        raw_mm = [max(0.0, v) for v in raw_mm]

        bias = 0.0
        if use_bias_correction and settings.bias_correction_days > 0:
            bias = _bias_store.get_correction(city, model, "precipitation", settings.bias_correction_days)

        corrected_mm = [max(0.0, v + bias) for v in raw_mm]

        w = DEFAULT_MODEL_WEIGHTS.get(model, 0.8)
        repeats = max(1, round(w * 10))
        all_weighted_mm.extend(corrected_mm * repeats)

        mean_mm = float(np.mean(corrected_mm))
        zero_frac = float(np.mean(np.array(corrected_mm) <= 0.001))
        logger.debug(f"  precip/{model}: {len(raw_mm)} members, mean={mean_mm:.2f}mm, dry={zero_frac:.0%}, bias={bias:+.2f}mm")

    if not all_weighted_mm:
        logger.warning(f"No precipitation forecast for {city} on {target_date}")
        return None

    arr = np.array(all_weighted_mm)
    result.combined_members_mm = all_weighted_mm
    result.mean_f = float(np.mean(arr))
    result.std_f = float(np.std(arr))
    result.zero_fraction = float(np.mean(arr <= 0.001))
    result.confidence = _precip_confidence(arr, len(models))

    logger.info(
        f"Precip forecast {city} {target_date}: mean={result.mean_f:.2f}mm "
        f"dry={result.zero_fraction:.0%} conf={result.confidence:.2f}"
    )
    return result


# ── Snowfall forecast ─────────────────────────────────────────────────────────

def get_snow_forecast(
    city: str,
    lat: float,
    lon: float,
    target_date: date,
    models: Optional[List[str]] = None,
    use_bias_correction: bool = True,
) -> Optional[SnowForecast]:
    """
    Fetch snowfall_sum ensemble, apply bias correction, return SnowForecast.
    All values in cm.
    """
    if models is None:
        models = settings.ensemble_model_list

    result = SnowForecast(city=city, target_date=target_date)
    all_weighted_cm: List[float] = []

    for model, data in _fetch_models_parallel(lat, lon, target_date, models, "snowfall_sum"):
        if not data:
            continue

        raw_cm = data.get("snowfall_sum", [])
        if not raw_cm:
            continue

        raw_cm = [max(0.0, v) for v in raw_cm]

        bias = 0.0
        if use_bias_correction and settings.bias_correction_days > 0:
            bias = _bias_store.get_correction(city, model, "snowfall", settings.bias_correction_days)

        corrected_cm = [max(0.0, v + bias) for v in raw_cm]

        w = DEFAULT_MODEL_WEIGHTS.get(model, 0.8)
        repeats = max(1, round(w * 10))
        all_weighted_cm.extend(corrected_cm * repeats)

        mean_cm = float(np.mean(corrected_cm))
        zero_frac = float(np.mean(np.array(corrected_cm) <= 0.001))
        logger.debug(f"  snow/{model}: {len(raw_cm)} members, mean={mean_cm:.2f}cm, no-snow={zero_frac:.0%}, bias={bias:+.2f}cm")

    if not all_weighted_cm:
        logger.warning(f"No snowfall forecast for {city} on {target_date}")
        return None

    arr = np.array(all_weighted_cm)
    result.combined_members_cm = all_weighted_cm
    result.mean_f = float(np.mean(arr))
    result.std_f = float(np.std(arr))
    result.zero_fraction = float(np.mean(arr <= 0.001))
    result.confidence = _precip_confidence(arr, len(models))  # same logic as precip

    logger.info(
        f"Snow forecast {city} {target_date}: mean={result.mean_f:.2f}cm "
        f"no-snow={result.zero_fraction:.0%} conf={result.confidence:.2f}"
    )
    return result


# ── Public bias recording ─────────────────────────────────────────────────────

def record_observed_temp(city: str, model: str, target_date: date,
                         forecast_mean_f: float, observed_f: float) -> None:
    _bias_store.record(city, model, "temperature", target_date, forecast_mean_f, observed_f)
    logger.info(f"Bias recorded [temp]: {city}/{model} forecast={forecast_mean_f:.1f}°F actual={observed_f:.1f}°F error={observed_f-forecast_mean_f:+.1f}°F")


def record_observed_precip(city: str, model: str, target_date: date,
                           forecast_mean_mm: float, observed_mm: float) -> None:
    _bias_store.record(city, model, "precipitation", target_date, forecast_mean_mm, observed_mm)
    logger.info(f"Bias recorded [precip]: {city}/{model} forecast={forecast_mean_mm:.2f}mm actual={observed_mm:.2f}mm error={observed_mm-forecast_mean_mm:+.2f}mm")


def record_observed_snow(city: str, model: str, target_date: date,
                         forecast_mean_cm: float, observed_cm: float) -> None:
    _bias_store.record(city, model, "snowfall", target_date, forecast_mean_cm, observed_cm)
    logger.info(f"Bias recorded [snow]: {city}/{model} forecast={forecast_mean_cm:.2f}cm actual={observed_cm:.2f}cm error={observed_cm-forecast_mean_cm:+.2f}cm")
