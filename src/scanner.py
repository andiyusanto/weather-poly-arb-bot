"""
Scanner — orchestrates market discovery, geocoding, forecast fetching, and
opportunity surfacing across temperature, precipitation, and snowfall markets.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import yaml
from geopy.exc import GeocoderServiceError
from geopy.geocoders import Nominatim
from loguru import logger
from rich.console import Console
from rich.table import Table

from config.settings import CITIES_CACHE_DB, CITIES_YAML, settings
from src.forecast import (
    AnyForecast,
    EnsembleForecast,
    get_ensemble_forecast,
    get_precip_forecast,
    get_snow_forecast,
)
from src.polymarket_client import (
    MarketType,
    WeatherBucket,
    WeatherMarket,
    enrich_with_prices,
    fetch_weather_markets,
)
from src.strategy import Opportunity, evaluate_market
from src.utils import GeoCache, fmt_pct, fmt_usdc, hours_until, rate_limited_sleep

console = Console()
_geo_cache = GeoCache(CITIES_CACHE_DB)
_geolocator = Nominatim(user_agent="polymarket_weather_bot/1.0")


# ── Geocoding ─────────────────────────────────────────────────────────────────

def _geocode_city(city: str) -> Optional[Dict]:
    cached = _geo_cache.get(city)
    if cached:
        return cached

    logger.debug(f"Geocoding: {city}")
    rate_limited_sleep(1.1)

    try:
        location = _geolocator.geocode(city, addressdetails=True, language="en")
        if not location:
            logger.warning(f"Could not geocode: {city}")
            return None

        lat = location.latitude
        lon = location.longitude
        country = location.raw.get("address", {}).get("country_code", "").upper()

        import httpx
        try:
            with httpx.Client(timeout=10) as client:
                tz_resp = client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1},
                )
                tz_name = tz_resp.json().get("timezone", "UTC")
        except Exception:
            tz_name = "UTC"

        geo = dict(lat=lat, lon=lon, timezone=tz_name, country=country, display_name=str(location))
        _geo_cache.set(city, lat=lat, lon=lon, timezone=tz_name, country=country, display_name=str(location))
        logger.info(f"Geocoded {city}: {lat:.4f},{lon:.4f} tz={tz_name}")
        return geo

    except GeocoderServiceError as e:
        logger.error(f"Geocoder error for {city}: {e}")
        return None


def _load_priority_cities() -> None:
    if not CITIES_YAML.exists():
        return
    try:
        with open(CITIES_YAML) as f:
            data = yaml.safe_load(f)
        for city_def in data.get("priority_cities", []):
            name = city_def["name"]
            if not _geo_cache.get(name):
                _geo_cache.set(
                    name,
                    lat=city_def["lat"],
                    lon=city_def["lon"],
                    timezone=city_def["timezone"],
                    country=city_def.get("country", ""),
                    display_name=name,
                    priority=True,
                )
    except Exception as e:
        logger.warning(f"Failed to load cities.yaml: {e}")


# ── Forecast dispatch ─────────────────────────────────────────────────────────

def _fetch_forecast(
    city: str, lat: float, lon: float,
    target_date: date, market_type: MarketType,
) -> Optional[AnyForecast]:
    """Route to the correct forecast function based on market type."""
    if market_type == MarketType.TEMPERATURE:
        return get_ensemble_forecast(city=city, lat=lat, lon=lon, target_date=target_date)
    if market_type == MarketType.PRECIPITATION:
        return get_precip_forecast(city=city, lat=lat, lon=lon, target_date=target_date)
    if market_type == MarketType.SNOWFALL:
        return get_snow_forecast(city=city, lat=lat, lon=lon, target_date=target_date)
    return None


# ── ScanResult ────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    opportunities: List[Opportunity]
    markets_scanned: int
    cities_resolved: int
    errors: int
    scan_duration_s: float
    # Per-type counts for summary display
    type_counts: Dict[str, int] = field(default_factory=dict)
    type_opp_counts: Dict[str, int] = field(default_factory=dict)


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan(
    min_ev: Optional[float] = None,
    min_confidence: Optional[float] = None,
    max_hours: Optional[float] = None,
    dry_run: bool = True,
    enabled_types: Optional[set] = None,
) -> ScanResult:
    """
    Full scan cycle across all enabled weather market types:
      1. Load priority cities into geocache
      2. Discover all active markets from Gamma (temp + precip + snow)
      3. Filter by time-to-resolution window
      4. Enrich with live CLOB prices
      5. Geocode new cities
      6. Fetch type-specific ensemble forecasts (cached per city/date/type)
      7. Evaluate EV on every bucket
      8. Return sorted opportunities
    """
    t0 = time.time()
    _load_priority_cities()

    min_ev = min_ev if min_ev is not None else settings.min_ev_threshold
    min_confidence = min_confidence if min_confidence is not None else settings.min_confidence
    max_hours = max_hours if max_hours is not None else settings.max_hours_to_resolution
    if enabled_types is None:
        enabled_types = settings.enabled_market_type_set

    errors = 0
    cities_resolved = 0

    logger.info(f"Discovering markets [types: {enabled_types}]...")
    markets = fetch_weather_markets(enabled_types=enabled_types)

    if not markets:
        logger.warning("No weather markets found")
        return ScanResult([], 0, 0, 0, time.time() - t0)

    # Filter by time window
    active_markets: List[WeatherMarket] = []
    for m in markets:
        if m.resolution_datetime:
            h = hours_until(m.resolution_datetime)
            if h < -6:
                logger.debug(f"Skipping {m.city}/{m.market_type.value} (resolved {-h:.0f}h ago)")
                continue
            if h > max_hours:
                logger.debug(f"Skipping {m.city}/{m.market_type.value} ({h:.0f}h > {max_hours}h)")
                continue
        active_markets.append(m)

    type_counts: Dict[str, int] = {}
    for m in active_markets:
        k = m.market_type.value
        type_counts[k] = type_counts.get(k, 0) + 1

    logger.info(
        f"{len(active_markets)}/{len(markets)} markets within {max_hours}h: "
        + ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items()))
    )

    # Enrich with live prices
    active_markets = enrich_with_prices(active_markets)

    # Geocode new cities
    city_geo: Dict[str, Optional[Dict]] = {}
    for m in active_markets:
        if m.city not in city_geo:
            geo = _geocode_city(m.city)
            city_geo[m.city] = geo
            if geo:
                cities_resolved += 1

    # Collect unique (city, date, market_type) keys to avoid duplicate fetches
    forecast_keys: Dict[Tuple[str, date, MarketType], tuple] = {}
    for market in active_markets:
        geo = city_geo.get(market.city)
        if not geo:
            logger.warning(f"No geo data for {market.city} — skipping")
            errors += 1
            continue
        key = (market.city, market.target_date, market.market_type)
        if key not in forecast_keys:
            forecast_keys[key] = (market.city, geo["lat"], geo["lon"], market.target_date, market.market_type)

    # Fetch all forecasts in parallel
    def _fetch_key(item: tuple):
        key, (city, lat, lon, target_date, market_type) = item
        try:
            return key, _fetch_forecast(city=city, lat=lat, lon=lon,
                                        target_date=target_date, market_type=market_type)
        except Exception as e:
            logger.error(f"Forecast error [{market_type.value}] {city}: {e}")
            return key, None

    n_keys = len(forecast_keys)
    workers = min(settings.max_concurrency, n_keys) if n_keys else 1
    logger.info(f"Fetching {n_keys} forecasts with {workers} parallel workers...")
    t_forecast = time.time()

    forecasts: Dict[Tuple[str, date, MarketType], Optional[AnyForecast]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, result in pool.map(_fetch_key, list(forecast_keys.items())):
            forecasts[key] = result
            if result is None:
                errors += 1

    logger.info(f"Forecasts complete: {n_keys} fetched in {time.time() - t_forecast:.1f}s")

    opportunities: List[Opportunity] = []
    for market in active_markets:
        if not city_geo.get(market.city):
            continue
        key = (market.city, market.target_date, market.market_type)
        forecast = forecasts.get(key)
        if forecast is None:
            continue
        market_opps = evaluate_market(
            market, forecast, min_ev=min_ev, min_confidence=min_confidence
        )
        opportunities.extend(market_opps)

    opportunities.sort(key=lambda o: o.ev, reverse=True)

    type_opp_counts: Dict[str, int] = {}
    for o in opportunities:
        k = o.market.market_type.value
        type_opp_counts[k] = type_opp_counts.get(k, 0) + 1

    duration = time.time() - t0
    logger.success(
        f"Scan complete: {len(opportunities)} opportunities "
        f"({', '.join(f'{k}={v}' for k, v in sorted(type_opp_counts.items()))}) "
        f"from {len(active_markets)} markets in {duration:.1f}s"
    )
    return ScanResult(
        opportunities=opportunities,
        markets_scanned=len(active_markets),
        cities_resolved=cities_resolved,
        errors=errors,
        scan_duration_s=duration,
        type_counts=type_counts,
        type_opp_counts=type_opp_counts,
    )


# ── Rich display ──────────────────────────────────────────────────────────────

def display_opportunities(result: ScanResult, top_n: int = 20) -> None:
    opps = result.opportunities[:top_n]

    console.rule("[bold cyan]Weather Arbitrage Opportunities[/bold cyan]")

    # Summary line with per-type breakdown
    type_summary = " · ".join(
        f"{MarketType(k).emoji} {k}={v}" for k, v in sorted(result.type_counts.items())
    )
    opp_summary = " · ".join(
        f"{MarketType(k).emoji} {v} opps" for k, v in sorted(result.type_opp_counts.items())
    )
    console.print(
        f"  Scanned [bold]{result.markets_scanned}[/bold] markets "
        f"({type_summary}) · "
        f"[bold]{result.cities_resolved}[/bold] cities geocoded · "
        f"[bold]{len(result.opportunities)}[/bold] opportunities ({opp_summary}) · "
        f"{result.scan_duration_s:.1f}s",
        style="dim",
    )

    if not opps:
        console.print("[yellow]No opportunities found meeting thresholds.[/yellow]")
        return

    table = Table(
        show_header=True,
        header_style="bold magenta",
        border_style="dim",
        row_styles=["", "dim"],
    )
    table.add_column("Type", no_wrap=True)
    table.add_column("City", style="cyan", no_wrap=True)
    table.add_column("Date", no_wrap=True)
    table.add_column("Bucket", no_wrap=True)
    table.add_column("Model%", justify="right", style="green")
    table.add_column("Mkt%", justify="right", style="red")
    table.add_column("EV", justify="right", style="bold green")
    table.add_column("Conf", justify="right")
    table.add_column("Size$", justify="right", style="yellow")
    table.add_column("hrs", justify="right")

    for opp in opps:
        hrs = (
            f"{hours_until(opp.market.resolution_datetime):.0f}"
            if opp.market.resolution_datetime
            else "?"
        )
        ev_style = "bold green" if opp.ev >= 0.35 else "green"
        mtype = opp.market.market_type
        table.add_row(
            f"{mtype.emoji} {mtype.value[:4]}",
            opp.market.city,
            str(opp.market.target_date),
            opp.bucket.outcome_label,
            fmt_pct(opp.model_prob),
            fmt_pct(opp.market_price),
            f"[{ev_style}]{fmt_pct(opp.ev)}[/{ev_style}]",
            fmt_pct(opp.confidence),
            fmt_usdc(opp.suggested_size_usdc),
            hrs,
        )

    console.print(table)
    console.print()
