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
    WIND_SPEED    = "wind_speed"

    @property
    def emoji(self) -> str:
        return {
            "temperature": "🌡️",
            "precipitation": "🌧️",
            "snowfall": "❄️",
            "wind_speed": "💨",
        }[self.value]

    @property
    def unit_label(self) -> str:
        return {
            "temperature": "°F",
            "precipitation": "mm",
            "snowfall": "cm",
            "wind_speed": "mph",
        }[self.value]


# ── Unified bucket dataclass ──────────────────────────────────────────────────

@dataclass
class WeatherBucket:
    """
    Generic weather market bucket. `lower` and `upper` are in the market's
    native unit (°F for temperature, mm for precipitation, cm for snowfall).
    Open-ended buckets use ±9999 as sentinels.

    A bucket maps to one binary Polymarket market (its own conditionId) with
    YES and NO tokens. For tradability we track both sides' best ask.
    """
    token_id: str            # YES token id (clobTokenIds[0])
    outcome_label: str
    lower: float             # inclusive lower bound in native unit
    upper: float             # exclusive upper bound in native unit
    condition_id: str = ""   # Polymarket conditionId (for resolution lookups)
    no_token_id: str = ""    # NO token id (clobTokenIds[1])
    best_ask: float = 0.0    # YES best ask
    best_bid: float = 0.0    # YES best bid
    best_ask_no: float = 0.0 # NO best ask (approx 1 - best_bid_yes when missing)
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

    @property
    def no_ask(self) -> float:
        """Best ask for the NO token. Falls back to (1 - YES bid) when missing."""
        if self.best_ask_no > 0:
            return self.best_ask_no
        if self.best_bid > 0:
            return max(0.01, min(0.99, 1.0 - self.best_bid))
        return 0.0


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

# Wind speed: "Will the (maximum) wind speed in {CITY} be {BUCKET} (mph|km/h) on {DATE}?"
_WIND_TITLE_RE = re.compile(
    r"Will the (?:maximum |max |average |avg )?wind (?:speed|gust|gusts?) in "
    r"(?P<city>[A-Za-z][A-Za-z\s]+?) be (?P<bucket>[^?]+?) on (?P<date>[A-Za-z]+ \d+)\?",
    re.IGNORECASE,
)

# Shared helpers
_RANGE_RE  = re.compile(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)")
_INCH_MM   = 25.4
_INCH_CM   = 2.54
_KPH_TO_MPH = 0.621371


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


def _to_mph(value: float, label: str) -> float:
    """Convert wind value to mph; treats bare numbers and 'mph' as-is, converts 'km/h' or 'kph'."""
    if re.search(r"\bk(?:m(?:/h|ph)?|ph)\b", label, re.IGNORECASE):
        return value * _KPH_TO_MPH
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


def _parse_wind_bucket_from_question(bucket_str: str) -> Optional[Tuple[float, float]]:
    """
    Parse wind speed range from the bucket portion of a title.
    Returns (lower_mph, upper_mph). Converts km/h if unit label present.
    """
    clean = bucket_str.strip()

    if re.search(r"\b(?:less than|below|under)\b", clean, re.IGNORECASE):
        m = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m:
            return 0.0, _to_mph(float(m.group(1)), clean)

    if re.search(r"\b(?:more than|or more|over|above|or higher)\b", clean, re.IGNORECASE):
        m = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m:
            return _to_mph(float(m.group(1)), clean), 9999.0

    m = re.search(r"between\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)", clean, re.IGNORECASE)
    if m:
        return _to_mph(float(m.group(1)), clean), _to_mph(float(m.group(2)), clean)

    m = _RANGE_RE.search(clean)
    if m:
        return _to_mph(float(m.group(1)), clean), _to_mph(float(m.group(2)), clean)

    # Exact single value (e.g. "15 mph") — treat as [v, v+5) 5-mph bin
    m2 = re.search(r"(\d+(?:\.\d+)?)", clean)
    if m2:
        v = _to_mph(float(m2.group(1)), clean)
        return v, v + 5.0

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

    # Wind speed: "Will the (max) wind speed in {CITY} be {BUCKET} on {DATE}?"
    m = _WIND_TITLE_RE.match(question)
    if m:
        d = _parse_date(m.group("date"))
        bucket_str = m.group("bucket").strip()
        bucket = _parse_wind_bucket_from_question(bucket_str)
        if d and bucket:
            return MarketType.WIND_SPEED, m.group("city").strip().title(), d, bucket_str, bucket

    return None


# ── Gamma API discovery ───────────────────────────────────────────────────────

# Gamma tag_slugs that actually contain weather markets (verified against live API).
# - "weather" hosts temperature, wind, and snow markets (and a few hurricane events)
# - "precipitation" is a separate slug for monthly precip markets
# Per-type classification is done by question-title regex, not by slug.
_DISCOVERY_SLUGS: List[str] = ["weather", "precipitation"]


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


def _safe_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@http_retry
def fetch_weather_markets(
    enabled_types: Optional[set] = None,
    **_kwargs,
) -> List[WeatherMarket]:
    """
    Fetch active weather markets from the Gamma /events endpoint.

    Polymarket groups weather markets under two tag slugs: "weather"
    (temperature/wind/snow/hurricane) and "precipitation" (monthly precip).
    Per-market type is determined by the question-title regex, so a single
    union query is sufficient — each market lands in the right MarketType
    or is skipped if it does not match any pattern.

    Bid/ask/volume are read directly from the event payload, eliminating a
    second round-trip to a non-existent /prices endpoint.
    """
    if enabled_types is None:
        enabled_types = settings.enabled_market_type_set

    # Group buckets per event — buckets within an event are mutually exclusive,
    # so keeping them grouped is critical for correct probability normalization.
    grouped: Dict[str, WeatherMarket] = {}
    seen_condition_ids: set = set()
    skipped_closed = 0
    skipped_unpriced = 0
    skipped_unparseable = 0

    for tag_slug in _DISCOVERY_SLUGS:
        offset = 0
        events_seen = 0

        while True:
            try:
                events = _events_page(tag_slug, offset)
            except Exception as e:
                logger.error(f"Events fetch failed [tag={tag_slug} offset={offset}]: {e}")
                break

            if not events:
                break

            for event in events:
                events_seen += 1
                event_id = str(event.get("id") or event.get("slug") or "")
                event_end = _parse_res_time(event.get("endDate"))

                for item in event.get("markets", []):
                    condition_id = item.get("conditionId") or item.get("id", "")
                    if not condition_id or condition_id in seen_condition_ids:
                        continue

                    if item.get("closed"):
                        skipped_closed += 1
                        continue

                    question = item.get("question", "") or item.get("title", "")
                    classification = _classify_market(question)
                    if not classification:
                        skipped_unparseable += 1
                        continue

                    mtype_parsed, city, target_date, bucket_label, (lower, upper) = classification
                    if mtype_parsed.value not in enabled_types:
                        continue

                    best_ask = _safe_float(item.get("bestAsk"))
                    best_bid = _safe_float(item.get("bestBid"))

                    # Fallback to outcomePrices if bestAsk missing (Yes is index 0).
                    no_mid = 0.0
                    op = item.get("outcomePrices")
                    if isinstance(op, str):
                        try:
                            import json as _json
                            op = _json.loads(op)
                        except Exception:
                            op = None
                    if isinstance(op, list) and len(op) >= 2:
                        if best_ask <= 0:
                            best_ask = _safe_float(op[0])
                        no_mid = _safe_float(op[1])

                    if best_ask <= 0 or best_ask >= 1:
                        skipped_unpriced += 1
                        continue

                    seen_condition_ids.add(condition_id)

                    res_time = _parse_res_time(item.get("endDate")) or event_end
                    if res_time:
                        res_date = res_time.date()
                        if res_date.month == target_date.month and res_date.day == target_date.day:
                            target_date = res_date

                    clob_ids = _parse_clob_ids(item.get("clobTokenIds", []))
                    yes_token_id = clob_ids[0] if clob_ids else ""
                    no_token_id = clob_ids[1] if len(clob_ids) > 1 else ""

                    bucket_vol = _safe_float(
                        item.get("volume24hrClob")
                        or item.get("volume24hr")
                        or item.get("liquidityNum")
                        or item.get("liquidity")
                    )

                    bucket = WeatherBucket(
                        token_id=yes_token_id,
                        no_token_id=no_token_id,
                        condition_id=condition_id,
                        outcome_label=bucket_label,
                        lower=lower,
                        upper=upper,
                        best_ask=best_ask,
                        best_bid=best_bid,
                        best_ask_no=no_mid if no_mid > 0 else 0.0,
                        volume_usdc=bucket_vol,
                    )

                    # Group by (event_id, type, city, date) — same event_id can host
                    # multiple types if a creator splits oddly, so include all keys.
                    group_key = f"{event_id}|{mtype_parsed.value}|{city}|{target_date.isoformat()}"
                    if group_key in grouped:
                        grouped[group_key].buckets.append(bucket)
                        grouped[group_key].total_volume_usdc += _safe_float(
                            item.get("volume") or item.get("volumeNum")
                        )
                    else:
                        grouped[group_key] = WeatherMarket(
                            market_id=event_id or condition_id,
                            question=event.get("title") or question,
                            city=city,
                            target_date=target_date,
                            resolution_datetime=res_time,
                            market_type=mtype_parsed,
                            buckets=[bucket],
                            total_volume_usdc=_safe_float(
                                item.get("volume") or item.get("volumeNum")
                            ),
                        )

            logger.debug(f"[{tag_slug}] offset={offset}: {len(events)} events fetched")

            if len(events) < 100:
                break
            offset += 100
            rate_limited_sleep(0.3)

        logger.info(f"[{tag_slug}] scanned {events_seen} events")

    markets = list(grouped.values())
    n_buckets = sum(len(m.buckets) for m in markets)
    by_type: Dict[str, int] = {}
    for m in markets:
        by_type[m.market_type.value] = by_type.get(m.market_type.value, 0) + 1
    logger.info(
        f"Discovered {len(markets)} weather events ({n_buckets} buckets): "
        + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
        + f" (skipped: closed={skipped_closed} unpriced={skipped_unpriced} "
        f"unparseable={skipped_unparseable})"
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
        if settings.poly_api_key:
            creds = ApiCreds(
                api_key=settings.poly_api_key,
                api_secret=settings.poly_api_secret,
                api_passphrase=settings.poly_api_passphrase,
            )

        return ClobClient(
            host=settings.clob_host,
            key=settings.poly_private_key,
            chain_id=137,
            funder=settings.poly_funder_address or None,
            signature_type=settings.poly_sig_type,
            creds=creds,
        )
    except Exception as e:
        logger.warning(f"Could not initialize CLOB client: {e}")
        return None


@http_retry
def fetch_live_prices(token_ids: List[str]) -> Dict[str, Dict[str, float]]:
    """
    Public CLOB price fallback. Used only when a bucket arrives without a
    price from Gamma (rare). The CLOB /price endpoint is public and needs
    no credentials.

    Endpoint: GET {clob_host}/price?token_id=X&side=BUY|SELL → {"price": "0.42"}
    """
    if not token_ids:
        return {}

    prices: Dict[str, Dict[str, float]] = {}

    def _fetch_one(tid: str):
        try:
            with httpx.Client(timeout=10) as client:
                ask_resp = client.get(f"{settings.clob_host}/price",
                                      params={"token_id": tid, "side": "BUY"})
                bid_resp = client.get(f"{settings.clob_host}/price",
                                      params={"token_id": tid, "side": "SELL"})
                ask = float(ask_resp.json().get("price", 0.0)) if ask_resp.status_code == 200 else 0.0
                bid = float(bid_resp.json().get("price", 0.0)) if bid_resp.status_code == 200 else 0.0
                if ask > 0 or bid > 0:
                    return tid, {"ask": ask, "bid": bid}
        except Exception as e:
            logger.debug(f"CLOB /price fetch failed for {tid}: {e}")
        return tid, None

    workers = min(settings.max_concurrency, len(token_ids), 10)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for tid, result in pool.map(_fetch_one, token_ids):
                if result:
                    prices[tid] = result
    except Exception as e:
        logger.warning(f"CLOB price fetch error: {e}")

    return prices


def enrich_with_prices(markets: List[WeatherMarket]) -> List[WeatherMarket]:
    """
    Most buckets come pre-priced from the Gamma payload. Backfill any that
    arrived unpriced via the public CLOB /price endpoint.
    """
    needing_price = [
        b.token_id
        for m in markets
        for b in m.buckets
        if b.token_id and b.best_ask <= 0
    ]
    if not needing_price:
        logger.debug("All buckets already priced from Gamma payload")
        return markets

    logger.info(f"Backfilling prices for {len(needing_price)} unpriced buckets…")

    all_prices: Dict[str, Dict[str, float]] = {}
    for i in range(0, len(needing_price), 50):
        all_prices.update(fetch_live_prices(needing_price[i : i + 50]))

    backfilled = 0
    for market in markets:
        for bucket in market.buckets:
            if bucket.best_ask > 0:
                continue
            p = all_prices.get(bucket.token_id)
            if p:
                bucket.best_ask = p.get("ask", 0.0)
                bucket.best_bid = p.get("bid", 0.0)
                if bucket.best_ask > 0:
                    backfilled += 1

    logger.info(f"Backfilled {backfilled}/{len(needing_price)} bucket prices")
    return markets


# ── Market resolution ────────────────────────────────────────────────────────

def _winner_outcome(tokens: list) -> Optional[str]:
    """
    Determine the winning outcome of a binary bucket from CLOB ``tokens[]``.

    A token wins when ``winner is True`` or its ``price`` is ≈ 1. Returns
    'yes'/'no' for the winning side, or None when no token has resolved
    (market still pending finalization) or the payload is malformed.
    """
    if not isinstance(tokens, list) or not tokens:
        return None

    def _is_winner(tok: dict) -> bool:
        if tok.get("winner") is True:
            return True
        return _safe_float(tok.get("price")) >= 0.99

    winning = next((t for t in tokens if isinstance(t, dict) and _is_winner(t)), None)
    if winning is None:
        return None
    outcome = (winning.get("outcome") or "").strip().lower()
    return outcome if outcome in ("yes", "no") else None


def fetch_market_resolution(condition_id: str) -> Optional[str]:
    """
    Query the CLOB for the resolution outcome of a single binary bucket market.

    Resolution is read from the CLOB ``/markets/{conditionId}`` endpoint, whose
    ``tokens[]`` carry a per-token ``winner`` flag (and ``price`` 1/0). This is
    the only source that works by conditionId and is *resolution-engine-agnostic*
    — it reflects the final outcome whether the market was graded by UMA's
    optimistic oracle, Chainlink Data Streams, or the Polymarket team.

    We deliberately do NOT gate on ``closed``/``active``: observed payloads carry
    inconsistent combinations (e.g. ``closed:true, active:true`` on a finalized
    market). The unambiguous signal is a token with ``winner == true`` (or
    ``price`` ≈ 1). Absent that, the market is treated as still-pending — which
    correctly handles the MOOV2 propose→dispute→finalize window where trading has
    stopped but the outcome is not yet final.

    Args:
        condition_id: The market's hex conditionId (stored in ``trades.condition_id``).

    Returns:
        'yes' if the market's "Yes" outcome won, 'no' if "No" won, or None when
        the market is not yet finalized or the lookup failed.
    """
    if not condition_id:
        return None
    with httpx.Client(timeout=15) as client:
        return _resolution_via_client(client, condition_id)


def _resolution_via_client(client: httpx.Client, condition_id: str) -> Optional[str]:
    """Resolve a single conditionId reusing an existing (keep-alive) client."""
    if not condition_id:
        return None
    try:
        resp = client.get(f"{settings.clob_host}/markets/{condition_id}")
        if resp.status_code != 200:
            logger.debug(f"Resolution fetch HTTP {resp.status_code} [{condition_id}]")
            return None
        data = resp.json()
        if not isinstance(data, dict):
            logger.debug(f"Resolution fetch: unexpected payload type [{condition_id}]")
            return None
        # None ⇒ not yet finalized (trading may have stopped); a winner ⇒ 'yes'/'no'.
        return _winner_outcome(data.get("tokens") or [])
    except Exception as e:
        logger.debug(f"Resolution fetch failed [{condition_id}]: {e}")
        return None


def fetch_market_resolutions(
    condition_ids: List[str], max_workers: int = 5
) -> Dict[str, Optional[str]]:
    """
    Resolve many conditionIds at once, deduped and concurrently.

    Uses one keep-alive ``httpx.Client`` shared across a small thread pool so
    TLS/DNS is reused and we stay well under CLOB rate limits. Duplicate ids are
    fetched only once. Returns ``{condition_id: 'yes'|'no'|None}`` for every
    unique id (None ⇒ unresolved/failed).

    Args:
        condition_ids: Hex conditionIds (duplicates allowed; deduped internally).
        max_workers: Concurrent requests. Kept low (default 5) — CLOB throttles.
    """
    unique = [c for c in dict.fromkeys(condition_ids) if c]
    if not unique:
        return {}

    results: Dict[str, Optional[str]] = {}
    limits = httpx.Limits(max_keepalive_connections=max_workers, max_connections=max_workers)
    with httpx.Client(timeout=15, limits=limits) as client:
        def _one(cid: str) -> Tuple[str, Optional[str]]:
            return cid, _resolution_via_client(client, cid)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for cid, outcome in pool.map(_one, unique):
                results[cid] = outcome
    return results


# ── Order placement ───────────────────────────────────────────────────────────

def place_market_order(
    token_id: str,
    side: str,
    size_usdc: float,
    dry_run: bool = True,
    expected_quote: Optional[float] = None,
    model_prob: Optional[float] = None,
    min_ev: Optional[float] = None,
) -> Optional[Dict]:
    """
    Place a market BUY for the given token. To buy YES on a bucket, pass the
    YES token id; to buy NO, pass the NO token id (clobTokenIds[1]). The
    `side` argument is a label for logging/recording — the actual side at the
    CLOB is always BUY.

    Routing:
      * dry_run / settings.dry_run / shadow → log-only short-circuit
      * settings.use_sdk_executor=True → deposit-wallet SDK path (post-V2)
      * otherwise → legacy py-clob direct-EOA path (kept as fallback)
    """
    if dry_run or settings.dry_run:
        logger.info(f"[DRY RUN] Would BUY {side} token {token_id} for ${size_usdc:.2f} USDC")
        return {"status": "dry_run", "token_id": token_id, "side": side,
                "size_usdc": size_usdc, "order_id": "DRY_RUN"}

    # ── Deposit-wallet SDK path (Polymarket V2, post-2026-04-28) ────────────
    # The legacy direct-EOA path below stopped working for new wallets after
    # the V2 cutover. The SDK signs deposit-wallet (POLY_1271) orders through
    # the relayer. See src/sdk_executor.py + the bear-bot reference. The async
    # bridge is intentional: weather bot's trading loop is sync, the SDK is
    # async-first; per-call asyncio.run() is fine at ~10-20 orders/day.
    if settings.use_sdk_executor:
        import asyncio
        from src.sdk_executor import sdk_place_market_order
        try:
            return asyncio.run(sdk_place_market_order(
                token_id, side, size_usdc,
                expected_quote=expected_quote,
                model_prob=model_prob,
                min_ev=min_ev,
            ))
        except Exception as e:
            logger.error(f"SDK execution failed (token={token_id}, side={side}): {e}")
            return {"status": "error", "error": str(e), "path": "sdk"}

    # ── Legacy py-clob direct-EOA path (kept for grandfathered wallets) ─────
    clob = _get_clob_client()
    if not clob:
        logger.error("CLOB client unavailable — cannot place order")
        return None

    try:
        from py_clob_client.clob_types import MarketOrderArgs
        args = MarketOrderArgs(token_id=token_id, amount=size_usdc)
        resp = clob.create_market_order(args)
        logger.success(f"Order placed: BUY {side} {token_id} ${size_usdc:.2f} → {resp}")
        return {"status": "placed", "token_id": token_id, "side": side,
                "size_usdc": size_usdc, "response": resp}
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return {"status": "error", "error": str(e), "path": "legacy"}
