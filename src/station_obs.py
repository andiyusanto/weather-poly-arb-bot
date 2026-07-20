"""
Settlement-station observations: daily max temperature from the airport
station that actually resolves each Polymarket temperature market.

Markets settle on a NAMED station's readings (Wunderground/NOAA history for
an ICAO airport — see the rules text of any daily temperature market), not
on grid model values. The 2026-07-11 settlement audit measured Open-Meteo's
grid ground truth landing in the winning bucket only 26% of the time
(offset +1.27°F global, ±4°F per city, 2.4°F residual scatter), so bias and
sigma must ultimately be fit against station data.

Station METAR history comes from the Iowa Environmental Mesonet ASOS archive
(free, global coverage). The city → ICAO map is data/station_map.json,
built by scripts/build_station_map.py from Gamma market descriptions.

All lookups are best-effort: any failure returns None and the caller keeps
the Open-Meteo path — this module must never block trading or resolution.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date as date_type, timedelta
from typing import Optional

import httpx
from loguru import logger

from config.settings import DATA_DIR

STATION_MAP_PATH = DATA_DIR / "station_map.json"
IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

_station_map: Optional[dict] = None


def _load_station_map() -> dict:
    """Return the station map, (re)loading on demand.

    A failed/empty load is NOT cached: the map is the prerequisite for the
    GROUND_TRUTH_SOURCE=station flip, so a transient read error (e.g. reading
    the file mid-rewrite) must not permanently disable station recording for
    a long-running process. Retries on every call until a non-empty map loads.
    """
    global _station_map
    if _station_map:
        return _station_map
    try:
        data = json.loads(STATION_MAP_PATH.read_text())
    except (OSError, ValueError) as e:
        logger.error(f"station map unavailable ({e}) — station ground truth "
                     f"disabled until it loads; will retry")
        return {}
    _station_map = data
    return _station_map


def mapped_cities() -> set:
    """Lower-cased city keys that have a settlement-station mapping."""
    return {k for k in _load_station_map() if not k.startswith("_")}


def station_for_city(city: str) -> Optional[str]:
    """ICAO code of the settlement station for a city, or None if unmapped."""
    entry = _load_station_map().get(city.strip().lower())
    return entry.get("icao") if isinstance(entry, dict) else None


def fetch_station_daily_max_f(icao: str, target: date_type, tz: str) -> Optional[float]:
    """
    Max METAR temperature (°F) at a station over one LOCAL calendar day.

    Mirrors how Wunderground/NOAA daily history computes the day's high:
    the max over all reports timestamped on that local date. Returns None on
    any failure, missing data, or suspiciously thin coverage (<6 reports —
    a partial day would understate the max).
    """
    if not icao or not tz:
        return None
    end = target + timedelta(days=1)
    params = {
        "station": icao, "data": "tmpf",
        "year1": target.year, "month1": target.month, "day1": target.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": tz, "format": "onlycomma", "latlon": "no", "missing": "empty",
    }
    try:
        with httpx.Client(timeout=25) as client:
            resp = client.get(IEM_URL, params=params)
            resp.raise_for_status()
            text = resp.text
    except httpx.HTTPError as e:
        logger.warning(f"IEM fetch failed for {icao}/{target}: {e}")
        return None

    day_prefix = target.isoformat()
    temps = []
    try:
        for row in csv.reader(io.StringIO(text)):
            # columns: station, valid(local), tmpf
            if len(row) >= 3 and row[1].startswith(day_prefix) and row[2] not in ("", "tmpf", "M"):
                temps.append(float(row[2]))
    except (ValueError, IndexError) as e:
        logger.warning(f"IEM parse failed for {icao}/{target}: {e}")
        return None
    if len(temps) < 6:
        logger.debug(f"IEM thin coverage for {icao}/{target}: {len(temps)} reports")
        return None
    return max(temps)
