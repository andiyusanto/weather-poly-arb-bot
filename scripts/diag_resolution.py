"""
Diagnostic: probe Polymarket resolution endpoints for already-settled markets.

Run this ON THE TOKYO SERVER (geounblocked):

    python scripts/diag_resolution.py

It hits three candidate endpoints for two known-settled conditionIds and prints
the resolution-relevant fields so we can write a correct fetch_market_resolution.
No DB writes, no trading — pure read.
"""

from __future__ import annotations

import json

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Two May-18 buckets that have long since settled.
SAMPLES = [
    ("Beijing",  "0x1d3e1176b7a9ba3968a72bf46afd4e35f6c63e869b3af9e21d8ee395a0eaca5a"),
    ("Jakarta",  "0x34ca6ca32cdfe469c18ba00f67031ff5d0b56ee8f750f4eaa8329f274a06719b"),
]

INTERESTING = [
    "id", "conditionId", "question", "closed", "active", "resolved",
    "outcomes", "outcomePrices", "umaResolutionStatus", "resolvedBy",
    "endDate", "closedTime",
]


def _show(label: str, status: int, payload) -> None:
    print(f"\n  [{label}] HTTP {status}")
    if status != 200:
        print(f"    body: {str(payload)[:300]}")
        return
    if isinstance(payload, list):
        print(f"    (list of {len(payload)})")
        payload = payload[0] if payload else {}
    if isinstance(payload, dict):
        for k in INTERESTING:
            if k in payload:
                print(f"    {k} = {payload[k]!r}")
        # CLOB market shape: tokens[] with winner flags
        if "tokens" in payload:
            print(f"    tokens = {json.dumps(payload['tokens'], indent=6)[:600]}")


def probe(name: str, cond: str) -> None:
    print("=" * 70)
    print(f"{name}  {cond}")
    with httpx.Client(timeout=20) as c:
        # A) Current (broken) path — condition id on /markets/{id}
        try:
            r = c.get(f"{GAMMA}/markets/{cond}")
            _show("A gamma /markets/{cond}", r.status_code, r.json() if r.status_code == 200 else r.text)
        except Exception as e:
            print(f"  [A] ERROR {e}")

        # B) Gamma filter by condition_ids — expected correct Gamma path
        try:
            r = c.get(f"{GAMMA}/markets", params={"condition_ids": cond})
            _show("B gamma /markets?condition_ids", r.status_code, r.json() if r.status_code == 200 else r.text)
        except Exception as e:
            print(f"  [B] ERROR {e}")

        # C) CLOB market — tokens[].winner is the cleanest resolution signal
        try:
            r = c.get(f"{CLOB}/markets/{cond}")
            _show("C clob /markets/{cond}", r.status_code, r.json() if r.status_code == 200 else r.text)
        except Exception as e:
            print(f"  [C] ERROR {e}")


if __name__ == "__main__":
    for name, cond in SAMPLES:
        probe(name, cond)
    print("\nDone. Paste the full output back.")
