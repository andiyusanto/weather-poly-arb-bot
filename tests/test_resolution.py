"""
Unit tests for shadow-trade resolution parsing.

Covers ``_winner_outcome`` (the pure CLOB ``tokens[]`` grader) and
``fetch_market_resolution`` (HTTP wrapper, mocked). Fixtures are the *real*
CLOB payloads captured from the Tokyo server for two May-18 buckets:
  - Beijing 24°C  → finalized, "No" won (winner flag + price 1)
  - Jakarta 32°C  → trading stopped but NOT finalized (no winner)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.polymarket_client import _winner_outcome, fetch_market_resolution

# ── Real captured payloads ─────────────────────────────────────────────────────

BEIJING_TOKENS = [
    {"token_id": "1010943819...", "outcome": "Yes", "price": 0, "winner": False},
    {"token_id": "6183649765...", "outcome": "No", "price": 1, "winner": True},
]

JAKARTA_TOKENS = [  # closed:false, active:false, neither token is a winner yet
    {"token_id": "8044282378...", "outcome": "Yes", "price": 0.0475, "winner": False},
    {"token_id": "9480608316...", "outcome": "No", "price": 0.9525, "winner": False},
]


# ── _winner_outcome ─────────────────────────────────────────────────────────────

def test_finalized_no_wins_via_winner_flag() -> None:
    assert _winner_outcome(BEIJING_TOKENS) == "no"


def test_pending_market_returns_none() -> None:
    # 0.9525 is below the 0.99 finalization threshold and winner flags are false.
    assert _winner_outcome(JAKARTA_TOKENS) is None


def test_yes_wins_via_price_only() -> None:
    # winner flag absent; price ≈ 1 must still grade as a win.
    tokens = [
        {"outcome": "Yes", "price": 1.0},
        {"outcome": "No", "price": 0.0},
    ]
    assert _winner_outcome(tokens) == "yes"


def test_empty_or_malformed_tokens() -> None:
    assert _winner_outcome([]) is None
    assert _winner_outcome(None) is None  # type: ignore[arg-type]
    assert _winner_outcome([{"outcome": "Maybe", "price": 1}]) is None


# ── fetch_market_resolution (HTTP mocked) ───────────────────────────────────────

def _mock_client(payload: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    client = MagicMock()
    client.get.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    return ctx


def test_fetch_resolution_finalized() -> None:
    with patch("src.polymarket_client.httpx.Client", return_value=_mock_client({"tokens": BEIJING_TOKENS})):
        assert fetch_market_resolution("0xabc") == "no"


def test_fetch_resolution_pending() -> None:
    with patch("src.polymarket_client.httpx.Client", return_value=_mock_client({"tokens": JAKARTA_TOKENS})):
        assert fetch_market_resolution("0xabc") is None


def test_fetch_resolution_http_error() -> None:
    with patch("src.polymarket_client.httpx.Client", return_value=_mock_client({}, status=422)):
        assert fetch_market_resolution("0xabc") is None


def test_fetch_resolution_empty_condition() -> None:
    assert fetch_market_resolution("") is None
