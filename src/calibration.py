"""
Empirical calibration & per-city skill weighting.

Two layers, both learned from resolved shadow trades:

1. ``calibrate_probability`` — isotonic-style mapping that pulls overconfident
   model probabilities toward realised win-rate. Weather ensembles tend to be
   overconfident at the tails (95% predicted → ~80% realised). Built per
   market type because temp/precip/snow have different bias profiles.

2. ``city_skill_factor`` — multiplicative confidence adjustment per (city,
   market_type). Coastal/mountainous cells have higher forecast variance and
   should be downweighted; consistently accurate cities get a small bonus.

Both default to identity transforms (1.0 / pass-through) until enough resolved
trades exist (>= MIN_SAMPLES). Recompute with ``rebuild_calibration()`` after
each batch of shadow resolutions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from loguru import logger

from config.settings import DATA_DIR, TRADES_DB

CALIB_DB = DATA_DIR / "calibration.db"
MIN_SAMPLES_TYPE = 30           # need ≥30 resolved trades per type to calibrate
MIN_SAMPLES_CITY = 10           # need ≥10 resolved trades per city to score


# ── Storage ───────────────────────────────────────────────────────────────────

def _init_db() -> None:
    with sqlite3.connect(CALIB_DB) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS calibration_curve (
                market_type TEXT PRIMARY KEY,
                bin_edges TEXT,         -- JSON list of bin upper-edges
                bin_rates TEXT,         -- JSON list of realized win-rates
                n_samples INTEGER,
                updated_at TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS city_skill (
                city TEXT,
                market_type TEXT,
                skill REAL,             -- multiplicative factor [0.5, 1.1]
                n_samples INTEGER,
                updated_at TEXT,
                PRIMARY KEY (city, market_type)
            )
            """
        )
        c.commit()


_init_db()
_cache_lock = threading.Lock()
_curve_cache: Dict[str, Tuple[List[float], List[float]]] = {}
_skill_cache: Dict[Tuple[str, str], float] = {}


def reset_cache() -> None:
    """
    Drop the in-memory curve/skill caches so the next lookup re-reads the DB.

    Critical for the long-running ``trade`` process: ``rebuild_calibration`` runs
    in a *separate* process (the resolve cron), so it can only clear its own
    cache. Without an explicit reset here, the trader would keep serving whatever
    curve it cached at startup — typically the warm-up identity curve — and bet on
    uncalibrated probabilities forever. The scanner calls this once per cycle.
    """
    with _cache_lock:
        _curve_cache.clear()
        _skill_cache.clear()


# ── Inference ─────────────────────────────────────────────────────────────────

def calibrate_probability(raw_prob: float, market_type: str) -> float:
    """
    Apply the empirical calibration curve for this market_type. Returns the
    raw probability unchanged if no curve has been fit yet.
    """
    with _cache_lock:
        curve = _curve_cache.get(market_type)
    if curve is None:
        curve = _load_curve(market_type)
        with _cache_lock:
            _curve_cache[market_type] = curve
    if not curve:
        return float(raw_prob)

    edges, rates = curve
    if not edges or not rates:
        return float(raw_prob)

    # Locate the bin and return its empirical rate.
    p = max(0.0, min(1.0, float(raw_prob)))
    for i, edge in enumerate(edges):
        if p <= edge:
            return float(rates[i])
    return float(rates[-1])


def city_skill_factor(city: str, market_type: str) -> float:
    """
    Multiplicative confidence factor for (city, market_type). Defaults to 1.0
    when we don't yet have enough resolved trades.
    """
    key = (city, market_type)
    with _cache_lock:
        if key in _skill_cache:
            return _skill_cache[key]
    factor = _load_skill(city, market_type)
    with _cache_lock:
        _skill_cache[key] = factor
    return factor


def _load_curve(market_type: str) -> Tuple[List[float], List[float]]:
    with sqlite3.connect(CALIB_DB) as c:
        row = c.execute(
            "SELECT bin_edges, bin_rates FROM calibration_curve WHERE market_type=?",
            (market_type,),
        ).fetchone()
    if not row:
        return ([], [])
    try:
        return (json.loads(row[0]), json.loads(row[1]))
    except Exception:
        return ([], [])


def _load_skill(city: str, market_type: str) -> float:
    with sqlite3.connect(CALIB_DB) as c:
        row = c.execute(
            "SELECT skill FROM city_skill WHERE city=? AND market_type=?",
            (city, market_type),
        ).fetchone()
    return float(row[0]) if row else 1.0


# ── Training ──────────────────────────────────────────────────────────────────

def rebuild_calibration(n_bins: int = 10) -> Dict[str, int]:
    """
    Recompute calibration curves and per-city skill from the resolved shadow
    trade history. Returns a summary of how many samples were used per type.
    """
    samples = _load_resolved_trades()
    summary: Dict[str, int] = {}

    by_type: Dict[str, List[Tuple[float, int]]] = {}
    by_city_type: Dict[Tuple[str, str], List[Tuple[float, int]]] = {}
    for s in samples:
        by_type.setdefault(s["market_type"], []).append((s["model_prob"], s["won"]))
        key = (s["city"], s["market_type"])
        by_city_type.setdefault(key, []).append((s["model_prob"], s["won"]))

    for mtype, rows in by_type.items():
        summary[mtype] = len(rows)
        if len(rows) < MIN_SAMPLES_TYPE:
            logger.info(
                f"calibration[{mtype}]: {len(rows)} samples < {MIN_SAMPLES_TYPE} — using identity"
            )
            continue
        edges, rates = _fit_isotonic(rows, n_bins=n_bins)
        with sqlite3.connect(CALIB_DB) as c:
            c.execute(
                "INSERT OR REPLACE INTO calibration_curve VALUES (?,?,?,?,?)",
                (mtype, json.dumps(edges), json.dumps(rates), len(rows),
                 datetime.now(timezone.utc).isoformat()),
            )
            c.commit()
        logger.info(
            f"calibration[{mtype}]: fit on {len(rows)} samples — edges={edges}, rates={rates}"
        )

    for (city, mtype), rows in by_city_type.items():
        if len(rows) < MIN_SAMPLES_CITY:
            continue
        skill = _city_skill(rows)
        with sqlite3.connect(CALIB_DB) as c:
            c.execute(
                "INSERT OR REPLACE INTO city_skill VALUES (?,?,?,?,?)",
                (city, mtype, skill, len(rows),
                 datetime.now(timezone.utc).isoformat()),
            )
            c.commit()

    # Invalidate caches so this process serves the freshly-fit curve immediately.
    reset_cache()

    return summary


def _load_resolved_trades() -> List[Dict]:
    """Fetch resolved shadow trades joined with market type."""
    if not Path(TRADES_DB).exists():
        return []
    rows: List[Dict] = []
    try:
        with sqlite3.connect(TRADES_DB) as c:
            c.row_factory = sqlite3.Row
            cur = c.execute(
                """
                SELECT city, bucket_label, model_prob, market_price, side, outcome
                FROM trades
                WHERE shadow=1 AND outcome IS NOT NULL
                """
            )
            for r in cur.fetchall():
                outcome = (r["outcome"] or "").lower()
                if outcome not in ("yes", "no"):
                    continue
                side = (r["side"] or "yes").lower() if "side" in r.keys() else "yes"
                # Did we win? Yes if (side==yes and outcome==yes) or (side==no and outcome==no)
                won = int((side == "yes" and outcome == "yes") or
                          (side == "no" and outcome == "no"))
                # Infer market_type from bucket_label heuristically (legacy rows
                # didn't store it). °/°F/°C → temp, mm/inch/precip → precip,
                # cm/snow → snow, mph → wind.
                lbl = (r["bucket_label"] or "").lower()
                if "°" in lbl or "f" in lbl and "in" not in lbl:
                    mtype = "temperature"
                elif "snow" in lbl or "cm" in lbl:
                    mtype = "snowfall"
                elif "mph" in lbl or "kph" in lbl or "wind" in lbl:
                    mtype = "wind_speed"
                else:
                    mtype = "precipitation"
                rows.append({
                    "city": r["city"] or "",
                    "market_type": mtype,
                    "model_prob": float(r["model_prob"] or 0.0),
                    "won": won,
                })
    except Exception as e:
        logger.warning(f"calibration: failed to load resolved trades: {e}")
    return rows


def _fit_isotonic(
    rows: List[Tuple[float, int]], n_bins: int = 10
) -> Tuple[List[float], List[float]]:
    """
    Lightweight isotonic-style binning. Sort by predicted prob, split into
    n_bins equal-count bins, return (upper_edges, realised_rates) with the
    realised rates monotonically increasing (PAV merge).
    """
    rows = sorted(rows, key=lambda x: x[0])
    if not rows:
        return ([], [])
    n = len(rows)
    bin_size = max(1, n // n_bins)
    edges: List[float] = []
    rates: List[float] = []
    for i in range(0, n, bin_size):
        chunk = rows[i:i + bin_size]
        if not chunk:
            continue
        edges.append(float(chunk[-1][0]))
        # Laplace (additive) smoothing: (wins + 1) / (n + 2). With ~3 samples per
        # bin a raw proportion yields hard 0.0/1.0 — and a calibrated prob of 1.0
        # makes Kelly treat an ~85%-true outcome as certain and size to the cap.
        # Smoothing shrinks extremes toward 0.5 and auto-relaxes as n grows.
        wins = sum(w for _, w in chunk)
        rates.append((wins + 1.0) / (len(chunk) + 2.0))

    # Pool-adjacent-violators to enforce monotonicity
    i = 0
    while i < len(rates) - 1:
        if rates[i] > rates[i + 1]:
            merged = (rates[i] + rates[i + 1]) / 2
            rates[i] = rates[i + 1] = merged
            i = max(0, i - 1)
        else:
            i += 1

    edges[-1] = 1.0
    return (edges, rates)


def _city_skill(rows: List[Tuple[float, int]]) -> float:
    """
    Per-city skill = clip(mean realized / mean predicted, 0.6, 1.1).
    A score < 1.0 means the city's forecasts are overconfident — downweight.
    """
    pred = sum(p for p, _ in rows) / max(len(rows), 1)
    real = sum(w for _, w in rows) / max(len(rows), 1)
    if pred <= 0:
        return 1.0
    return max(0.6, min(1.1, real / pred))
