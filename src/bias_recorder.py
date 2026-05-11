"""
Record observed-vs-forecast errors for resolved trades so the rolling bias
correction in ``forecast.py`` has data to work with.

Called from ``trader.resolve_shadow_trades`` whenever a shadow trade resolves.
Fetches the actual observed weather value from Open-Meteo's archive endpoint
and stores ``observed - forecast`` per (city, model, variable, target_date).
"""

from __future__ import annotations

from datetime import date as date_type, datetime
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

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def _fetch_observed(lat: float, lon: float, target: date_type, variable: str) -> Optional[float]:
    """Fetch a single daily observation from the Open-Meteo archive."""
    field = _ARCHIVE_VAR.get(variable)
    if not field:
        return None
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(ARCHIVE_URL, params={
                "latitude": lat,
                "longitude": lon,
                "start_date": str(target),
                "end_date": str(target),
                "daily": field,
                "timezone": "UTC",
            })
            if resp.status_code != 200:
                logger.debug(f"archive fetch HTTP {resp.status_code} for {variable}/{target}")
                return None
            data = resp.json().get("daily", {}).get(field, [])
            if not data:
                return None
            v = data[0]
            return float(v) if v is not None else None
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

    # Store one bias entry per ensemble model so the rolling correction has
    # per-model history. We use a single combined entry tagged "ensemble" —
    # this is a coarse approximation but better than no signal.
    for model in settings.ensemble_model_list:
        _bias_store.record(
            city=city,
            model=model,
            variable=variable,
            target_date=target,
            forecast_mean=float(forecast_mean),
            observed=float(observed),
        )

    logger.info(
        f"bias recorded {city}/{variable} {target}: "
        f"forecast={forecast_mean:.2f} observed={observed:.2f} "
        f"err={observed - forecast_mean:+.2f}"
    )
    return True
