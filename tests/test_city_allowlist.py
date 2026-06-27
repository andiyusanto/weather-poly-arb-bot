"""
Tests for the city_allowlist scanner filter.

The allowlist is the deployment mechanism for the city-level edge found in
resolved-trade analysis: 7 inland/stable-climate cities show +8% ROI across 5/5
weekly cohorts on n=474, vs the full bot's -2% on n=1394. Filtering markets
before forecast fetch saves Open-Meteo quota for cities we won't trade.
"""

from __future__ import annotations

from unittest.mock import patch

from config.settings import settings


def test_empty_allowlist_means_no_filter() -> None:
    # Default state: empty allowlist set → filter is no-op.
    with patch.object(settings, "city_allowlist", ""):
        assert settings.city_allowlist_set == set()


def test_allowlist_parses_and_lowercases() -> None:
    # CSV with mixed case, spaces, trailing comma — normalised to a lowercase set.
    with patch.object(settings, "city_allowlist", "Mexico City, Wuhan ,  GUANGZHOU,"):
        assert settings.city_allowlist_set == {"mexico city", "wuhan", "guangzhou"}


def test_allowlist_match_is_case_insensitive_and_trimmed() -> None:
    # The scanner does `m.city.strip().lower() in allow` — verify the comparison
    # tolerates the same kinds of whitespace/case noise the data source may emit.
    with patch.object(settings, "city_allowlist", "Mexico City,Wuhan"):
        allow = settings.city_allowlist_set
        for city in ("Mexico City", "mexico city", "  Mexico City  ", "WUHAN"):
            assert city.strip().lower() in allow, city
        for city in ("Tokyo", "Istanbul", ""):
            assert city.strip().lower() not in allow, city
