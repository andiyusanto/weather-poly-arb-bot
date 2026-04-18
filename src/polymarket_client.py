"""
Polymarket client — wraps Gamma API (market discovery) and CLOB API
(live prices + order placement via py-clob-client).

Supports temperature, precipitation, and snowfall bucket markets.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from config.settings import settings
from src.utils import celsius_to_fahrenheit, http_retry, rate_limited_sleep


# ── Market type classification ────────────────────────────────────────────────

class MarketType(str, Enum):
    TEMPERATURE   = "temperature"
    PRECIPITATION = "precipitation"
    SNOWFALL      = "snowfall"

    @property
    def emoji(self) -> str:
        return {"temperature": "🌡️", "precipitation": "🌧️", "snowfall": "❄️"}[self.value]

    @property
    def unit_label(self) -> str:
        return {"temperature": "°F", "precipitation": "mm", "snowfall": "cm"}[self.value]


# ── Unified bucket dataclass ──────────────────────────────────────────────────

@dataclass
class WeatherBucket:
    """
    Generic weather market bucket. `lower` and `upper` are in the market's
    native unit (°F for temperature, mm for precipitation, cm for snowfall).
    Open-ended buckets use ±9999 as sentinels.
    """
    token_id: str
    outcome_label: str
    lower: float          # inclusive lower bound in native unit
    upper: float          # exclusive upper bound in native unit
    best_ask: float = 0.0
    best_bid: float = 0.0
    volume_usdc: float = 0.0

    # Back-compat aliases used by legacy code
    @property
    def lower_f(self) -> float:
        return self.lower

    @property
    def upper_f(self) -> float:
        return self.upper

    @property
    def mid_price(self) -> float:
        if self.best_ask and self.best_bid:
            return (self.best_ask + self.best_bid) / 2
        return self.best_ask or self.best_bid or 0.5


# Keep old name as alias so existing imports in strategy.py still work
TemperatureBucket = WeatherBucket


@dataclass
class WeatherMarket:
    market_id: str
    question: str
    city: str
    target_date: date
    resolution_datetime: Optional[datetime]
    market_type: MarketType = MarketType.TEMPERATURE
    buckets: List[WeatherBucket] = field(default_factory=list)
    total_volume_usdc: float = 0.0
    active: bool = True


# ── Regex patterns ────────────────────────────────────────────────────────────

# Temperature: "Will the highest temperature in {CITY} be {BUCKET} on {MONTH DAY}?"
_TEMP_TITLE_RE = re.compile(
    r"Will the highest temperature in (?P<city>[A-Za-z][A-Za-z\s]+?) be "
    r"(?P<bucket>[^?]+?) on (?P<date>[A-Za-z]+ \d+)\?",
    re.IGNORECASE,
)

# Precipitation: "Will {CITY} have {BUCKET} of precipitation in {MONTH}?"
_PRECIP_TITLE_RE = re.compile(
    r"Will (?P<city>[A-Za-z][A-Za-z\s,]+?) have (?P<bucket>[^?]+?) "
    r"(?:of )?precipitation in (?P<date>[A-Za-z]+ ?\d*)\?",
    re.IGNORECASE,
)

# Snowfall: "Will {CITY} have {BUCKET} of snowfall in {MONTH}?"
_SNOW_TITLE_RE = re.compile(
    r"Will (?P<city>[A-Za-z][A-Za-z\s,]+?) have (?P<bucket>[^?]+?) "
    r"(?:of )?(?:snowfall|snow) in (?P<date>[A-Za-z]+ ?\d*)\?",
    re.IGNORECASE,
)

# Shared helpers
_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)")
_INCH_MM  = 25.4
_INCH_CM  = 2.54


# ── Date parser ───────────────────────────────────────────────────────────────

def _parse_date(s: str) -> Optional[date]:
    from datetime import timedelta
    s = s.replace(",", "").strip()
    # With year
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Month + day, no year — infer year from today
    for fmt in ("%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(s, fmt)
            today = date.today()
            result = parsed.replace(year=today.year).date()
            if result < today - timedelta(days=1):
                result = result.replace(year=today.year + 1)
            return result
        except ValueError:
            continue
    # Month only (for monthly precipitation markets) — return last day of month
    for fmt in ("%B %Y", "%b %Y", "%B", "%b"):
        try:
            parsed = datetime.strptime(s, fmt)
            today = date.today()
            year = parsed.year if parsed.year != 1900 else today.year
            month = parsed.month
            # last day of that month
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            result = date(year, month, last_day)
            if result < today - timedelta(days=1):
                result = date(year + 1, month, last_day)
            return result
        except ValueError:
            continue
    return None


# ── Unit converters ───────────────────────────────────────────────────────────

def _to_mm(value: float, label: str) -> float:
    if re.search(r"\bin(?:ch(?:es)?)?\b", label, re.IGNORECASE):
        return value * _INCH_MM
    return value


def _to_cm(value: float, label: str) -> float:
    if re.search(r"\bin(?:ch(?:es)?)?\b", label, re.IGNORECASE):
        return value * _INCH_CM
    return value


# ── Bucket parsers (from question title) ──────────────────────────────────────

def _parse_temp_bucket_from_question(bucket_str: str) -> Optional[Tuple[float, float]]:
    """
    Parse temperature range from the bucket portion of a title like:
      'between 80-81°F', '84°F or higher', '65°F or below', '22°C', '22°C or higher'
    Always returns values in °F (converts °C if needed).
    """
    is_celsius = bool(re.search(r"°\s*[Cc]", bucket_str))

    # Range: "80-81" or "between 80-81"
    m = _RANGE_RE.search(bucket_str)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if is_celsius:
            lo, hi = celsius_to_fahrenheit(lo), celsius_to_fahrenheit(hi)
        return lo, hi

    # "or higher" / "or above"
    if re.search(r"or\s+(?:higher|above)", bucket_str, re.IGNORECASE):
        m2 = re.search(r"(\d+(?:\.\d+)?)", bucket_str)
        if m2:
            v = float(m2.group(1))
            return (celsius_to_fahrenheit(v) if is_celsius else v), 9999.0

    # "or below"
    if re.search(r"or\s+below", bucket_str, re.IGNORECASE):
        m2 = re.search(r"(\d+(?:\.\d+)?)", bucket_str)
        if m2:
            v = float(m2.group(1))
            return -9999.0, (celsius_to_fahrenheit(v) if is_celsius else v)

    # Exact value: "22°C" or "68°F"
    m2 = re.search(r"(\d+(?:\.\d+)?)", bucket_str)
    if m2:
        v = float(m2.group(1))
        if is_celsius:
            return celsius_to_fahrenheit(v), celsius_to_fahrenheit(v + 1)
        return v, v + 1.0

    return None


def _parse_precip_bucket_from_question(bucket_str: str) -> Optional[Tuple[float, float]]:
    """
    Parse precipitation range from the bucket portion of a title like:
      'less than 2 inches', 'between 2 and 3 inches', 'more than 6 inches',
      'less than 20mm', 'between 20-30mm', '70mm or more'
    Returns (lower_mm, upper_mm).
    """
    clean = bucket_str.strip()

    if re.search(r"\b(?:less than|below|under)\b", clean, re.IGNORECASE):
        m = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m:
            return 0.0, _to_mm(float(m.group(1)), clean)

    if re.search(r"\b(?:more than|or more|over|above)\b", clean, re.IGNORECASE):
        m = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m:
            return _to_mm(float(m.group(1)), clean), 9999.0

    # "between N and M" (words)
    m = re.search(r"between\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)", clean, re.IGNORECASE)
    if m:
        return _to_mm(float(m.group(1)), clean), _to_mm(float(m.group(2)), clean)

    # "N-M" numeric range
    m = _RANGE_RE.search(clean)
    if m:
        return _to_mm(float(m.group(1)), clean), _to_mm(float(m.group(2)), clean)

    return None


def _parse_snow_bucket_from_question(bucket_str: str) -> Optional[Tuple[float, float]]:
    """
    Parse snowfall range from the bucket portion of a title.
    Returns (lower_cm, upper_cm).
    """
    clean = bucket_str.strip()

    if re.search(r"\b(?:less than|below|under)\b", clean, re.IGNORECASE):
        m = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m:
            return 0.0, _to_cm(float(m.group(1)), clean)

    if re.search(r"\b(?:more than|or more|over|above)\b", clean, re.IGNORECASE):
        m = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m:
            return _to_cm(float(m.group(1)), clean), 9999.0

    m = re.search(r"between\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)", clean, re.IGNORECASE)
    if m:
        return _to_cm(float(m.group(1)), clean), _to_cm(float(m.group(2)), clean)

    m = _RANGE_RE.search(clean)
    if m:
        return _to_cm(float(m.group(1)), clean), _to_cm(float(m.group(2)), clean)

    return None


# ── Market classifier (returns type + city + date + bucket range) ─────────────

def _classify_market(
    question: str,
) -> Optional[Tuple[MarketType, str, date, str, Tuple[float, float]]]:
    """
    Parse a Polymarket weather question.
    Returns (MarketType, city, target_date, bucket_label, (lower, upper)) or None.
    """
    # Temperature: "Will the highest temperature in {CITY} be {BUCKET} on {DATE}?"
    m = _TEMP_TITLE_RE.match(question)
    if m:
        d = _parse_date(m.group("date"))
        bucket_str = m.group("bucket").strip()
        bucket = _parse_temp_bucket_from_question(bucket_str)
        if d and bucket:
            return MarketType.TEMPERATURE, m.group("city").strip().title(), d, bucket_str, bucket

    # Precipitation: "Will {CITY} have {BUCKET} of precipitation in {MONTH}?"
    m = _PRECIP_TITLE_RE.match(question)
    if m:
        d = _parse_date(m.group("date"))
        bucket_str = m.group("bucket").strip()
        bucket = _parse_precip_bucket_from_question(bucket_str)
        if d and bucket:
            return MarketType.PRECIPITATION, m.group("city").strip().title(), d, bucket_str, bucket

    # Snowfall: "Will {CITY} have {BUCKET} of snowfall in {MONTH}?"
    m = _SNOW_TITLE_RE.match(question)
    if m:
        d = _parse_date(m.group("date"))
        bucket_str = m.group("bucket").strip()
        bucket = _parse_snow_bucket_from_question(bucket_str)
        if d and bucket:
            return MarketType.SNOWFALL, m.group("city").strip().title(), d, bucket_str, bucket

    return None


# ── Gamma API discovery ───────────────────────────────────────────────────────

# Maps MarketType → Gamma events tag_slug
_TAG_SLUG: Dict[str, str] = {
    MarketType.TEMPERATURE.value:   "temperature",
    MarketType.PRECIPITATION.value: "precipitation",
    MarketType.SNOWFALL.value:      "snowfall",
}


def _parse_clob_ids(raw) -> List[str]:
    """clobTokenIds arrives as a JSON string or a list; normalise to list."""
    import json as _json
    if isinstance(raw, str):
        try:
            return _json.loads(raw)
        except Exception:
            return []
    return raw or []


def _parse_res_time(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _events_page(tag_slug: str, offset: int) -> List[dict]:
    """Fetch one page of events from the Gamma /events endpoint."""
    url = f"{settings.gamma_api_host}/events"
    params = {"limit": 100, "offset": offset, "tag_slug": tag_slug, "closed": "false"}
    with httpx.Client(timeout=20) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else data.get("events", [])


@http_retry
def fetch_weather_markets(
    enabled_types: Optional[set] = None,
    **_kwargs,
) -> List[WeatherMarket]:
    """
    Fetch active weather markets from the Gamma /events endpoint.

    Polymarket exposes weather events via tag_slug filters (temperature,
    precipitation, snowfall). Each event contains embedded binary markets —
    one per temperature/precipitation bucket. This is far more efficient
    than scanning all 50 000+ markets on the /markets endpoint.
    """
    if enabled_types is None:
        enabled_types = settings.enabled_market_type_set

    markets: List[WeatherMarket] = []
    seen_ids: set = set()

    for mtype in MarketType:
        if mtype.value not in enabled_types:
            continue

        tag_slug = _TAG_SLUG[mtype.value]
        offset = 0
        event_count = 0

        while True:
            try:
                events = _events_page(tag_slug, offset)
            except Exception as e:
                logger.error(f"Events fetch failed [tag={tag_slug} offset={offset}]: {e}")
                break

            if not events:
                break

            for event in events:
                event_count += 1
                event_end = _parse_res_time(event.get("endDate"))

                for item in event.get("markets", []):
                    if item.get("closed"):
                        continue

                    market_id = item.get("conditionId") or item.get("id", "")
                    if not market_id or market_id in seen_ids:
                        continue

                    question = item.get("question", "") or item.get("title", "")
                    classification = _classify_market(question)
                    if not classification:
                        continue

                    mtype_parsed, city, target_date, bucket_label, (lower, upper) = classification
                    if mtype_parsed != mtype:
                        continue

                    seen_ids.add(market_id)

                    res_time = _parse_res_time(item.get("endDate")) or event_end

                    clob_ids = _parse_clob_ids(item.get("clobTokenIds", []))
                    yes_token_id = clob_ids[0] if clob_ids else ""

                    bucket = WeatherBucket(
                        token_id=yes_token_id,
                        outcome_label=bucket_label,
                        lower=lower,
                        upper=upper,
                    )

                    vol = float(item.get("volume", item.get("volumeNum", 0)) or 0)
                    markets.append(WeatherMarket(
                        market_id=market_id,
                        question=question,
                        city=city,
                        target_date=target_date,
                        resolution_datetime=res_time,
                        market_type=mtype,
                        buckets=[bucket],
                        total_volume_usdc=vol,
                    ))

            logger.debug(f"[{tag_slug}] offset={offset}: {len(events)} events fetched")

            if len(events) < 100:
                break
            offset += 100
            rate_limited_sleep(0.3)

        logger.info(f"[{tag_slug}] scanned {event_count} events, found {sum(1 for m in markets if m.market_type == mtype)} markets")

    by_type: Dict[str, int] = {}
    for m in markets:
        by_type[m.market_type.value] = by_type.get(m.market_type.value, 0) + 1
    logger.info(
        f"Discovered {len(markets)} active weather markets: "
        + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
    )
    return markets


# ── CLOB price enrichment ─────────────────────────────────────────────────────

def _get_clob_client():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if not settings.has_polymarket_key:
            return None

        creds = None
        if settings.polymarket_api_key:
            creds = ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret,
                api_passphrase=settings.polymarket_api_passphrase,
            )
        return ClobClient(
            host=settings.clob_host,
            key=settings.polymarket_private_key,
            chain_id=137,
            creds=creds,
        )
    except Exception as e:
        logger.warning(f"Could not initialize CLOB client: {e}")
        return None


@http_retry
def fetch_live_prices(token_ids: List[str]) -> Dict[str, Dict[str, float]]:
    if not token_ids:
        return {}

    prices: Dict[str, Dict[str, float]] = {}

    clob = _get_clob_client()
    if clob:
        def _fetch_clob(tid: str):
            try:
                ob = clob.get_order_book(tid)
                ask = float(ob.asks[0].price) if ob.asks else 0.0
                bid = float(ob.bids[0].price) if ob.bids else 0.0
                return tid, {"ask": ask, "bid": bid}
            except Exception as e:
                logger.debug(f"CLOB price fetch failed for {tid}: {e}")
                return tid, None

        _workers = min(settings.max_concurrency, len(token_ids), 10)
        try:
            with ThreadPoolExecutor(max_workers=_workers) as pool:
                for tid, result in pool.map(_fetch_clob, token_ids):
                    if result:
                        prices[tid] = result
            if prices:
                return prices
        except Exception as e:
            logger.warning(f"CLOB price fetch error: {e}")

    def _fetch_gamma(tid: str):
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    f"{settings.gamma_api_host}/prices",
                    params={"token_id": tid},
                )
                if resp.status_code == 200:
                    d = resp.json()
                    ask = float(d.get("ask", d.get("price", 0.5)))
                    bid = float(d.get("bid", ask * 0.95))
                    return tid, {"ask": ask, "bid": bid}
        except Exception:
            pass
        return tid, None

    _workers = min(settings.max_concurrency, len(token_ids), 10)
    try:
        with ThreadPoolExecutor(max_workers=_workers) as pool:
            for tid, result in pool.map(_fetch_gamma, token_ids):
                if result:
                    prices[tid] = result
    except Exception as e:
        logger.warning(f"Gamma price fallback error: {e}")

    return prices


def enrich_with_prices(markets: List[WeatherMarket]) -> List[WeatherMarket]:
    all_token_ids = [b.token_id for m in markets for b in m.buckets if b.token_id]
    if not all_token_ids:
        logger.warning("No token IDs available — prices not enriched")
        return markets

    all_prices: Dict[str, Dict[str, float]] = {}
    for i in range(0, len(all_token_ids), 50):
        all_prices.update(fetch_live_prices(all_token_ids[i : i + 50]))

    enriched = 0
    for market in markets:
        for bucket in market.buckets:
            p = all_prices.get(bucket.token_id)
            if p:
                bucket.best_ask = p.get("ask", 0.0)
                bucket.best_bid = p.get("bid", 0.0)
                enriched += 1

    logger.info(f"Enriched {enriched} buckets with live prices")
    return markets


# ── Order placement ───────────────────────────────────────────────────────────

def place_market_order(
    token_id: str,
    side: str,
    size_usdc: float,
    dry_run: bool = True,
) -> Optional[Dict]:
    if dry_run or settings.dry_run:
        logger.info(f"[DRY RUN] Would {side} token {token_id} for ${size_usdc:.2f} USDC")
        return {"status": "dry_run", "token_id": token_id, "side": side,
                "size_usdc": size_usdc, "order_id": "DRY_RUN"}

    clob = _get_clob_client()
    if not clob:
        logger.error("CLOB client unavailable — cannot place order")
        return None

    try:
        from py_clob_client.clob_types import MarketOrderArgs
        args = MarketOrderArgs(token_id=token_id, amount=size_usdc)
        resp = clob.create_market_order(args)
        logger.success(f"Order placed: {side} {token_id} ${size_usdc:.2f} → {resp}")
        return {"status": "placed", "token_id": token_id, "side": side,
                "size_usdc": size_usdc, "response": resp}
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return {"status": "error", "error": str(e)}
