"""
Settlement-station audit: does Open-Meteo's observed daily max agree with the
bucket Polymarket actually settled?

Our bias correction calibrates forecasts against Open-Meteo's grid value at
the geocoded lat/lon — but Polymarket settles against a specific station's
reading. If they diverge, the model is being calibrated toward the wrong
ground truth. This audit quantifies the divergence per city using resolved
markets we already hold in history_local.db:

  For each (city, date) event with exactly one YES winner, fetch Open-Meteo's
  observed temperature_2m_max for that LOCAL calendar day and check whether it
  falls inside the winning bucket's [lower, upper) °F range. Misses are
  reported with signed distance (observed − nearest bucket edge), so a
  consistent sign reveals a systematic station-vs-grid offset.

Also fetches the UTC-day value to measure how much error the bias recorder's
``timezone=UTC`` convention injects for high-offset cities.

Read-only against the APIs; writes nothing to production DBs.

Usage:
    python3 scripts/audit_settlement.py [--db ~/polymarket-arbitrage-bot/history_local.db]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
OM_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


def _get(url: str, params: dict, retries: int = 4) -> Optional[dict]:
    qs = urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{url}?{qs}", timeout=20) as resp:
                return json.load(resp)
        except Exception as e:  # noqa: BLE001 — standalone audit script
            if attempt == retries - 1:
                print(f"  fetch failed: {e} [{url}?{qs[:80]}]")
                return None
            time.sleep(2 ** attempt)
    return None


def load_geocodes(cities: set) -> dict:
    geo = {}
    with sqlite3.connect(REPO / "data" / "cities_cache.db") as c:
        for key, lat, lon, tz in c.execute("SELECT city_key, lat, lon, timezone FROM geocache"):
            geo[key] = (lat, lon, tz)
    for city in cities:
        if city.lower() in geo:
            continue
        data = _get(GEOCODE_URL, {"name": city, "count": 1})
        hits = (data or {}).get("results") or []
        if hits:
            h = hits[0]
            geo[city.lower()] = (h["latitude"], h["longitude"], h.get("timezone", "UTC"))
            print(f"  geocoded {city}: {h['latitude']:.3f},{h['longitude']:.3f} {h.get('timezone')}")
        time.sleep(1.0)  # Nominatim etiquette applies to any geocoder
    return geo


def fetch_observed_series(lat: float, lon: float, tz: str, d0: str, d1: str) -> dict:
    """{date: tmax_F} for the range, computed on the given timezone's days."""
    data = _get(OM_URL, {
        "latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
        "start_date": d0, "end_date": d1, "timezone": tz,
    })
    daily = (data or {}).get("daily") or {}
    out = {}
    for d, v in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
        if v is not None:
            out[d] = v * 9 / 5 + 32
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / "polymarket-arbitrage-bot" / "history_local.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    # One row per event: the winning bucket's bounds.
    winners = conn.execute("""
        SELECT w.city, w.target_date, w.lower, w.upper, w.bucket_label
        FROM hist_buckets w
        WHERE w.market_type='temperature' AND w.winner='yes'
          AND (SELECT COUNT(*) FROM hist_buckets x
               WHERE x.event_id=w.event_id AND x.target_date=w.target_date
                 AND x.winner='yes') = 1
    """).fetchall()
    by_city: dict[str, list] = defaultdict(list)
    for city, d, lo, hi, label in winners:
        by_city[city].append((d, lo, hi, label))
    print(f"{len(winners)} settled events across {len(by_city)} cities\n")

    geo = load_geocodes(set(by_city))

    rows = []          # (city, date, observed_local, observed_utc, lo, hi)
    for city, events in sorted(by_city.items()):
        g = geo.get(city.lower())
        if not g:
            print(f"  no geocode for {city} — skipped")
            continue
        lat, lon, tz = g
        dates = [e[0] for e in events]
        d0, d1 = min(dates), max(dates)
        obs_local = fetch_observed_series(lat, lon, tz, d0, d1)
        obs_utc = fetch_observed_series(lat, lon, "UTC", d0, d1)
        time.sleep(0.3)
        for d, lo, hi, label in events:
            if d in obs_local:
                rows.append((city, d, obs_local[d], obs_utc.get(d), lo, hi))
        print(f"  {city}: {len([1 for d,_,_,_ in events if d in obs_local])}/{len(events)} days observed")

    # ── analysis ─────────────────────────────────────────────────────────────
    def signed_miss(obs: float, lo: float, hi: float) -> float:
        """0 if inside [lo, hi); else signed distance to the nearest edge (°F)."""
        if lo <= obs < hi:
            return 0.0
        return obs - hi if obs >= hi else obs - lo

    print(f"\n== SETTLEMENT AUDIT: observed (local-day) vs actual winner, n={len(rows)} ==")
    per_city: dict[str, list] = defaultdict(list)
    for city, d, ol, ou, lo, hi in rows:
        per_city[city].append(signed_miss(ol, lo, hi))
    total_n = total_miss = 0
    print(f"{'city':16s} {'n':>4s} {'match%':>7s} {'miss_mean°F':>12s} {'miss_bias':>10s}")
    for city in sorted(per_city, key=lambda c: sum(1 for m in per_city[c] if m != 0) / len(per_city[c]), reverse=True):
        ms = per_city[city]
        misses = [m for m in ms if m != 0.0]
        total_n += len(ms); total_miss += len(misses)
        bias = st.mean(misses) if misses else 0.0
        print(f"{city:16s} {len(ms):4d} {100*(1-len(misses)/len(ms)):6.1f}% "
              f"{st.mean(map(abs, misses)) if misses else 0:12.2f} {bias:+10.2f}")
    print(f"\nOVERALL: {total_n} events, match={100*(1-total_miss/total_n):.1f}%  miss rate={100*total_miss/total_n:.1f}%")

    # UTC-day vs local-day error contribution
    both = [(ol, ou, lo, hi) for _, _, ol, ou, lo, hi in rows if ou is not None]
    n_l = sum(1 for ol, ou, lo, hi in both if not lo <= ol < hi)
    n_u = sum(1 for ol, ou, lo, hi in both if not lo <= ou < hi)
    diffs = [abs(ol - ou) for ol, ou, lo, hi in both]
    print(f"\n== TIMEZONE CHECK (bias recorder uses UTC days) ==")
    print(f"local-day miss rate: {100*n_l/len(both):.1f}%   UTC-day miss rate: {100*n_u/len(both):.1f}%")
    print(f"mean |local − UTC| daily max: {st.mean(diffs):.2f}°F  (>0 means the recorder's "
          f"ground truth differs from the settlement day's)")


if __name__ == "__main__":
    main()
