"""
Diagnose why archive-api.open-meteo.com is unreachable and pick the fix.

Run ON THE TOKYO SERVER:

    python scripts/diag_archive.py

Tests four things for an already-observed date (Beijing, 2026-05-18):
  1. DNS — does archive-api resolve to IPv4 (A) and/or IPv6 (AAAA)?
  2. archive-api default     (what the bot does today)
  3. archive-api forced IPv4 (local_address=0.0.0.0 binds an IPv4 socket)
  4. api.open-meteo.com forecast endpoint with past dates (known-reachable host)

Whichever of 3 or 4 returns a temperature is the fix we wire in.
"""

from __future__ import annotations

import socket
import time

import httpx

LAT, LON, DAY = 39.9, 116.4, "2026-05-18"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"


def _dns(host: str) -> None:
    print(f"\n[1] DNS for {host}")
    try:
        infos = socket.getaddrinfo(host, 443)
        fams = {("IPv6" if i[0] == socket.AF_INET6 else "IPv4"): i[4][0] for i in infos}
        for fam, addr in fams.items():
            print(f"    {fam}: {addr}")
    except Exception as e:
        print(f"    DNS error: {e}")


def _try(label: str, client: httpx.Client, url: str, params: dict, field: str) -> None:
    t = time.time()
    try:
        r = client.get(url, params=params)
        dt = round(time.time() - t, 1)
        daily = r.json().get("daily", {}) if r.status_code == 200 else {}
        print(f"    {label}: HTTP {r.status_code} in {dt}s  daily.{field}={daily.get(field)}")
    except Exception as e:
        dt = round(time.time() - t, 1)
        print(f"    {label}: FAILED in {dt}s  {type(e).__name__}: {e}")


def main() -> None:
    _dns("archive-api.open-meteo.com")
    _dns("api.open-meteo.com")

    arch_params = {
        "latitude": LAT, "longitude": LON, "start_date": DAY, "end_date": DAY,
        "daily": "temperature_2m_max", "timezone": "UTC",
    }
    fc_params = {
        "latitude": LAT, "longitude": LON, "start_date": DAY, "end_date": DAY,
        "daily": "temperature_2m_max", "timezone": "UTC",
    }

    print("\n[2] archive-api, default transport")
    with httpx.Client(timeout=20) as c:
        _try("archive/default", c, ARCHIVE, arch_params, "temperature_2m_max")

    print("\n[3] archive-api, forced IPv4 (local_address=0.0.0.0)")
    try:
        tr = httpx.HTTPTransport(local_address="0.0.0.0")
        with httpx.Client(timeout=20, transport=tr) as c:
            _try("archive/ipv4", c, ARCHIVE, arch_params, "temperature_2m_max")
    except Exception as e:
        print(f"    setup failed: {e}")

    print("\n[4] api.open-meteo.com forecast endpoint, past date (known-reachable host)")
    with httpx.Client(timeout=20) as c:
        _try("forecast/default", c, FORECAST, fc_params, "temperature_2m_max")

    print("\nDone. Paste the full output back.")


if __name__ == "__main__":
    main()
