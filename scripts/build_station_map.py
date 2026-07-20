"""
Harvest the city → settlement-station (ICAO) map from Polymarket market rules.

Every daily temperature market's description names its resolution source as a
Wunderground history URL whose last path segment is the station's ICAO code:

    https://www.wunderground.com/history/daily/cn/jinan/ZSJN

This is the station whose METAR readings actually settle the market (see
settlement audit 2026-07-11: Open-Meteo grid ground truth mismatches it in
74% of events). The map is written to data/station_map.json and consumed by
src/station_obs.py.

Run wherever Gamma is reachable (uses the DoH pinned session when the local
ISP blocks polymarket.com). Re-run occasionally; cities are stable.

Usage:
    python scripts/build_station_map.py [--days 30]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetch_history import _get_json, init_network, make_session  # noqa: E402
from src.polymarket_client import _classify_market  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
OUT = Path(__file__).resolve().parent.parent / "data" / "station_map.json"

# Two known rules formats name the station:
#   Wunderground: .../history/daily/cc/city/ICAO
#   NOAA:         weather.gov/wrh/timeseries?site=ICAO
_STATION_RES = [
    re.compile(r"wunderground\.com/history/daily/[^\s\"']*/([A-Za-z0-9]{4})\b"),
    re.compile(r"weather\.gov/wrh/timeseries\?site=([A-Za-z0-9]{4})\b"),
]


def _extract_icao(description: str) -> str | None:
    for rx in _STATION_RES:
        m = rx.search(description)
        if m:
            return m.group(1).upper()
    return None


def harvest(days: int) -> dict:
    init_network()
    client = make_session()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stations: dict[str, dict] = {}

    for closed in ("false", "true"):
        offset = 0
        while offset < 2000:
            params = {"limit": 100, "offset": offset, "tag_slug": "weather", "closed": closed}
            if closed == "true":
                params["end_date_min"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
            events = _get_json(client, f"{GAMMA}/events", params)
            if isinstance(events, dict):
                events = events.get("events", [])
            if not events:
                break
            for event in events:
                for m in event.get("markets", []):
                    question = m.get("question", "") or ""
                    cls = _classify_market(question)
                    if not cls or cls[0].value != "temperature":
                        continue
                    city = cls[1]
                    if city.lower() in stations:
                        continue
                    icao = _extract_icao(m.get("description") or "")
                    if icao:
                        stations[city.lower()] = {
                            "icao": icao,
                            "city": city,
                            "sample_question": question[:80],
                        }
                        print(f"  {city}: {icao}")
            if len(events) < 100:
                break
            offset += 100
            time.sleep(0.3)
    return stations


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="closed-events lookback for coverage")
    args = ap.parse_args()
    stations = harvest(args.days)
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    existing.update(stations)
    existing["_meta"] = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "source": "Wunderground URL in Gamma market descriptions"}
    # Atomic write: a reader (the live bot's station_for_city) must never see a
    # truncated file mid-rewrite.
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=1, sort_keys=True))
    tmp.replace(OUT)
    n = len([k for k in existing if not k.startswith("_")])
    print(f"\nstation map: {n} cities -> {OUT}")


if __name__ == "__main__":
    main()
