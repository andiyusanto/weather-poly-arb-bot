"""
Tests for TradeStore.traded_bucket_keys — the basis of one-bet-per-bucket dedup.
"""

from __future__ import annotations

from pathlib import Path

from src.utils import TradeStore


def _store(tmp_path: Path) -> TradeStore:
    return TradeStore(tmp_path / "trades.db")


def test_traded_bucket_keys_roundtrip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.record(dict(city="Madrid", target_date="2026-05-27", bucket_label="31°C",
                  side="yes", shadow=1, size_usdc=10, model_prob=0.3, market_price=0.1, ev=2.0,
                  market_id="m1", token_id="t1", confidence=0.8, dry_run=0))
    s.record(dict(city="Tokyo", target_date="2026-05-27", bucket_label="19°C",
                  side="no", shadow=1, size_usdc=10, model_prob=0.3, market_price=0.1, ev=2.0,
                  market_id="m2", token_id="t2", confidence=0.8, dry_run=0))
    keys = s.traded_bucket_keys()
    # Keys are 5-tuples: (city, date, bucket, side, contrarian_int).
    assert ("Madrid", "2026-05-27", "31°C", "yes", 0) in keys
    assert ("Tokyo", "2026-05-27", "19°C", "no", 0) in keys
    assert ("Madrid", "2026-05-27", "31°C", "no", 0) not in keys  # different side


def test_dedup_filter_logic(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.record(dict(city="Madrid", target_date="2026-05-27", bucket_label="31°C",
                  side="yes", shadow=1, size_usdc=10, model_prob=0.3, market_price=0.1, ev=2.0,
                  market_id="m1", token_id="t1", confidence=0.8, dry_run=0))
    traded = s.traded_bucket_keys()

    # Simulate two opportunities: one already traded, one fresh.
    candidates = [
        ("Madrid", "2026-05-27", "31°C", "yes", 0),  # dup
        ("Madrid", "2026-05-27", "32°C", "yes", 0),  # fresh
    ]
    fresh = [c for c in candidates if c not in traded]
    assert fresh == [("Madrid", "2026-05-27", "32°C", "yes", 0)]


def test_contrarian_and_natural_no_on_same_bucket_are_distinct(tmp_path: Path) -> None:
    # The fix that unblocked Option F on legacy-NO-saturated histories: a prior
    # natural NO must NOT dedup-block a fresh contrarian NO on the same bucket
    # (and vice versa). They are different strategies, same outcome direction.
    s = _store(tmp_path)
    s.record(dict(city="Tokyo", target_date="2026-06-23", bucket_label="19°C",
                  side="no", contrarian=0, shadow=1, size_usdc=10, model_prob=0.8,
                  market_price=0.6, ev=0.3, market_id="m1", token_id="t1",
                  confidence=0.7, dry_run=0))
    traded = s.traded_bucket_keys()
    assert ("Tokyo", "2026-06-23", "19°C", "no", 0) in traded
    assert ("Tokyo", "2026-06-23", "19°C", "no", 1) not in traded  # contrarian still allowed
