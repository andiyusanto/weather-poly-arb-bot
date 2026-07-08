"""
Segment-calibration study on historical resolved weather buckets (history.db).

Measures where the MARKET ITSELF is miscalibrated — no forecast needed. For
every resolved bucket we take the YES price at fixed horizons before
resolution and compare implied probability to realized outcome frequency.

PRE-REGISTERED PRIMARY HYPOTHESIS (from the June shadow tape, n=302):
    Buying NO at NO-price 0.50–0.80 (YES 0.20–0.50), 12–24 h before
    resolution, is +ROI after realistic execution costs.
Everything else printed here is EXPLORATORY — treat as hypothesis
generation, not confirmation (multiple-comparisons risk).

Train/test discipline: segments are shown per calendar month; the most
recent full month is the holdout. A slice only "confirms" if it is positive
in the holdout too.

Cost model: entry = quoted price + slip (0/2/5 ¢) and 1.25 % taker fee on
spend. Win pays $1/share.

Usage:
    python scripts/analyze_history.py [--db data/history.db] [--type temperature]
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HORIZONS_H = [48, 24, 12, 6, 2]
STALE_LIMIT_S = 3 * 3600          # skip horizon sample if nearest price older
TAKER_FEE = 0.0125
SLIPS = [0.00, 0.02, 0.05]
ALLOWLIST = {"manila", "jeddah", "moscow", "mexico city", "chengdu", "guangzhou"}


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def load_samples(db: Path, mtype: str) -> list[dict]:
    """One sample per (bucket, horizon): YES price H hours before resolution."""
    conn = sqlite3.connect(db)
    buckets = conn.execute(
        """SELECT b.condition_id, b.city, b.target_date, b.bucket_label,
                  b.end_date, b.winner, b.yes_token_id
           FROM hist_buckets b
           JOIN hist_price_status s ON s.token_id = b.yes_token_id AND s.status='ok'
           WHERE b.winner IS NOT NULL AND b.market_type = ?""",
        (mtype,)).fetchall()

    samples = []
    for cid, city, target_date, label, end_date, winner, token in buckets:
        if not end_date:
            continue
        try:
            end_ts = int(datetime.fromisoformat(end_date).timestamp())
        except ValueError:
            continue
        series = conn.execute(
            "SELECT t, p FROM hist_prices WHERE token_id=? ORDER BY t", (token,)).fetchall()
        if not series:
            continue
        for h in HORIZONS_H:
            cut = end_ts - h * 3600
            # last price at or before the horizon cut
            price_at = None
            for t, p in series:
                if t <= cut:
                    price_at = (t, p)
                else:
                    break
            if price_at is None or cut - price_at[0] > STALE_LIMIT_S:
                continue
            samples.append(dict(
                cid=cid, city=city, target_date=target_date, label=label,
                month=(target_date or "")[:7], horizon=h,
                yes_price=price_at[1], won_yes=1 if winner == "yes" else 0,
            ))
    conn.close()
    return samples


def no_trade_roi(yes_price: float, won_yes: int, slip: float) -> float:
    """Per-$ ROI of buying NO at (1 - yes_price + slip), incl. taker fee."""
    entry = min(0.999, 1.0 - yes_price + slip)
    cost = entry * (1 + TAKER_FEE)
    return (1.0 - cost) / cost if not won_yes else -1.0


def _fmt_slice(rows: list[dict], slip: float = 0.02) -> str:
    n = len(rows)
    if n == 0:
        return "n=0"
    wins = sum(1 - r["won_yes"] for r in rows)
    lo, hi = wilson_ci(wins, n)
    roi = sum(no_trade_roi(r["yes_price"], r["won_yes"], slip) for r in rows) / n
    return (f"n={n:5d}  NO-WR={wins / n * 100:5.1f}% [CI {lo * 100:.0f}–{hi * 100:.0f}] "
            f"ROI@{slip * 100:.0f}¢slip={roi * 100:+6.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "data" / "history.db"))
    ap.add_argument("--type", default="temperature")
    args = ap.parse_args()

    samples = load_samples(Path(args.db), args.type)
    if not samples:
        print("No samples — run scripts/fetch_history.py first.")
        return
    months = sorted({s["month"] for s in samples})
    holdout = months[-1]
    print(f"{len(samples)} samples ({len({s['cid'] for s in samples})} buckets), "
          f"months {months[0]}..{months[-1]}, holdout={holdout}\n")

    # ── 1. Global calibration curve (24h horizon) ────────────────────────────
    print("== Market calibration, 24h horizon (YES price decile → realized YES freq) ==")
    h24 = [s for s in samples if s["horizon"] == 24]
    by_decile: dict[int, list] = defaultdict(list)
    for s in h24:
        by_decile[min(9, int(s["yes_price"] * 10))].append(s)
    for d in sorted(by_decile):
        rows = by_decile[d]
        n = len(rows)
        freq = sum(r["won_yes"] for r in rows) / n
        implied = sum(r["yes_price"] for r in rows) / n
        lo, hi = wilson_ci(sum(r["won_yes"] for r in rows), n)
        edge = freq - implied
        print(f"  YES {d / 10:.1f}–{d / 10 + .1:.1f}: implied={implied:.3f} "
              f"realized={freq:.3f} [CI {lo:.2f}–{hi:.2f}] edge={edge:+.3f} n={n}")

    # ── 2. PRIMARY: NO band 0.50–0.80, by horizon, with cost sensitivity ─────
    print("\n== PRIMARY HYPOTHESIS: buy NO at NO-price 0.50–0.80 (YES 0.20–0.50) ==")
    for h in HORIZONS_H:
        band = [s for s in samples if s["horizon"] == h and 0.20 <= s["yes_price"] <= 0.50]
        line = " | ".join(
            f"ROI@{sl * 100:.0f}¢={sum(no_trade_roi(r['yes_price'], r['won_yes'], sl) for r in band) / len(band) * 100:+5.1f}%"
            for sl in SLIPS) if band else ""
        wins = sum(1 - r["won_yes"] for r in band)
        n = len(band)
        wr = f"{wins / n * 100:5.1f}%" if n else "  n/a"
        print(f"  H={h:2d}h  n={n:5d}  NO-WR={wr}  {line}")

    # ── 3. Primary band train vs holdout (24h + 12h pooled) ──────────────────
    print(f"\n== PRIMARY band by month (12–24h pooled; holdout = {holdout}) ==")
    band = [s for s in samples if s["horizon"] in (12, 24) and 0.20 <= s["yes_price"] <= 0.50]
    for m in months:
        rows = [s for s in band if s["month"] == m]
        tag = " *HOLDOUT*" if m == holdout else ""
        print(f"  {m}: {_fmt_slice(rows)}{tag}")

    # ── 4. EXPLORATORY: by city (12–24h pooled, primary band) ────────────────
    print("\n== EXPLORATORY: primary band by city (12–24h) ==")
    by_city: dict[str, list] = defaultdict(list)
    for s in band:
        by_city[s["city"]].append(s)
    for city in sorted(by_city, key=lambda c: -len(by_city[c])):
        rows = by_city[city]
        if len(rows) < 30:
            continue
        mark = " <== allowlist" if city.lower() in ALLOWLIST else ""
        print(f"  {city:15s} {_fmt_slice(rows)}{mark}")

    # ── 5. EXPLORATORY: full NO-price grid (24h) to spot better bands ────────
    print("\n== EXPLORATORY: NO-price bands, 24h horizon ==")
    for lo_b in [x / 10 for x in range(1, 10)]:
        rows = [s for s in h24 if lo_b <= 1 - s["yes_price"] < lo_b + 0.1]
        if len(rows) < 30:
            continue
        print(f"  NO {lo_b:.1f}–{lo_b + 0.1:.1f}: {_fmt_slice(rows)}")

    # ── 6. EXPLORATORY: YES side sanity check (24h) ──────────────────────────
    print("\n== EXPLORATORY: buy YES bands, 24h (fee+2¢ slip) ==")
    for lo_b in [x / 10 for x in range(1, 10)]:
        rows = [s for s in h24 if lo_b <= s["yes_price"] < lo_b + 0.1]
        if len(rows) < 30:
            continue
        n = len(rows)
        wins = sum(r["won_yes"] for r in rows)
        roi = sum(
            ((1 - min(0.999, r["yes_price"] + 0.02) * (1 + TAKER_FEE))
             / (min(0.999, r["yes_price"] + 0.02) * (1 + TAKER_FEE))
             if r["won_yes"] else -1.0)
            for r in rows) / n
        print(f"  YES {lo_b:.1f}–{lo_b + 0.1:.1f}: n={n:5d} YES-WR={wins / n * 100:5.1f}% ROI={roi * 100:+6.1f}%")


if __name__ == "__main__":
    main()
