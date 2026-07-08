"""
Fetch RESOLVED Polymarket weather markets + hourly price histories → SQLite.

Run ON THE VPS (geo-unblocked). Read-only against Gamma + CLOB; writes to
data/history.db (separate from trades.db — never touches the live tape).

Two phases, both resumable (re-running skips what's already stored):

  Phase 1 — Gamma /events?closed=true&tag_slug=weather|precipitation, paginated.
            Every bucket whose question parses via _classify_market lands in
            `hist_buckets` with its resolution (winner yes/no) read from the
            final outcomePrices when decisive, else from CLOB
            /markets/{conditionId} tokens[].winner as fallback.

  Phase 2 — CLOB /prices-history for each YES token (fidelity=60 → hourly
            mid prices over the market's life) into `hist_prices`.

Usage (VPS):
    python scripts/fetch_history.py --days 90                 # both phases
    python scripts/fetch_history.py --days 90 --skip-prices   # metadata only
    python scripts/fetch_history.py --max-tokens 500          # bounded test run

Request budget: phase 1 is ~1 request/100 events; phase 2 is 1 request per
bucket (≈600/day of history → ~54k for 90 days) at ≤5 req/s ≈ 3 h. Resumable,
so it can be interrupted and re-run freely.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import threading
from urllib.parse import urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter

# Make repo modules importable when run as `python scripts/fetch_history.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.polymarket_client import _classify_market, _parse_clob_ids, _parse_res_time  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HOSTS = ("gamma-api.polymarket.com", "clob.polymarket.com")
DOH_URL = "https://1.1.1.1/dns-query"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "history.db"


# ── Hijack-proof session (ported from Polymarket-Signal-Edge-Finder) ─────────
# Some ISPs DNS-hijack *.polymarket.com to a block page. Re-resolve via
# DNS-over-HTTPS at 1.1.1.1 (reachable by IP) and pin the real IP while
# keeping full TLS verification (SNI + cert hostname) against the real host.

class _PinnedHostAdapter(HTTPAdapter):
    def __init__(self, hostname: str, ip: str, **kwargs):
        self._hostname = hostname
        self._ip = ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["server_hostname"] = self._hostname
        kwargs["assert_hostname"] = self._hostname
        super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        parts = urlparse(request.url)
        if parts.hostname == self._hostname:
            netloc = self._ip if parts.port is None else f"{self._ip}:{parts.port}"
            request.url = urlunparse(parts._replace(netloc=netloc))
            request.headers["Host"] = self._hostname
        return super().send(request, **kwargs)


def _doh_resolve(hostname: str) -> str:
    r = requests.get(DOH_URL, params={"name": hostname, "type": "A"},
                     headers={"accept": "application/dns-json"}, timeout=15)
    r.raise_for_status()
    answers = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
    if not answers:
        raise RuntimeError(f"DoH returned no A records for {hostname}")
    return answers[0]


def _dns_is_hijacked() -> bool:
    try:
        requests.head(f"{GAMMA}/markets", timeout=10).raise_for_status()
        return False
    except requests.exceptions.SSLError:
        return True
    except requests.exceptions.RequestException:
        return True  # unresolvable/reset — try DoH pinning either way


_PINNED_IPS: dict = {}


def make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers["User-Agent"] = "weather-poly-arb-bot/history-fetch"
    for host, ip in _PINNED_IPS.items():
        sess.mount(f"https://{host}", _PinnedHostAdapter(host, ip))
    return sess


def init_network() -> None:
    if _dns_is_hijacked():
        print("[net] polymarket.com unreachable via local DNS; pinning via 1.1.1.1 DoH")
        for host in HOSTS:
            _PINNED_IPS[host] = _doh_resolve(host)
            print(f"[net]   {host} -> {_PINNED_IPS[host]}")


_tls = threading.local()


def _thread_session() -> requests.Session:
    if not hasattr(_tls, "sess"):
        _tls.sess = make_session()
    return _tls.sess

# Stay far below CLOB rate limits; 429s trigger exponential backoff anyway.
MAX_WORKERS = 5
PRICE_FIDELITY_MIN = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_buckets (
    condition_id TEXT PRIMARY KEY,
    event_id     TEXT,
    event_title  TEXT,
    question     TEXT,
    market_type  TEXT,
    city         TEXT,
    target_date  TEXT,
    bucket_label TEXT,
    lower        REAL,
    upper        REAL,
    yes_token_id TEXT,
    no_token_id  TEXT,
    end_date     TEXT,
    winner       TEXT,          -- 'yes' | 'no' | NULL (unresolved/unknown)
    winner_src   TEXT,          -- 'gamma' | 'clob'
    volume_usdc  REAL,
    fetched_at   TEXT
);
CREATE TABLE IF NOT EXISTS hist_prices (
    token_id TEXT NOT NULL,
    t        INTEGER NOT NULL,  -- unix seconds
    p        REAL NOT NULL,     -- YES price 0..1
    PRIMARY KEY (token_id, t)
);
CREATE TABLE IF NOT EXISTS hist_price_status (
    token_id   TEXT PRIMARY KEY,
    status     TEXT,            -- 'ok' | 'empty' | 'error'
    n_points   INTEGER,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hb_city_date ON hist_buckets (city, target_date);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_json(client: requests.Session, url: str, params: dict, retries: int = 4):
    """GET with 429/5xx exponential backoff."""
    for attempt in range(retries):
        try:
            resp = client.get(url, params=params, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                print(f"  HTTP {resp.status_code} on {url} — backoff {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"unreachable: {url}")


def _winner_from_outcome_prices(raw) -> Optional[str]:
    """Final outcomePrices of a resolved market is ~[1,0] or [0,1] (Yes first)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    try:
        yes, no = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if yes > 0.99 and no < 0.01:
        return "yes"
    if no > 0.99 and yes < 0.01:
        return "no"
    return None


def _winner_from_clob(client: requests.Session, condition_id: str) -> Optional[str]:
    """Fallback: CLOB /markets/{conditionId} tokens[].winner (proven reliable)."""
    try:
        data = _get_json(client, f"{CLOB}/markets/{condition_id}", {})
    except requests.RequestException:
        return None
    for tok in data.get("tokens", []):
        if tok.get("winner"):
            outcome = (tok.get("outcome") or "").strip().lower()
            if outcome in ("yes", "no"):
                return outcome
    return None


def fetch_buckets(conn: sqlite3.Connection, days: int) -> None:
    """Phase 1: paginate closed weather events into hist_buckets."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    known = {r[0] for r in conn.execute("SELECT condition_id FROM hist_buckets")}
    n_new = n_unresolved = 0

    client = make_session()
    if True:
        for tag_slug in ("weather", "precipitation"):
            offset = 0
            consecutive_old_pages = 0
            while offset < 40_000:  # hard safety cap
                params = {
                    "limit": 100, "offset": offset, "tag_slug": tag_slug,
                    "closed": "true", "order": "endDate", "ascending": "false",
                }
                events = _get_json(client, f"{GAMMA}/events", params)
                if isinstance(events, dict):
                    events = events.get("events", [])
                if not events:
                    break

                page_all_old = True
                for event in events:
                    event_end = _parse_res_time(event.get("endDate"))
                    if event_end and event_end >= cutoff:
                        page_all_old = False
                    for item in event.get("markets", []):
                        cid = item.get("conditionId") or ""
                        if not cid or cid in known:
                            continue
                        end = _parse_res_time(item.get("endDate")) or event_end
                        if end and end < cutoff:
                            continue
                        question = item.get("question", "") or item.get("title", "")
                        classification = _classify_market(question)
                        if not classification:
                            continue
                        mtype, city, target_date, bucket_label, (lower, upper) = classification
                        # _classify_market assumes an ACTIVE market and rolls
                        # past month/days into next year. For historical data,
                        # re-anchor the year to the market's end date: pick the
                        # candidate year closest to when it actually resolved.
                        if end:
                            candidates = []
                            for y in (end.year - 1, end.year, end.year + 1):
                                try:
                                    candidates.append(target_date.replace(year=y))
                                except ValueError:  # Feb 29
                                    continue
                            target_date = min(
                                candidates,
                                key=lambda d: abs((d - end.date()).days))

                        winner = _winner_from_outcome_prices(item.get("outcomePrices"))
                        winner_src = "gamma" if winner else None
                        if winner is None:
                            winner = _winner_from_clob(client, cid)
                            winner_src = "clob" if winner else None
                            time.sleep(0.15)
                        if winner is None:
                            n_unresolved += 1

                        clob_ids = _parse_clob_ids(item.get("clobTokenIds", []))
                        conn.execute(
                            """INSERT OR IGNORE INTO hist_buckets VALUES
                               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (cid, str(event.get("id") or ""), event.get("title", ""),
                             question, mtype.value, city, target_date.isoformat(),
                             bucket_label, lower, upper,
                             clob_ids[0] if clob_ids else "",
                             clob_ids[1] if len(clob_ids) > 1 else "",
                             end.isoformat() if end else None,
                             winner, winner_src,
                             float(item.get("volume") or item.get("volumeNum") or 0.0),
                             _now_iso()),
                        )
                        known.add(cid)
                        n_new += 1
                conn.commit()
                print(f"[{tag_slug}] offset={offset}: +{n_new} buckets total "
                      f"({n_unresolved} unresolved)")

                if len(events) < 100:
                    break
                # Events are requested newest-first, but don't trust a single
                # page's ordering: stop only after 3 consecutive pages entirely
                # older than the cutoff.
                consecutive_old_pages = consecutive_old_pages + 1 if page_all_old else 0
                if consecutive_old_pages >= 3:
                    break
                offset += 100
                time.sleep(0.3)

    total = conn.execute("SELECT COUNT(*) FROM hist_buckets").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM hist_buckets WHERE winner IS NOT NULL").fetchone()[0]
    print(f"Phase 1 done: {total} buckets stored, {resolved} resolved "
          f"({total - resolved} without winner)")


def refresh_unresolved(conn: sqlite3.Connection, cap: int = 500) -> None:
    """Retry CLOB winner lookup for previously-unresolved buckets (resumable)."""
    rows = conn.execute(
        "SELECT condition_id FROM hist_buckets WHERE winner IS NULL LIMIT ?",
        (cap,)).fetchall()
    if not rows:
        return
    print(f"Retrying winner lookup for {len(rows)} unresolved buckets…")
    n_fixed = 0
    client = make_session()
    if True:
        for (cid,) in rows:
            winner = _winner_from_clob(client, cid)
            if winner:
                conn.execute(
                    "UPDATE hist_buckets SET winner=?, winner_src='clob' WHERE condition_id=?",
                    (winner, cid))
                n_fixed += 1
            time.sleep(0.2)
    conn.commit()
    print(f"  resolved {n_fixed}/{len(rows)} on retry")


def _fetch_one_price_series(token_id: str) -> tuple[str, str, list]:
    """Worker: one token's full-life hourly price series."""
    try:
        data = _get_json(_thread_session(), f"{CLOB}/prices-history",
                         {"market": token_id, "interval": "max",
                          "fidelity": PRICE_FIDELITY_MIN})
        pts = data.get("history", []) or []
        return token_id, ("ok" if pts else "empty"), pts
    except Exception as e:  # noqa: BLE001 — worker must not kill the pool
        return token_id, f"error: {e}", []


def fetch_prices(conn: sqlite3.Connection, max_tokens: Optional[int]) -> None:
    """Phase 2: hourly YES-price series for every bucket not yet fetched."""
    todo = [r[0] for r in conn.execute(
        """SELECT b.yes_token_id FROM hist_buckets b
           LEFT JOIN hist_price_status s ON s.token_id = b.yes_token_id
           WHERE b.yes_token_id != '' AND b.winner IS NOT NULL AND s.token_id IS NULL
           ORDER BY b.end_date DESC""")]
    if max_tokens:
        todo = todo[:max_tokens]
    print(f"Phase 2: {len(todo)} price series to fetch "
          f"(~{len(todo) * 0.45 / MAX_WORKERS / 60:.0f} min estimated)")

    n_done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one_price_series, t): t for t in todo}
        for fut in as_completed(futures):
            token_id, status, pts = fut.result()
            if pts:
                conn.executemany(
                    "INSERT OR IGNORE INTO hist_prices VALUES (?,?,?)",
                    [(token_id, int(pt["t"]), float(pt["p"])) for pt in pts])
            conn.execute(
                "INSERT OR REPLACE INTO hist_price_status VALUES (?,?,?,?)",
                (token_id, status.split(":")[0], len(pts), _now_iso()))
            n_done += 1
            if n_done % 200 == 0:
                conn.commit()
                print(f"  {n_done}/{len(todo)} series fetched")
            time.sleep(0.05)  # ~global 5 req/s ceiling across workers
    conn.commit()
    ok = conn.execute(
        "SELECT COUNT(*) FROM hist_price_status WHERE status='ok'").fetchone()[0]
    npts = conn.execute("SELECT COUNT(*) FROM hist_prices").fetchone()[0]
    print(f"Phase 2 done: {ok} series ok, {npts} price points total")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90, help="history window (default 90)")
    ap.add_argument("--skip-prices", action="store_true", help="phase 1 only")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="cap phase-2 series (bounded test run)")
    args = ap.parse_args()

    init_network()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    print(f"DB: {DB_PATH}")

    fetch_buckets(conn, args.days)
    refresh_unresolved(conn)
    if not args.skip_prices:
        fetch_prices(conn, args.max_tokens)
    conn.close()


if __name__ == "__main__":
    main()
