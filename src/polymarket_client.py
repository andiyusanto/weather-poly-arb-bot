"""
Polymarket client — wraps Gamma API (market discovery) and CLOB API
(live prices + order placement via py-clob-client).

Supports temperature, precipitation, and snowfall bucket markets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from config.settings import settings
from src.utils import http_retry, rate_limited_sleep


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

# ── Temperature ──
_TEMP_TITLE_RE = re.compile(
    r"(?:highest|high)\s+temperature\s+in\s+(?P<city>[^,\n]+?)\s+on\s+(?P<date>\w+\s+\d+,?\s*\d{4})",
    re.IGNORECASE,
)

# ── Precipitation ──
_PRECIP_TITLE_RE = re.compile(
    r"(?:precipitation|rainfall?|rain|total\s+precip)\s+(?:in\s+)?(?P<city>[^,\n]+?)\s+on\s+(?P<date>\w+\s+\d+,?\s*\d{4})"
    r"|(?P<city2>[^,\n]+?)\s+(?:precipitation|rainfall?)\s+on\s+(?P<date2>\w+\s+\d+,?\s*\d{4})",
    re.IGNORECASE,
)
_PRECIP_KEYWORD_RE = re.compile(
    r"\b(?:precipitation|rainfall?|total\s+rain|total\s+precip|precip\s+≥|precip\s+>=)\b",
    re.IGNORECASE,
)

# ── Snowfall ──
_SNOW_TITLE_RE = re.compile(
    r"(?:snowfall?|snow\s+accumulation|total\s+snow)\s+(?:in\s+)?(?P<city>[^,\n]+?)\s+on\s+(?P<date>\w+\s+\d+,?\s*\d{4})"
    r"|(?P<city2>[^,\n]+?)\s+(?:snowfall?|snow)\s+on\s+(?P<date2>\w+\s+\d+,?\s*\d{4})",
    re.IGNORECASE,
)
_SNOW_KEYWORD_RE = re.compile(
    r"\b(?:snowfall?|snow\s+accumulation|total\s+snow|inches\s+of\s+snow|cm\s+of\s+snow)\b",
    re.IGNORECASE,
)

# ── Bucket boundary regexes (shared) ──
_RANGE_RE    = re.compile(r"(?P<lo>\d+(?:\.\d+)?)\s*[-–to]+\s*(?P<hi>\d+(?:\.\d+)?)", re.IGNORECASE)
_ABOVE_RE    = re.compile(r"(?:above|over|>\s*|≥\s*|>=\s*)(?P<lo>\d+(?:\.\d+)?)", re.IGNORECASE)
_BELOW_RE    = re.compile(r"(?:below|under|<\s*|≤\s*|<=\s*)(?P<hi>\d+(?:\.\d+)?)", re.IGNORECASE)
_EXACT_RE    = re.compile(r"^(?P<v>\d+(?:\.\d+)?)\s*(?:mm|cm|in(?:ch(?:es)?)?|°[Ff])?$", re.IGNORECASE)
_INCH_MM     = 25.4   # 1 inch in mm
_INCH_CM     = 2.54   # 1 inch in cm (snow)


# ── Date parser ───────────────────────────────────────────────────────────────

def _parse_date(s: str) -> Optional[date]:
    s = s.replace(",", "").strip()
    for fmt in ("%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ── Temperature bucket parser ─────────────────────────────────────────────────

def _parse_temp_bucket(label: str) -> Optional[Tuple[float, float]]:
    m = _RANGE_RE.search(label)
    if m:
        return float(m.group("lo")), float(m.group("hi"))
    m = _ABOVE_RE.search(label)
    if m:
        return float(m.group("lo")), 9999.0
    m = _BELOW_RE.search(label)
    if m:
        return -9999.0, float(m.group("hi"))
    return None


# ── Precipitation bucket parser (normalises to mm) ────────────────────────────

def _to_mm(value: float, label: str) -> float:
    """Convert value to mm if label suggests inches."""
    if re.search(r"\bin(?:ch(?:es)?)?\b", label, re.IGNORECASE):
        return value * _INCH_MM
    return value


def _parse_precip_bucket(label: str) -> Optional[Tuple[float, float]]:
    """
    Parse a precip outcome label into (lower_mm, upper_mm).
    Returns (0.0, 0.01) for 'No rain' / 'Dry' / '0 mm' type labels.
    """
    clean = label.strip()

    # "No rain", "Dry", "No precipitation"
    if re.search(r"\b(?:no\s+(?:rain|precip\w*)|dry|zero\s+precip)\b", clean, re.IGNORECASE):
        return 0.0, 0.01

    # "Any precipitation" / "Yes (rain)" binary YES outcome → treat as ≥0.01
    if re.search(r"\b(?:any\s+precip\w*|yes|rain(?:y)?)\b", clean, re.IGNORECASE):
        return 0.01, 9999.0

    # Exact zero: "0 mm", "0.00 mm", "0 in"
    m = _EXACT_RE.match(clean)
    if m and float(m.group("v")) == 0.0:
        return 0.0, 0.01

    m = _RANGE_RE.search(clean)
    if m:
        lo = _to_mm(float(m.group("lo")), clean)
        hi = _to_mm(float(m.group("hi")), clean)
        return lo, hi

    m = _ABOVE_RE.search(clean)
    if m:
        lo = _to_mm(float(m.group("lo")), clean)
        return lo, 9999.0

    m = _BELOW_RE.search(clean)
    if m:
        hi = _to_mm(float(m.group("hi")), clean)
        # "< 1 mm" typically includes zero
        return 0.0, hi

    # Single value "≥ 1 mm" already handled by ABOVE_RE; catch plain "1 mm"
    m = _EXACT_RE.match(clean)
    if m:
        v = _to_mm(float(m.group("v")), clean)
        return v, v + 1.0  # treat single value as 1-unit wide bucket

    return None


# ── Snowfall bucket parser (normalises to cm) ─────────────────────────────────

def _to_cm(value: float, label: str) -> float:
    if re.search(r"\bin(?:ch(?:es)?)?\b", label, re.IGNORECASE):
        return value * _INCH_CM
    return value


def _parse_snow_bucket(label: str) -> Optional[Tuple[float, float]]:
    """Parse a snowfall outcome label into (lower_cm, upper_cm)."""
    clean = label.strip()

    if re.search(r"\b(?:no\s+snow|zero\s+snow|dry|0\s*cm|0\s*in)\b", clean, re.IGNORECASE):
        return 0.0, 0.01

    if re.search(r"\b(?:any\s+snow|yes|snowy?)\b", clean, re.IGNORECASE):
        return 0.01, 9999.0

    m = _EXACT_RE.match(clean)
    if m and float(m.group("v")) == 0.0:
        return 0.0, 0.01

    m = _RANGE_RE.search(clean)
    if m:
        lo = _to_cm(float(m.group("lo")), clean)
        hi = _to_cm(float(m.group("hi")), clean)
        return lo, hi

    m = _ABOVE_RE.search(clean)
    if m:
        lo = _to_cm(float(m.group("lo")), clean)
        return lo, 9999.0

    m = _BELOW_RE.search(clean)
    if m:
        hi = _to_cm(float(m.group("hi")), clean)
        return 0.0, hi

    m = _EXACT_RE.match(clean)
    if m:
        v = _to_cm(float(m.group("v")), clean)
        return v, v + 1.0

    return None


# ── Market type classifier ────────────────────────────────────────────────────

def _classify_market(question: str, search_term: str) -> Optional[Tuple[MarketType, str, date]]:
    """
    Returns (MarketType, city, date) or None if unparseable.
    Tries temperature → precipitation → snowfall in priority order.
    """
    # Temperature
    m = _TEMP_TITLE_RE.search(question)
    if m:
        d = _parse_date(m.group("date"))
        if d:
            return MarketType.TEMPERATURE, m.group("city").strip().title(), d

    # Precipitation
    if _PRECIP_KEYWORD_RE.search(question) or "precipitation" in search_term or "rain" in search_term:
        m = _PRECIP_TITLE_RE.search(question)
        if m:
            city = (m.group("city") or m.group("city2") or "").strip().title()
            raw_date = (m.group("date") or m.group("date2") or "").strip()
            d = _parse_date(raw_date)
            if city and d:
                return MarketType.PRECIPITATION, city, d

        # Fallback: any market with precipitation keyword + parseable city/date
        if _PRECIP_KEYWORD_RE.search(question):
            # Try generic "in CITY on DATE" extraction
            m2 = re.search(
                r"\bin\s+(?P<city>[A-Z][a-zA-Z\s]+?)\s+on\s+(?P<date>\w+\s+\d+,?\s*\d{4})",
                question, re.IGNORECASE,
            )
            if m2:
                d = _parse_date(m2.group("date"))
                if d:
                    return MarketType.PRECIPITATION, m2.group("city").strip().title(), d

    # Snowfall
    if _SNOW_KEYWORD_RE.search(question) or "snow" in search_term:
        m = _SNOW_TITLE_RE.search(question)
        if m:
            city = (m.group("city") or m.group("city2") or "").strip().title()
            raw_date = (m.group("date") or m.group("date2") or "").strip()
            d = _parse_date(raw_date)
            if city and d:
                return MarketType.SNOWFALL, city, d

        if _SNOW_KEYWORD_RE.search(question):
            m2 = re.search(
                r"\bin\s+(?P<city>[A-Z][a-zA-Z\s]+?)\s+on\s+(?P<date>\w+\s+\d+,?\s*\d{4})",
                question, re.IGNORECASE,
            )
            if m2:
                d = _parse_date(m2.group("date"))
                if d:
                    return MarketType.SNOWFALL, m2.group("city").strip().title(), d

    return None


# ── Gamma API discovery ───────────────────────────────────────────────────────

# All search terms to scan; mapped to a hint for classifier
_SEARCH_TERMS: Dict[str, str] = {
    "highest temperature": "temperature",
    "high temperature": "temperature",
    "precipitation in": "precipitation",
    "total precipitation": "precipitation",
    "rainfall in": "precipitation",
    "rain in": "precipitation",
    "precipitation >=": "precipitation",
    "precipitation ≥": "precipitation",
    "snowfall in": "snowfall",
    "total snowfall": "snowfall",
    "snow in": "snowfall",
    "inches of snow": "snowfall",
}


def _parse_buckets_for_type(
    market_type: MarketType,
    tokens: list,
) -> List[WeatherBucket]:
    buckets: List[WeatherBucket] = []
    for tok in tokens:
        if isinstance(tok, str):
            outcome_label, token_id = tok, ""
        else:
            outcome_label = tok.get("outcome", tok.get("label", ""))
            token_id = tok.get("token_id", tok.get("id", ""))

        if market_type == MarketType.TEMPERATURE:
            parsed = _parse_temp_bucket(outcome_label)
        elif market_type == MarketType.PRECIPITATION:
            parsed = _parse_precip_bucket(outcome_label)
        else:
            parsed = _parse_snow_bucket(outcome_label)

        if parsed:
            buckets.append(WeatherBucket(
                token_id=token_id,
                outcome_label=outcome_label,
                lower=parsed[0],
                upper=parsed[1],
            ))
    return buckets


@http_retry
def fetch_weather_markets(
    limit: int = 200,
    enabled_types: Optional[set] = None,
) -> List[WeatherMarket]:
    """
    Query Gamma API for all active weather markets (temperature + precipitation + snowfall).
    Filters by enabled_types (default: all from settings).
    """
    if enabled_types is None:
        enabled_types = settings.enabled_market_type_set

    markets: List[WeatherMarket] = []
    seen_ids: set = set()

    for term, hint in _SEARCH_TERMS.items():
        # Skip terms for disabled market types
        if hint not in enabled_types:
            continue

        offset = 0
        while True:
            url = f"{settings.gamma_api_host}/markets"
            params = {
                "limit": min(limit, 100),
                "offset": offset,
                "active": "true",
                "closed": "false",
                "q": term,
            }
            try:
                with httpx.Client(timeout=20) as client:
                    resp = client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as e:
                logger.error(f"Gamma API fetch failed [{term}]: {e}")
                break

            items = data if isinstance(data, list) else data.get("markets", data.get("data", []))
            if not items:
                break

            for item in items:
                market_id = item.get("conditionId") or item.get("id", "")
                if not market_id or market_id in seen_ids:
                    continue

                question = item.get("question", "") or item.get("title", "")
                classification = _classify_market(question, hint)

                if not classification:
                    # Try description field as fallback
                    question = item.get("description", "")
                    classification = _classify_market(question, hint)

                if not classification:
                    continue

                market_type, city, target_date = classification

                # Respect enabled_types filter
                if market_type.value not in enabled_types:
                    continue

                seen_ids.add(market_id)

                # Parse resolution time
                res_time = None
                for key in ("endDate", "end_date", "resolveTime", "resolution_time"):
                    if item.get(key):
                        try:
                            ts = item[key]
                            if isinstance(ts, (int, float)):
                                res_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                            else:
                                res_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        except Exception:
                            pass
                        if res_time:
                            break

                tokens = item.get("tokens", item.get("outcomes", []))
                buckets = _parse_buckets_for_type(market_type, tokens)

                if not buckets:
                    logger.debug(f"Skipping [{market_type.value}] {question}: no parseable buckets")
                    continue

                vol = float(item.get("volume", item.get("volumeNum", 0)) or 0)
                markets.append(WeatherMarket(
                    market_id=market_id,
                    question=question,
                    city=city,
                    target_date=target_date,
                    resolution_datetime=res_time,
                    market_type=market_type,
                    buckets=buckets,
                    total_volume_usdc=vol,
                ))

            if len(items) < params["limit"]:
                break
            offset += params["limit"]
            rate_limited_sleep(0.5)

    by_type = {}
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
        try:
            for tid in token_ids:
                try:
                    ob = clob.get_order_book(tid)
                    ask = float(ob.asks[0].price) if ob.asks else 0.0
                    bid = float(ob.bids[0].price) if ob.bids else 0.0
                    prices[tid] = {"ask": ask, "bid": bid}
                    rate_limited_sleep(0.1)
                except Exception as e:
                    logger.debug(f"CLOB price fetch failed for {tid}: {e}")
            if prices:
                return prices
        except Exception as e:
            logger.warning(f"CLOB price fetch error: {e}")

    try:
        with httpx.Client(timeout=15) as client:
            for tid in token_ids:
                try:
                    resp = client.get(
                        f"{settings.gamma_api_host}/prices",
                        params={"token_id": tid},
                    )
                    if resp.status_code == 200:
                        d = resp.json()
                        ask = float(d.get("ask", d.get("price", 0.5)))
                        bid = float(d.get("bid", ask * 0.95))
                        prices[tid] = {"ask": ask, "bid": bid}
                    rate_limited_sleep(0.2)
                except Exception:
                    pass
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
