"""
Backfill settlement-station bias rows for every (city, date) already in the
bias store, so station-based sigma/bias tables are warm immediately instead
of accruing at ~1-3 resolutions/day.

For each city: one IEM ASOS request covering the city's full date range
(tz=local so 'valid' timestamps group by settlement day), daily max computed
per local day (≥6 reports required), written as model='station' rows with the
same combined forecast_mean the OM rows carry. Resumable: (city, date) pairs
that already have a station row are skipped.

Run on the box whose bias DB should be populated (VPS for production; local
for offline evaluation). Read-only against IEM; writes only model='station'
rows — never touches OM/ensemble/per-model rows.

Known limitation (inherited from the OM rows): historical forecast_mean
values from same-day trades include the intraday clamp; sigma is slightly
understated for BOTH sources equally, so comparisons remain fair.

Usage:
    python scripts/backfill_station_bias.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import BIAS_DB, CITIES_CACHE_DB, DATA_DIR  # noqa: E402

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
MIN_REPORTS = 6


def fetch_station_range(icao: str, tz: str, d0: str, d1: str) -> dict[str, float]:
    """{local_date: daily_max_F} for a station over [d0, d1]."""
    y0, m0, dd0 = d0.split("-")
    import datetime as dt
    end = dt.date.fromisoformat(d1) + dt.timedelta(days=1)
    params = {
        "station": icao, "data": "tmpf",
        "year1": int(y0), "month1": int(m0), "day1": int(dd0),
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": tz, "format": "onlycomma", "latlon": "no", "missing": "empty",
    }
    for attempt in range(4):
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.get(IEM_URL, params=params)
                resp.raise_for_status()
                text = resp.text
            break
        except httpx.HTTPError as e:
            if attempt == 3:
                print(f"  {icao}: fetch failed ({e})")
                return {}
            time.sleep(5 * (attempt + 1))
    by_day: dict[str, list[float]] = defaultdict(list)
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 3 and parts[2] not in ("", "M"):
            try:
                by_day[parts[1][:10]].append(float(parts[2]))
            except ValueError:
                continue
    return {d: max(v) for d, v in by_day.items() if len(v) >= MIN_REPORTS}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stations = json.loads((DATA_DIR / "station_map.json").read_text())
    geo = {}
    with sqlite3.connect(CITIES_CACHE_DB) as c:
        for k, tz in c.execute("SELECT city_key, timezone FROM geocache"):
            geo[k] = tz

    conn = sqlite3.connect(BIAS_DB)
    # Combined mean per (city, date). Only pairs that HAVE an ensemble row (a
    # true combined day-ahead mean): pre-2026-07-10 legacy rows duplicated it,
    # forecast-logger and non-same-day trades write it. Pairs with ONLY
    # per-model rows are same-day trades — the recorder deliberately writes no
    # combined row for them; fabricating one from an unweighted AVG of
    # per-model means would inject short-lead errors into station sigma, the
    # very self-sharpening the same-day exclusion prevents (review 2026-07-20).
    targets = conn.execute("""
        SELECT city, target_date, forecast_mean FROM bias
        WHERE variable='temperature' AND model='ensemble'
    """).fetchall()
    have = {(r[0], r[1]) for r in conn.execute(
        "SELECT city, target_date FROM bias WHERE model='station'")}

    by_city: dict[str, list] = defaultdict(list)
    for city, d, fm in targets:
        if (city, d) not in have and fm is not None:
            by_city[city].append((d, fm))
    print(f"{sum(len(v) for v in by_city.values())} (city, date) pairs to backfill "
          f"across {len(by_city)} cities")

    written = 0
    for city, pairs in sorted(by_city.items()):
        entry = stations.get(city.lower())
        tz = geo.get(city.lower())
        if not isinstance(entry, dict) or not tz:
            print(f"  {city}: no station/tz mapping — skipped ({len(pairs)} days)")
            continue
        icao = entry["icao"]
        dates = [p[0] for p in pairs]
        obs = fetch_station_range(icao, tz, min(dates), max(dates))
        n = 0
        for d, fm in pairs:
            if d in obs and not args.dry_run:
                conn.execute(
                    "INSERT OR REPLACE INTO bias VALUES (?,?,?,?,?,?,?)",
                    (city, "station", "temperature", d, fm, obs[d], obs[d] - fm),
                )
                n += 1
        conn.commit()
        written += n
        print(f"  {city} [{icao}]: {n}/{len(pairs)} days backfilled")
        time.sleep(1.0)
    print(f"\nDONE: {written} station rows written to {BIAS_DB}")


if __name__ == "__main__":
    main()
