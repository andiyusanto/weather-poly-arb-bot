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
from datetime import date as date_type, datetime, timedelta
from typing import Optional

import httpx
from loguru import logger

from config.settings import DATA_DIR

# ── Peak-passed lock heuristic (real-time, honest) ──────────────────────────
# The daily max is "locked" once (a) the running-max reading is old enough that
# temps have been falling for a while, (b) the latest reading sits clearly below
# the running max, and (c) we are far enough into the local afternoon that a new
# higher peak is implausible. Gating on the LATEST reading's hour (not the peak
# hour) is deliberate: it still rejects a morning blip whose afternoon could
# exceed it (we defer judgment until mid-afternoon), yet it correctly locks a
# genuine pre-noon max on cold-front days where temps fall all afternoon. Tuned
# to the 2026-07-20 ceiling backtest (confirmed-lock median near 16:00 local).
_LOCK_CONFIRM_HOURS = 2.0    # temps falling for >= this long since the peak
_LOCK_FALL_TOL_F = 1.0       # latest reading is >= this far below the running max
_LOCK_MIN_CONFIRM_HOUR = 15.0  # only judge "locked" once past mid-afternoon local

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


def _fetch_iem_csv(icao: str, target: date_type, tz: str) -> Optional[str]:
    """
    Raw IEM ASOS onlycomma response for a station over the local day
    ``[target, target+1)``. Shared by the daily-max and intraday readers so the
    request params and HTTP error handling live in one place. Returns the CSV
    text, or None on missing args / HTTP failure (best-effort, never raises).
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
            return resp.text
    except httpx.HTTPError as e:
        logger.warning(f"IEM fetch failed for {icao}/{target}: {e}")
        return None


def fetch_station_daily_max_f(icao: str, target: date_type, tz: str) -> Optional[float]:
    """
    Max METAR temperature (°F) at a station over one LOCAL calendar day.

    Mirrors how Wunderground/NOAA daily history computes the day's high:
    the max over all reports timestamped on that local date. Returns None on
    any failure, missing data, or suspiciously thin coverage (<6 reports —
    a partial day would understate the max).
    """
    text = _fetch_iem_csv(icao, target, tz)
    if text is None:
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


def fetch_station_intraday_state(
    icao: str, target: date_type, tz: str
) -> Optional[dict]:
    """
    Current intraday state of a station's daily max, for the peak-passed edge.

    Fetches all of today's local-day METAR reports and returns the running max
    plus a real-time "is the max locked in?" flag. Unlike
    :func:`fetch_station_daily_max_f` this is for an *in-progress* day, so it
    does not require full coverage — it reports whatever is known so far.

    Args:
        icao: Settlement-station ICAO code.
        target: The local calendar day in progress.
        tz: IANA timezone of the station (drives the local-day boundary).

    Returns:
        Dict with ``running_max_f``, ``peak_hour_local`` (local hour of the
        running max, fractional), ``last_hour_local``, ``n_reports``,
        ``hours_since_peak``, and ``locked`` (bool per the peak-passed
        heuristic) — or None on any failure / no reports. Best-effort: never
        raises, never blocks.
    """
    text = _fetch_iem_csv(icao, target, tz)
    if text is None:
        return None

    day_prefix = target.isoformat()
    obs: list[tuple[float, float]] = []  # (local_hour_fractional, tmpf)
    skipped = 0
    for row in csv.reader(io.StringIO(text)):
        # columns: station, valid(local ISO), tmpf
        if len(row) < 3 or not row[1].startswith(day_prefix) or row[2] in ("", "tmpf", "M"):
            continue
        # Guard each row individually: one malformed timestamp/value must skip
        # that report, not discard the whole day's intraday state.
        try:
            dt = datetime.strptime(row[1].strip(), "%Y-%m-%d %H:%M")
            obs.append((dt.hour + dt.minute / 60.0, float(row[2])))
        except (ValueError, IndexError):
            skipped += 1
            continue
    if skipped:
        logger.debug(f"IEM intraday skipped {skipped} unparseable rows for {icao}/{target}")
    if not obs:
        return None

    obs.sort()
    running_max = max(t for _, t in obs)
    peak_hour = next(h for h, t in obs if t >= running_max - 1e-6)
    last_hour, last_temp = obs[-1]
    hours_since_peak = last_hour - peak_hour
    locked = (
        hours_since_peak >= _LOCK_CONFIRM_HOURS
        and last_temp <= running_max - _LOCK_FALL_TOL_F
        and last_hour >= _LOCK_MIN_CONFIRM_HOUR
    )
    return {
        "running_max_f": running_max,
        "peak_hour_local": round(peak_hour, 2),
        "last_hour_local": round(last_hour, 2),
        "n_reports": len(obs),
        "hours_since_peak": round(hours_since_peak, 2),
        "locked": locked,
    }
