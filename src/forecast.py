"""
Forecast engine — fetches Open-Meteo ensemble data and computes calibrated
bucket probabilities for temperature, precipitation, and snowfall markets.

Temperature uses Gaussian KDE over continuous ensemble spread.
Precipitation/snowfall use empirical bucket counting — better for zero-inflated
distributions where KDE performs poorly.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import httpx
import numpy as np
from loguru import logger
from scipy.stats import gaussian_kde

from config.settings import BIAS_DB, settings
from src.utils import celsius_to_fahrenheit, http_retry, rate_limited_sleep

OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Limit concurrent Open-Meteo requests across all threads. Free-tier hard
# limit is ~30 concurrent — but with 3 models × 1 variable per city and many
# cities, we hit the per-IP rate limit (429). Stay conservative.
_OPEN_METEO_SEM = threading.Semaphore(2)

# TTL cache for raw ensemble fetches keyed by (lat, lon, date, model, variables).
# The trader re-scans every ~30 min but weather forecasts barely move over a few
# hours, so without this we re-fetch everything each cycle and exhaust the
# Open-Meteo daily quota → ~69% of forecasts get dropped to 429s. Same-day
# targets still get last-minute tightening via the intraday-high path downstream.
# Only successful (non-None) results are cached, so a transient 429 is retried
# next cycle rather than suppressing the forecast for hours.
_FORECAST_TTL_S = 3 * 3600
_forecast_cache: Dict[tuple, Tuple[float, Dict[str, List[float]]]] = {}
_forecast_cache_lock = threading.Lock()

# Circuit breaker for sustained Open-Meteo rate limiting. When the quota is
# exhausted every request burns the full retry backoff (~15s) AND four more quota
# hits before giving up — deepening the hole. After enough consecutive persistent
# rate-limits we trip the breaker and short-circuit fetches for a cooldown, so the
# scan fails fast and stops hammering. Cached values are still served while open.
_CB_FAIL_THRESHOLD = 8
_CB_COOLDOWN_S = 300
_cb_lock = threading.Lock()
_cb_consecutive_fails = 0
_cb_open_until = 0.0


def _circuit_is_open() -> bool:
    with _cb_lock:
        return time.time() < _cb_open_until


def _circuit_record_success() -> None:
    global _cb_consecutive_fails, _cb_open_until
    with _cb_lock:
        _cb_consecutive_fails = 0
        _cb_open_until = 0.0


def _circuit_record_failure() -> None:
    global _cb_consecutive_fails, _cb_open_until
    with _cb_lock:
        _cb_consecutive_fails += 1
        if _cb_consecutive_fails >= _CB_FAIL_THRESHOLD and _cb_open_until <= time.time():
            _cb_open_until = time.time() + _CB_COOLDOWN_S
            logger.warning(
                f"Open-Meteo circuit breaker OPEN — {_cb_consecutive_fails} consecutive "
                f"rate-limit failures; pausing fetches {_CB_COOLDOWN_S}s to stop burning quota"
            )


def reset_circuit() -> None:
    """Force-close the rate-limit circuit breaker (e.g. at the start of a scan)."""
    _circuit_record_success()


# Variance-inflation (under-dispersion correction). Min resolved-error samples
# before we trust the bias table's dispersion estimate; until then, fall back to
# a conservative day-ahead temperature spread floor (°F). See _inflate_dispersion.
MIN_DISPERSION_SAMPLES = 20
_DEFAULT_DISPERSION_F = 2.0

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
    """Temperature forecast — KDE over °F ensemble members, or (when
    ``emos_sigma_f`` is set by the EMOS engine) a Gaussian at the
    bias-corrected combined mean with a per-city climatological error std."""
    city: str
    target_date: date
    model_results: List[ForecastResult] = field(default_factory=list)
    combined_members_f: List[float] = field(default_factory=list)
    combined_kde: Optional[gaussian_kde] = None
    mean_f: float = 0.0
    std_f: float = 0.0
    confidence: float = 0.0
    # EMOS engine: predictive sigma from BiasStore.city_error_sigma. None → KDE.
    emos_sigma_f: Optional[float] = None
    # Same-day markets: the max already observed today. The realized daily
    # high cannot land below this, so the EMOS Gaussian must be censored at
    # it (the KDE path gets the same effect from max()-clamped members).
    intraday_floor_f: Optional[float] = None

    def bucket_probability(self, lower_f: float, upper_f: float) -> float:
        if self.emos_sigma_f:
            # EMOS-lite: closed-form Gaussian mass on the bucket, censored at
            # the intraday floor. Without censoring, a same-day market with
            # the high already at 92°F would get ~40% mass on physically
            # impossible sub-92 buckets and manufacture fake NO edge.
            # Sigma is the city's realized forecast-error std, which
            # out-of-sample beat ensemble spread + global floor
            # (CRPS 0.955 vs 1.019, 2026-07-10).
            floor = self.intraday_floor_f
            if floor is not None and upper_f <= floor:
                return 0.0  # bucket entirely below what already happened
            sd = self.emos_sigma_f * math.sqrt(2.0)
            hi_cdf = 0.5 * (1.0 + math.erf((upper_f - self.mean_f) / sd))
            if floor is not None and lower_f <= floor:
                lo_cdf = 0.0  # censored mass collapses into this bucket
            else:
                lo_cdf = 0.5 * (1.0 + math.erf((lower_f - self.mean_f) / sd))
            return max(0.0, min(1.0, hi_cdf - lo_cdf))
        if self.combined_kde is None or not self.combined_members_f:
            return 0.0
        pts = np.linspace(lower_f, upper_f, 200)
        # np.trapz was removed in NumPy 2.0 — use trapezoid (also in scipy.integrate).
        _trap = getattr(np, "trapezoid", None) or getattr(np, "trapz", None)
        prob = float(_trap(self.combined_kde(pts), pts))
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


# ── Wind speed forecast (KDE-based) ──────────────────────────────────────────

KPH_TO_MPH = 0.621371

@dataclass
class WindForecast:
    """
    Wind speed forecast — Gaussian KDE over daily-max mph ensemble members.
    Wind speed is continuous and rarely zero, so KDE is appropriate (unlike precip/snow).
    """
    city: str
    target_date: date
    combined_members_mph: List[float] = field(default_factory=list)
    mean_f: float = 0.0      # mean wind speed in mph (duck-typed as mean_f for strategy compat)
    std_f: float = 0.0       # std in mph
    combined_kde: Optional[gaussian_kde] = None
    confidence: float = 0.0

    def bucket_probability(self, lower_mph: float, upper_mph: float) -> float:
        lo = max(0.0, lower_mph)
        if not self.combined_members_mph:
            return 0.0
        if self.combined_kde is not None:
            # Bound open-ended upper at 3× the observed max for integration
            hi = upper_mph if upper_mph < OPEN_END else float(np.max(self.combined_members_mph) * 3)
            if lo >= hi:
                return 0.0
            try:
                # integrate_box_1d avoids np.trapz (removed in NumPy 2.0)
                prob = float(self.combined_kde.integrate_box_1d(lo, hi))
                return max(0.0, min(1.0, prob))
            except Exception:
                pass
        # Fallback: empirical counting (also used when KDE not fitted, e.g. low variance)
        return _empirical_bucket_prob(np.array(self.combined_members_mph), lo, upper_mph)

    def all_bucket_probabilities(
        self, buckets: List[Tuple[float, float]]
    ) -> Dict[Tuple[float, float], float]:
        raw = {b: self.bucket_probability(b[0], b[1]) for b in buckets}
        return _normalize_probs(raw)


# Union type for all forecast variants — use in type hints
AnyForecast = Union[EnsembleForecast, PrecipForecast, SnowForecast, WindForecast]


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
            # Daily forecast snapshots — grow bias/sigma history without
            # trades (the funnel's same-day trades are all excluded from bias
            # recording, so trade-driven growth stalled; see 2026-07-20).
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS forecast_log (
                    city TEXT,
                    variable TEXT DEFAULT 'temperature',
                    target_date TEXT,
                    forecast_mean REAL,
                    created_at TEXT,
                    PRIMARY KEY (city, variable, target_date)
                )
                """
            )
            c.commit()

    def record(self, city: str, model: str, variable: str, target_date: date,
               forecast_mean: float, observed: float) -> None:
        with sqlite3.connect(self._db) as c:
            c.execute(
                "INSERT OR REPLACE INTO bias VALUES (?,?,?,?,?,?,?)",
                (city, model, variable, str(target_date), forecast_mean, observed, observed - forecast_mean),
            )
            c.commit()

    def log_forecast(self, city: str, variable: str, target_date: date,
                     forecast_mean: float) -> bool:
        """Snapshot a day-ahead forecast mean; first write per target wins
        (keeps a consistent ~24h lead). Returns True if newly inserted."""
        with sqlite3.connect(self._db) as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO forecast_log VALUES (?,?,?,?,?)",
                (city, variable, str(target_date), forecast_mean,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            c.commit()
            return cur.rowcount > 0

    def logged_cities(self, variable: str, target_date: date) -> set:
        """Cities already snapshotted for a target — callers skip these BEFORE
        fetching forecasts (a post-fetch dedup would re-fetch ~52 cities every
        resolve run: ~1,250 wasted Open-Meteo calls/day at 3h cadence)."""
        with sqlite3.connect(self._db) as c:
            rows = c.execute(
                "SELECT city FROM forecast_log WHERE variable=? AND target_date=?",
                (variable, str(target_date)),
            ).fetchall()
        return {r[0] for r in rows}

    def pending_forecast_logs(self, before: date, oldest: date
                              ) -> List[Tuple[str, str, str, float, bool, bool]]:
        """Elapsed snapshots still missing an OM or station bias row.

        Returns (city, variable, target_date, forecast_mean, need_om,
        need_station) for targets in [oldest, before]. The two legs are
        tracked independently so a transient IEM failure that lost only the
        station row is retried on later runs (the OM leg won't be rewritten).
        ``oldest`` caps the retry window — past it, OM's past-date endpoint no
        longer serves the day and the row is abandoned rather than re-fetched
        forever (bounds the per-run work and the shared Open-Meteo quota)."""
        with sqlite3.connect(self._db) as c:
            rows = c.execute(
                """SELECT f.city, f.variable, f.target_date, f.forecast_mean,
                     NOT EXISTS (SELECT 1 FROM bias b WHERE b.city=f.city
                          AND b.variable=f.variable AND b.target_date=f.target_date
                          AND b.model='ensemble'),
                     NOT EXISTS (SELECT 1 FROM bias b WHERE b.city=f.city
                          AND b.variable=f.variable AND b.target_date=f.target_date
                          AND b.model='station')
                   FROM forecast_log f
                   WHERE f.target_date <= ? AND f.target_date >= ?""",
                (str(before), str(oldest)),
            ).fetchall()
        # Keep only rows that still need at least one leg written.
        return [r for r in rows if r[4] or r[5]]

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

    def _combined_errors(self, variable: str, city: Optional[str] = None,
                         source: str = "om") -> List[float]:
        """
        Errors of the COMBINED ensemble mean, one value per (city, date).

        ``source`` picks the ground truth:
          "om"      — Open-Meteo grid observations: rows tagged
                      model='ensemble' merged with legacy rows (which
                      duplicated the combined mean under every model name;
                      averaging per (city, date) reproduces the combined
                      error exactly). model='station' rows are EXCLUDED.
          "station" — settlement-station METAR observations (model='station'
                      rows, recorded in parallel since 2026-07-11). The
                      2026-07-11 audit showed OM lands in the winning bucket
                      only 26% of the time — station rows measure error
                      against the quantity that actually pays.

        Merging is per (city, date) with ensemble rows winning over legacy:
        an all-or-nothing preference would let the FIRST post-fix ensemble
        row hide months of legacy history (error_std → None, EMOS stuck on
        the KDE fallback until new rows accumulate).
        """
        where_city = " AND city=?" if city else ""
        args: tuple = (variable, city) if city else (variable,)
        with sqlite3.connect(self._db) as c:
            if source == "station":
                rows = c.execute(
                    f"SELECT city, target_date, error FROM bias "
                    f"WHERE variable=? AND model='station'{where_city}",
                    args,
                ).fetchall()
                return [r[2] for r in rows if r[2] is not None]
            # Legacy branch is CAPPED at the 2026-07-10 recorder fix: before it,
            # per-model rows duplicated the combined mean (AVG reproduces it
            # exactly); after it, per-model rows carry each model's OWN day-of
            # mean and same-day trades deliberately write NO ensemble row —
            # without the cap their AVG leaks day-of-lead samples into the
            # combined-error pool, re-opening the sigma self-sharpening loop
            # the same-day exclusion exists to prevent (review 2026-07-20).
            legacy = c.execute(
                f"""SELECT city, target_date, AVG(error) FROM bias
                    WHERE variable=? AND model NOT IN ('ensemble','station')
                      AND target_date < '2026-07-10'{where_city}
                    GROUP BY city, target_date""",
                args,
            ).fetchall()
            ens = c.execute(
                f"SELECT city, target_date, error FROM bias "
                f"WHERE variable=? AND model='ensemble'{where_city}",
                args,
            ).fetchall()
        merged = {(r[0], r[1]): r[2] for r in legacy}
        merged.update({(r[0], r[1]): r[2] for r in ens})
        return [v for v in merged.values() if v is not None]

    def error_std(self, variable: str = "temperature",
                  min_samples: int = MIN_DISPERSION_SAMPLES,
                  source: str = "om") -> Optional[float]:
        """
        Global std of realized combined-mean forecast errors — the floor the
        forecast spread should not undercut. Returns None if too few samples.
        source='station' falls back to 'om' while station history is thin.
        """
        errs = self._combined_errors(variable, source=source)
        if len(errs) < min_samples and source == "station":
            errs = self._combined_errors(variable, source="om")
        if len(errs) < min_samples:
            return None
        return float(np.std(errs))

    def city_error_sigma(self, city: str, variable: str = "temperature",
                         shrink_k: int = 10,
                         min_global: int = MIN_DISPERSION_SAMPLES,
                         source: str = "om") -> Optional[float]:
        """
        Per-city predictive sigma for the EMOS engine, shrunk toward the
        global error std by w = n/(n+k). Per-city stds ranged 1.0–3.8°F on
        2026-07-10, so a single global sigma is wrong in both directions:
        underconfident in tight cities, overconfident in volatile ones.

        source='station' measures against settlement-station ground truth and
        silently falls back to 'om' while the station history is too thin.
        Returns None when even the OM history is too thin — callers must
        fall back to the KDE/dispersion-floor path.
        """
        all_errs = self._combined_errors(variable, source=source)
        if len(all_errs) < min_global and source == "station":
            source = "om"
            all_errs = self._combined_errors(variable, source=source)
        if len(all_errs) < min_global:
            return None
        g_var = float(np.var(all_errs))
        city_errs = self._combined_errors(variable, city=city, source=source)
        n = len(city_errs)
        if n < 2:
            return float(np.sqrt(g_var))
        w = n / (n + shrink_k)
        return float(np.sqrt(w * float(np.var(city_errs)) + (1 - w) * g_var))


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


_MAX_FORECAST_DAYS = 16  # Open-Meteo ensemble horizon

@http_retry
def _fetch_ensemble_vars(
    lat: float, lon: float, target_date: date, model: str, variables: str
) -> Optional[Dict[str, List[float]]]:
    """
    Generic Open-Meteo ensemble fetch.
    Returns {variable_key: [member_values]} or None on failure.
    """
    days_ahead = (target_date - date.today()).days
    if days_ahead > _MAX_FORECAST_DAYS:
        logger.debug(f"Skipping {model}/{variables} for {target_date} ({days_ahead}d > {_MAX_FORECAST_DAYS}d horizon)")
        return None

    cache_key = (round(lat, 3), round(lon, 3), str(target_date), model, variables)
    now = time.time()
    with _forecast_cache_lock:
        hit = _forecast_cache.get(cache_key)
        if hit is not None and (now - hit[0]) < _FORECAST_TTL_S:
            return hit[1]

    # Breaker tripped: skip the network entirely (no cached value available).
    if _circuit_is_open():
        return None

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
            for attempt in range(4):
                resp = client.get(OPEN_METEO_ENSEMBLE_URL, params=params)
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.debug(f"Open-Meteo 429 — backing off {wait}s [{model}/{variables}]")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            else:
                logger.warning(f"Open-Meteo persistently rate-limited [{model}/{variables}]")
                _circuit_record_failure()
                return None
            _circuit_record_success()
            data = resp.json()

        daily = data.get("daily", {})
        result: Dict[str, List[float]] = {}
        for key, values in daily.items():
            if key == "time" or not values:
                continue
            val = values[0]
            if val is not None:
                try:
                    result.setdefault(key.split("_member")[0], []).append(float(val))
                except (TypeError, ValueError):
                    pass

        if result:
            with _forecast_cache_lock:
                _forecast_cache[cache_key] = (now, result)
        return result if result else None
    except Exception as e:
        logger.error(f"Open-Meteo fetch failed [{model}/{variables}] ({lat},{lon}) {target_date}: {e}")
        return None


# ── KDE fitting ───────────────────────────────────────────────────────────────

def _inflate_dispersion(members: List[float], target_std: float) -> List[float]:
    """
    Scale ensemble members about their mean so the spread is at least ``target_std``.

    Weather ensembles are chronically *under-dispersed*: the spread across members
    understates the true forecast error. Audit of the bias table showed claimed
    spread ~1.56°F vs realized error ~2.34°F (mean error ~0), i.e. ~1.5× too
    confident — the root of the single-degree-bucket overconfidence. We never
    *narrow* a forecast (factor floored at 1.0); we only widen ones tighter than
    our demonstrated track record. Mean is preserved (it's already ~unbiased).
    """
    if target_std <= 0 or len(members) < 2:
        return members
    arr = np.asarray(members, dtype=float)
    cur = float(arr.std())
    if cur <= 1e-6 or target_std <= cur:
        return members
    mu = float(arr.mean())
    factor = target_std / cur
    return (mu + (arr - mu) * factor).tolist()


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

def _conf_cap() -> float:
    """Maximum confidence. Below 1.0 so the time-decay multiplier stays meaningful."""
    return float(settings.confidence_max_cap)


def _temp_confidence(combined_std_f: float, n_models: int) -> float:
    spread_conf = float(np.clip(1.0 - (combined_std_f - 2.0) / 8.0, 0.1, 1.0))
    model_bonus = min(0.1 * (n_models - 1), 0.2)
    return float(np.clip(spread_conf + model_bonus, 0.0, _conf_cap()))


def _wind_confidence(std_mph: float, n_models: int) -> float:
    """
    Confidence for wind speed forecasts. Wind ensemble spread of ~5 mph = high confidence;
    ~25 mph = near-minimum confidence. Maps [5, 25] mph std → [0.9, 0.1].
    """
    spread_conf = float(np.clip(1.0 - (std_mph - 5.0) / 20.0, 0.1, 1.0))
    model_bonus = min(0.1 * (n_models - 1), 0.2)
    return float(np.clip(spread_conf + model_bonus, 0.0, _conf_cap()))


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
    return float(np.clip(decisiveness + spread_bonus + model_bonus, 0.0, _conf_cap()))


# ── Temperature forecast ──────────────────────────────────────────────────────

def get_ensemble_forecast(
    city: str,
    lat: float,
    lon: float,
    target_date: date,
    models: Optional[List[str]] = None,
    use_bias_correction: bool = True,
    allow_intraday: bool = True,
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

        logger.debug(f"  [{city}] temp/{model}: {len(raw_c)} members, mean={mean_f:.1f}°F, std={std_f:.1f}°F, bias={bias:+.1f}°F")

    if not all_weighted:
        logger.warning(f"No temperature forecast for {city} on {target_date}")
        return None

    # Variance inflation: widen the ensemble to match realized forecast error
    # before deriving bucket probabilities. Corrects chronic under-dispersion that
    # otherwise dumps ~90%+ mass onto a single-degree bucket. Floor is fit from the
    # bias table once enough errors exist, else a conservative default.
    if use_bias_correction:
        floor = (_bias_store.error_std("temperature", source=settings.ground_truth_source)
                 or _DEFAULT_DISPERSION_F)
        pre_std = float(np.std(all_weighted))
        all_weighted = _inflate_dispersion(all_weighted, floor)
        post_std = float(np.std(all_weighted))
        if post_std > pre_std + 1e-6:
            logger.debug(
                f"  [{city}] dispersion inflated {pre_std:.2f}→{post_std:.2f}°F (floor={floor:.2f})"
            )

    # Intraday refresh: when the target date is today and we already have
    # observed high-temp-so-far, the realized high is bounded below by it.
    # Tighten the ensemble around (max-so-far, ensemble-of-remaining) to
    # reduce variance in the final hours.
    # allow_intraday=False: callers needing a PURE day-ahead mean (the daily
    # forecast logger) — the clamp compares against box-LOCAL today, so on a
    # tz-ahead-of-UTC box "UTC tomorrow" can equal local today and silently
    # contaminate a snapshot that first-write-wins then makes permanent.
    if allow_intraday and target_date == date.today():
        intraday = _intraday_high_so_far(lat, lon)
        if intraday is not None:
            # Discard members that are below what already happened.
            tightened = [max(intraday, v) for v in all_weighted]
            all_weighted = tightened
            # The EMOS Gaussian needs the same lower bound the members just
            # received — it censors its CDF at this floor.
            result.intraday_floor_f = intraday

    result.combined_members_f = all_weighted
    result.combined_kde = _fit_kde(all_weighted)
    result.mean_f = float(np.mean(all_weighted))
    result.std_f = float(np.std(all_weighted))
    result.confidence = _temp_confidence(result.std_f, len(result.model_results))

    # EMOS engine: swap the second moment for the city's realized error sigma.
    # Falls back to KDE silently when the bias history is too thin to fit.
    engine = "kde"
    if settings.forecast_engine == "emos":
        sigma = _bias_store.city_error_sigma(city, source=settings.ground_truth_source)
        if sigma is not None:
            result.emos_sigma_f = max(sigma, 0.8)  # never sharper than 0.8°F
            engine = "emos"
        else:
            logger.debug(f"  [{city}] EMOS requested but bias history too thin — KDE fallback")

    logger.info(
        f"Temp forecast {city} {target_date} [{engine}]: mean={result.mean_f:.1f}°F "
        f"std={result.std_f:.1f}°F"
        + (f" emos_sigma={result.emos_sigma_f:.2f}°F" if result.emos_sigma_f else "")
        + f" conf={result.confidence:.2f} "
        f"models={[m.model_name for m in result.model_results]}"
    )
    return result


def _intraday_high_so_far(lat: float, lon: float) -> Optional[float]:
    """
    Fetch hourly temperatures so far today and return the max in °F. Used to
    tighten the day-of forecast distribution by discarding ensemble members
    that fall below what's already been observed.
    """
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "temperature_2m",
                    "forecast_days": 1,
                    "past_days": 0,
                    "timezone": "UTC",
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json().get("hourly", {})
            times = data.get("time", [])
            temps = data.get("temperature_2m", [])
            if not times or not temps:
                return None
            now = time.gmtime()
            today_str = f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}"
            past_today = [
                celsius_to_fahrenheit(t)
                for ts, t in zip(times, temps)
                if t is not None and ts.startswith(today_str) and ts <= time.strftime("%Y-%m-%dT%H:00", now)
            ]
            return float(max(past_today)) if past_today else None
    except Exception:
        return None


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


# ── Wind speed forecast ───────────────────────────────────────────────────────

def get_wind_forecast(
    city: str,
    lat: float,
    lon: float,
    target_date: date,
    models: Optional[List[str]] = None,
    use_bias_correction: bool = True,
) -> Optional[WindForecast]:
    """
    Fetch wind_speed_10m_max ensemble, convert kph→mph, apply bias correction,
    fit KDE, return WindForecast.

    Args:
        city: City name for logging and bias lookup.
        lat: Latitude.
        lon: Longitude.
        target_date: The date to forecast.
        models: Override model list (defaults to settings.ensemble_model_list).
        use_bias_correction: Whether to apply stored bias corrections.

    Returns:
        WindForecast with KDE fitted over mph members, or None if no data.
    """
    if models is None:
        models = settings.ensemble_model_list

    result = WindForecast(city=city, target_date=target_date)
    all_weighted_mph: List[float] = []

    for model, data in _fetch_models_parallel(lat, lon, target_date, models, "wind_speed_10m_max"):
        if not data:
            continue

        raw_kph = data.get("wind_speed_10m_max", [])
        if not raw_kph:
            continue

        raw_mph = [max(0.0, v * KPH_TO_MPH) for v in raw_kph]

        bias = 0.0
        if use_bias_correction and settings.bias_correction_days > 0:
            bias = _bias_store.get_correction(city, model, "wind_speed", settings.bias_correction_days)

        corrected_mph = [max(0.0, v + bias) for v in raw_mph]

        w = DEFAULT_MODEL_WEIGHTS.get(model, 0.8)
        repeats = max(1, round(w * 10))
        all_weighted_mph.extend(corrected_mph * repeats)

        mean_mph = float(np.mean(corrected_mph))
        logger.debug(
            f"  wind/{model}: {len(raw_kph)} members, mean={mean_mph:.1f}mph, bias={bias:+.1f}mph"
        )

    if not all_weighted_mph:
        logger.warning(f"No wind forecast for {city} on {target_date}")
        return None

    arr = np.array(all_weighted_mph)
    result.combined_members_mph = all_weighted_mph
    result.mean_f = float(np.mean(arr))
    result.std_f = float(np.std(arr))
    result.combined_kde = _fit_kde(all_weighted_mph)
    result.confidence = _wind_confidence(result.std_f, len(models))

    logger.info(
        f"Wind forecast {city} {target_date}: mean={result.mean_f:.1f}mph "
        f"std={result.std_f:.1f}mph conf={result.confidence:.2f}"
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


def record_observed_wind(city: str, model: str, target_date: date,
                         forecast_mean_mph: float, observed_mph: float) -> None:
    """Record observed wind speed for 30-day rolling bias correction."""
    _bias_store.record(city, model, "wind_speed", target_date, forecast_mean_mph, observed_mph)
    logger.info(
        f"Bias recorded [wind]: {city}/{model} "
        f"forecast={forecast_mean_mph:.1f}mph actual={observed_mph:.1f}mph "
        f"error={observed_mph - forecast_mean_mph:+.1f}mph"
    )
