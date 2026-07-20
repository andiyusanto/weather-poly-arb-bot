"""
Record observed-vs-forecast errors for resolved trades so the rolling bias
correction in ``forecast.py`` has data to work with.

Called from ``trader.resolve_open_trades`` whenever a trade resolves (shadow
or live — both contribute equally valid ground truth to the bias table).
Fetches the actual observed weather value from Open-Meteo's archive endpoint
and stores ``observed - forecast`` per (city, model, variable, target_date).
"""

from __future__ import annotations

import json
import time
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Optional

import httpx
from loguru import logger

from config.settings import CITIES_CACHE_DB, settings
from src.forecast import _bias_store
from src.utils import GeoCache, celsius_to_fahrenheit

_geo = GeoCache(CITIES_CACHE_DB)

# Variable → Open-Meteo archive daily field name.
_ARCHIVE_VAR = {
    "temperature":   "temperature_2m_max",
    "precipitation": "precipitation_sum",
    "snowfall":      "snowfall_sum",
    "wind_speed":    "wind_speed_10m_max",
}

# Map our market_type → bias-store variable key.
_BIAS_VAR = {
    "temperature":   "temperature",
    "precipitation": "precipitation",
    "snowfall":      "snowfall",
    "wind_speed":    "wind_speed",
}

# Observed daily values are read from the *forecast* host's past-date range, NOT
# archive-api.open-meteo.com. The archive host resolves IPv6-first and is
# unreachable from the IPv4-only APAC trading zones (ENETUNREACH), whereas
# api.open-meteo.com is reachable (~0.1s) and returns the identical daily schema.
# It serves recent past dates (well within the days-old window we resolve over).
OBSERVED_URL = "https://api.open-meteo.com/v1/forecast"

# An observation depends only on (lat, lon, date, variable) — never on the bucket.
# Many shadow trades share the same city+date, so during a backfill the same
# observation is requested dozens of times. Memoize per-process to collapse those
# into a single Open-Meteo call (the archive API is aggressively rate-limited).
_observed_cache: dict[tuple, Optional[float]] = {}


def _fetch_observed(lat: float, lon: float, target: date_type, variable: str) -> Optional[float]:
    """
    Fetch a single daily observation from the Open-Meteo archive.

    Memoized by (lat, lon, date, field); on HTTP 429 backs off and retries a few
    times rather than stalling to the socket timeout. Returns None on miss/failure
    (None results are NOT cached, so a transient rate-limit can be retried later).
    """
    field = _ARCHIVE_VAR.get(variable)
    if not field:
        return None

    key = (round(lat, 4), round(lon, 4), str(target), field)
    if key in _observed_cache:
        return _observed_cache[key]

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": str(target),
        "end_date": str(target),
        "daily": field,
        "timezone": "UTC",
    }
    try:
        with httpx.Client(timeout=15) as client:
            for attempt in range(3):
                resp = client.get(OBSERVED_URL, params=params)
                if resp.status_code == 429:
                    wait = 2 * (attempt + 1)
                    logger.debug(f"archive 429 for {variable}/{target}; backoff {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.debug(f"archive fetch HTTP {resp.status_code} for {variable}/{target}")
                    return None
                data = resp.json().get("daily", {}).get(field, [])
                if not data or data[0] is None:
                    return None
                value = float(data[0])
                _observed_cache[key] = value  # cache only successful reads
                return value
            logger.debug(f"archive persistently rate-limited for {variable}/{target}")
            return None
    except Exception as e:
        logger.debug(f"archive fetch failed: {e}")
        return None


def record_bias_for_resolved_trade(trade: dict) -> bool:
    """
    Compute and store the bias (observed - forecast) for one resolved trade.
    Returns True on success.
    """
    city = trade.get("city") or ""
    target_str = trade.get("target_date") or ""
    market_type = (trade.get("market_type") or "").lower()
    forecast_mean = trade.get("forecast_mean")

    if not city or not target_str or forecast_mean is None:
        return False

    try:
        target = datetime.strptime(target_str, "%Y-%m-%d").date()
    except ValueError:
        return False

    geo = _geo.get(city)
    if not geo:
        return False

    variable = _BIAS_VAR.get(market_type)
    if not variable:
        return False

    observed = _fetch_observed(geo["lat"], geo["lon"], target, variable)
    if observed is None:
        return False

    # Forecast mean for temperature is stored in °F; convert observed °C → °F to match.
    if market_type == "temperature":
        observed = celsius_to_fahrenheit(observed)

    # Combined-mean row: the ground truth for EMOS sigma and dispersion floor.
    # SKIPPED for same-day trades: their combined mean was tightened with the
    # intraday max-so-far, so the error is structurally tiny — feeding those
    # rows into city_error_sigma would shrink the EMOS sigma below the true
    # day-ahead error as same-day volume accumulates (self-sharpening loop).
    # Per-model rows below are safe: model means are computed BEFORE the clamp.
    trade_ts = str(trade.get("timestamp") or "")
    same_day_trade = trade_ts[:10] == target_str
    if not same_day_trade:
        _bias_store.record(
            city=city, model="ensemble", variable=variable, target_date=target,
            forecast_mean=float(forecast_mean), observed=float(observed),
        )
        # Parallel SETTLEMENT ground truth (model='station'): the same combined
        # mean scored against the named airport station's METAR daily max —
        # what the market actually settles on. Written alongside the OM row so
        # the corrected bias/sigma tables warm up without touching live
        # probabilities; GROUND_TRUTH_SOURCE=station flips consumers later.
        # (Temperature only — no station mapping for other variables.)
        if variable == "temperature":
            from src.station_obs import fetch_station_daily_max_f, station_for_city
            icao = station_for_city(city)
            tz = geo.get("timezone") or ""
            if icao and tz:
                st_obs = fetch_station_daily_max_f(icao, target, tz)
                if st_obs is not None:
                    _bias_store.record(
                        city=city, model="station", variable=variable,
                        target_date=target, forecast_mean=float(forecast_mean),
                        observed=float(st_obs),
                    )
                    logger.info(
                        f"station bias recorded {city}/{icao} {target}: "
                        f"station={st_obs:.2f} vs om={observed:.2f} "
                        f"(Δ={st_obs - observed:+.2f})"
                    )

    # Per-model rows: use each model's OWN mean when the trade carried it
    # (model_means JSON, recorded since 2026-07-10). Before that fix the
    # combined mean was duplicated under every model name, which made
    # per-model bias correction a no-op and BMA weights unfittable.
    per_model = {}
    raw_means = trade.get("model_means")
    if raw_means:
        try:
            per_model = {m: float(v) for m, v in json.loads(raw_means).items()}
        except (ValueError, TypeError, AttributeError) as e:
            # AttributeError: valid JSON that isn't an object (list/str/number)
            # has no .items() — must not abort the remaining rows mid-write.
            logger.warning(f"unparseable model_means for {city}/{target}: {e}")
    if per_model:
        for model, mean in per_model.items():
            _bias_store.record(
                city=city, model=model, variable=variable, target_date=target,
                forecast_mean=mean, observed=float(observed),
            )
    else:
        # Legacy trades without per-model means: keep the old duplication so
        # per-model rolling bias still receives (combined-mean) signal.
        for model in settings.ensemble_model_list:
            _bias_store.record(
                city=city, model=model, variable=variable, target_date=target,
                forecast_mean=float(forecast_mean), observed=float(observed),
            )

    logger.info(
        f"bias recorded {city}/{variable} {target}: "
        f"forecast={forecast_mean:.2f} observed={observed:.2f} "
        f"err={observed - forecast_mean:+.2f}"
        + (f" (+{len(per_model)} per-model rows)" if per_model else "")
    )
    return True


RESOLVE_MAX_AGE_DAYS = 14  # abandon snapshots older than this (OM past-date limit)


# ── Daily forecast logger ─────────────────────────────────────────────────────
# The funnel's trades are nearly all same-day (intraday-clamped means, excluded
# from bias recording by design), so trade-driven bias/sigma growth stalled at
# the 2026-07-11 backfill. These two functions grow the ground-truth tables
# every day regardless of trading: snapshot tomorrow's forecast mean per
# allowlist city (first write wins → consistent ~24h lead, never clamped),
# then score past snapshots against OM + settlement-station observations once
# the target's local day has fully elapsed everywhere.
# Both are idempotent and best-effort; they piggyback on the resolve cycle.

def _snapshot_cities() -> set:
    """Cities to snapshot: allowlist, plus every station-mapped city when
    FORECAST_LOG_ALL_CITIES (per-city skill vs settlement is the instrument
    future allowlist revisions need — market-price city tables are noise)."""
    from src.station_obs import mapped_cities
    cities = set(settings.city_allowlist_set)
    if settings.forecast_log_all_cities:
        cities |= mapped_cities()
    return cities


def _snapshot_one_lead(cities: set, target, lead: str) -> tuple[int, list]:
    """Snapshot one (target, lead) bucket across cities. First-write-wins per
    (city, target, lead); already-logged cities skipped BEFORE any fetch."""
    from src.forecast import get_ensemble_forecast

    now = datetime.now(timezone.utc)
    # Rough hours-to-end-of-target-day, diagnostic only (the bucket is fixed).
    lead_hours = ((datetime(target.year, target.month, target.day, tzinfo=timezone.utc)
                   + timedelta(days=1)) - now).total_seconds() / 3600.0
    done = {c.lower() for c in _bias_store.logged_cities("temperature", target, lead)}
    n_new, skipped = 0, []
    for key in sorted(cities):
        if key.lower() in done:
            continue
        geo = _geo.get(key)
        if not geo:
            skipped.append(key)
            continue
        # Match the market classifier's naming (.title()) so logger rows and
        # trade rows merge in per-city queries.
        city = key.title()
        try:
            # allow_intraday=False everywhere: the clamp compares against
            # box-local today and would contaminate a first-write-wins
            # snapshot; the same-day sigma must be pure forecast error, the
            # intraday floor is handled separately at bucket_probability time.
            fc = get_ensemble_forecast(city, geo["lat"], geo["lon"], target,
                                       allow_intraday=False)
        except Exception as e:  # noqa: BLE001 — logging must never break resolve
            logger.warning(f"forecast snapshot failed for {city}/{target}/{lead}: {e}")
            continue
        mean_f = getattr(fc, "mean_f", None) if fc else None
        if mean_f is not None:  # explicit: 0.0°F is a real winter mean, not "missing"
            if _bias_store.log_forecast(city, "temperature", target, float(mean_f),
                                        lead=lead, lead_hours=round(lead_hours, 1)):
                n_new += 1
    return n_new, skipped


def snapshot_daily_forecasts() -> int:
    """Log the combined forecast mean at BOTH leads for every snapshot city.

    day_ahead (target = UTC-tomorrow, ~24h lead) and same_day (target =
    UTC-today, ~6–18h lead) give the two calibration points for a
    lead-dependent EMOS sigma — the same_day one prices our same-day funnel
    trades correctly instead of over-widening them with a day-ahead σ.
    """
    from src.forecast import LEAD_DAY_AHEAD, LEAD_SAME_DAY

    cities = _snapshot_cities()
    if not cities:
        return 0
    now = datetime.now(timezone.utc)
    n_new = 0
    skipped: set = set()
    for target, lead in ((now.date() + timedelta(days=1), LEAD_DAY_AHEAD),
                         (now.date(), LEAD_SAME_DAY)):
        n, sk = _snapshot_one_lead(cities, target, lead)
        n_new += n
        skipped |= set(sk)
        if n:
            logger.info(f"forecast log: snapshotted {n} city means for {target} [{lead}]")
    if skipped:
        # Mapped cities absent from the geocache never get snapshotted — the
        # scanner only geocodes allowlist markets, so the all-cities skill
        # sample is silently partial without this.
        logger.warning(f"forecast log: {len(skipped)} mapped cities not in "
                       f"geocache, skipped: {sorted(skipped)}")
    return n_new


def resolve_forecast_logs() -> int:
    """Score elapsed snapshots against OM + station; write lead-tagged bias
    rows (day_ahead → ensemble/station, same_day → *@sameday)."""
    from src.forecast import om_bias_model, station_bias_model
    from src.station_obs import fetch_station_daily_max_f, station_for_city

    # Local day D has ended everywhere once UTC reaches ~D+1 12:00 (UTC-12
    # extreme). now-36h lands on D exactly then — the cheap universal guard.
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=36)).date()
    # OM's past-date endpoint stops serving old days; cap the retry window so
    # unresolvable snapshots are abandoned, not re-fetched forever.
    oldest = (now - timedelta(days=RESOLVE_MAX_AGE_DAYS)).date()
    pending = _bias_store.pending_forecast_logs(cutoff, oldest)
    n_done = 0
    for city, variable, target_str, lead, mean_f, need_om, need_station in pending:
        geo = _geo.get(city)
        if not geo:
            continue
        target = datetime.strptime(target_str, "%Y-%m-%d").date()
        wrote = False
        if need_om:
            observed = _fetch_observed(geo["lat"], geo["lon"], target, variable)
            if observed is not None:
                observed_f = (celsius_to_fahrenheit(observed)
                              if variable == "temperature" else float(observed))
                _bias_store.record(city=city, model=om_bias_model(lead),
                                   variable=variable, target_date=target,
                                   forecast_mean=mean_f, observed=observed_f)
                wrote = True
        if need_station and variable == "temperature":
            icao = station_for_city(city)
            tz = geo.get("timezone") or ""
            if icao and tz:
                st_obs = fetch_station_daily_max_f(icao, target, tz)
                if st_obs is not None:
                    _bias_store.record(city=city, model=station_bias_model(lead),
                                       variable=variable, target_date=target,
                                       forecast_mean=mean_f, observed=float(st_obs))
                    wrote = True
        if wrote:
            n_done += 1
    if n_done:
        logger.info(f"forecast log: resolved {n_done} pending snapshots")
    return n_done
