"""Central configuration — all values override-able via .env."""

from __future__ import annotations

from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"
CONFIG_DIR = ROOT_DIR / "config"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

CITIES_CACHE_DB = DATA_DIR / "cities_cache.db"
BIAS_DB = DATA_DIR / "bias_corrections.db"
TRADES_DB = DATA_DIR / "trades.db"
CITIES_YAML = CONFIG_DIR / "cities.yaml"

VALID_MARKET_TYPES = {"temperature", "precipitation", "snowfall", "wind_speed"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Polymarket ──────────────────────────────────────────────────────────
    # Written by setup.py — do not edit manually
    poly_private_key: str = ""           # Magic signer EOA — same field both paths
    poly_funder_address: str = ""        # legacy: EOA; SDK: deposit wallet (POLY_1271)
    poly_api_key: str = ""               # legacy CLOB Level-2 API key
    poly_api_secret: str = ""            # legacy
    poly_api_passphrase: str = ""        # legacy
    poly_sig_type: int = 0               # legacy: 0=EOA, 1=safe, 2=gnosis. SDK path ignores.
    clob_host: str = "https://clob.polymarket.com"
    gamma_api_host: str = "https://gamma-api.polymarket.com"

    # ── Deposit-wallet SDK live path (Polymarket V2, post-2026-04-28) ───────
    # Polymarket V2 no longer permits NEW direct-EOA wallets to trade — every
    # fresh EOA returns "400 maker address not allowed, please use the deposit
    # wallet flow". The deposit-wallet flow is implemented in the official
    # `polymarket-client` SDK (AsyncSecureClient). Set this true to route
    # `place_market_order` through src/sdk_executor.py; the legacy py-clob
    # path stays as a fallback and runs whenever this is false. The reference
    # implementation is bear-oracle-confirmed-sniper/execution/sdk_executor.py
    # (already live + profitable on the same wallet schema).
    #
    # When this is true, POLY_FUNDER_ADDRESS must be the DEPOSIT WALLET
    # (the profile "Address — For API use only" field), NOT the Magic signer
    # EOA, NOT the on-ramp "Transfer Crypto" address. Verify with
    # `python3 derive_wallet.py` from the ~/weatherlive venv before any live order.
    use_sdk_executor: bool = False

    # Builder API key from Polymarket → Settings → API Keys. Required when
    # use_sdk_executor=true. The SDK signs orders THROUGH the relayer as the
    # deposit wallet using these credentials.
    poly_builder_api_key: str = ""
    poly_builder_secret: str = ""
    poly_builder_passphrase: str = ""
    poly_builder_code: str = ""          # optional fee-discount code

    # Polymarket taker fee for the weather category is 1.25% (crypto is 1.8%).
    # sdk_executor folds this into the recorded trade size so downstream PnL
    # reflects the real post-fee spend. Limit/maker orders are free but we
    # always send FOK market orders today, so this always applies on the live
    # SDK path. The legacy py-clob path doesn't use this (it reports raw size).
    taker_fee_pct: float = 1.25

    # Slippage tax applied to the observed best-ask before EV is computed. Real
    # FOK market fills land ~1–2¢ above the top-of-book quote (empirically
    # measured against SDK fills; see 2026-06-29→07-01 live sample). Without a
    # tax the bot enters marginal trades whose true EV is ~0 post-slippage. Set
    # to 0.0 to disable. Expressed in probability units (0.02 = +2¢ on the ask).
    slippage_tax: float = 0.02

    # ── Live pre-order slippage abort (Opt 1) ───────────────────────────────
    # Right before firing a live order, the SDK executor calls
    # estimate_market_price and compares that estimate to the quoted ask that
    # produced the trade decision. If the estimate exceeds the quote by more
    # than ``max_pre_order_slip`` cents, or if EV recomputed against the
    # estimate falls below the min-EV threshold, the order is aborted before
    # any funds move. Empirically, illiquid weather buckets (Wuhan 30–31°C,
    # Manila 32°C) show 11–19¢ real slippage vs the quote — those are the
    # trades this gate catches. Set to a large value (e.g. 1.0) to disable.
    max_pre_order_slip: float = 0.05
    # Also require the re-computed EV to clear this threshold. Defaults to
    # ``min_ev_threshold`` (below) — separate knob so illiquid buckets can be
    # held to a stricter bar without changing the primary EV floor.
    pre_order_min_ev: float = 0.20

    # ── Mode-bucket NO gate (Opt 2) ─────────────────────────────────────────
    # For a NO bet on a temperature bucket whose center sits within
    # ``mode_bucket_c_radius`` degrees Celsius of the forecast mean, require
    # ``model_prob`` at least this high. Blocks coin-flip mode-bucket trades
    # where the model's ~0.60 probability is empirically ~0.40 (Wuhan 29°C,
    # Moscow 26°C, Manila 29°C on the live tape). Set to 0.0 to disable.
    mode_bucket_no_min_prob: float = 0.75
    mode_bucket_c_radius: float = 1.0

    # ── Raw-KDE bypass (Fix 1 — calibration curve corrupted 2026-07-02) ─────
    # The isotonic calibration curve for temperature was fit on ~1610 shadow
    # trades all drawn from a mode-bucket-picking policy, so it collapses raw
    # YES probs 0–0.78 to a flat 0.383 and destroys bucket-level ranking.
    # When ``use_raw_calibration`` is true, ``calibrate_probability`` skips
    # the isotonic lookup and instead pulls the raw KDE prob a fraction
    # ``calibration_haircut`` toward 0.5 (a linear overconfidence correction
    # that preserves bucket ordering — mode still ranks highest, tails still
    # rank lowest). Set false to fall back to the SQLite isotonic curve.
    use_raw_calibration: bool = True
    calibration_haircut: float = 0.7

    # ── Telegram ────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Trading ─────────────────────────────────────────────────────────────
    dry_run: bool = True
    min_ev_threshold: float = 0.20
    kelly_fraction: float = 0.25
    max_trade_usdc: float = 50.0
    daily_max_usdc: float = 500.0
    min_confidence: float = 0.55
    # Minimum probability for the side we bet. EV alone will buy 3¢ longshots on
    # model_prob a few % above the ask — exactly where the KDE tail is unreliable
    # and the model is anti-predictive (resolved data: every winner had side
    # prob >=0.76, every loser <=0.57). Only bet outcomes we think are likely.
    min_model_prob: float = 0.55
    # When True: every market the strategy would buy YES on is instead bought as NO
    # on the same bucket (same event, opposite outcome token). The bot's YES picks
    # have shown a stable -5.5% win-rate gap below break-even across 4 weekly
    # cohorts (n=95 post-gate); the mirror gap is +5.5% on the NO side of those
    # same markets. This flag captures that mirror. NO-side picks are unaffected
    # (they're already approximately market-fair). DRY_RUN/shadow first.
    contrarian_yes_inversion: bool = False
    # Comma-separated allowlist of cities to trade. Empty string = no filter
    # (trade all discovered cities). When set, scanner skips every market whose
    # city is not in the list — including the forecast fetch, so it's cheap.
    #
    # Use case: the bot's NO bets show a city-level pattern in resolved history.
    # Inland-continental and stable-tropical cities (Mexico City, Wuhan,
    # Guangzhou, Moscow, Jeddah, Manila, Chengdu) are +5/+8/+12% ROI cells
    # with 5/5 positive weekly cohorts on n=474. Maritime/transitional cities
    # (Tokyo, Istanbul, Panama City, Miami, Helsinki) are -8/-13% bleeders.
    # The Open-Meteo ensemble is meteorologically better at simple climates,
    # the market doesn't differentiate — that's the edge thesis.
    #
    # Match is case-insensitive and trimmed. Example .env line:
    #   CITY_ALLOWLIST=Mexico City,Wuhan,Guangzhou,Moscow,Jeddah,Manila,Chengdu
    city_allowlist: str = ""
    # Open-Meteo ensemble horizon is 16 days. Cap at 15d so every market we
    # surface has at least one valid forecast member.
    max_hours_to_resolution: float = 360.0
    # Minimum 24h volume on a bucket before we trade it (USDC). Per CLAUDE.md
    # liquidity rule — protects against thin precip/snow buckets.
    min_bucket_volume_usdc: float = 500.0
    # Tradable ask range. Below 0.03 or above 0.97 we are paying spread to
    # market makers on near-resolved or barely-active markets — the model
    # cannot generate edge there.
    min_ask_price: float = 0.03
    max_ask_price: float = 0.97
    # Hard cap on accepted EV. Anything above this is almost always a bucket
    # parsing error, near-resolution illiquidity, or stale price — never real.
    max_ev_cap: float = 1.50
    # Confidence is multiplicative; saturating at 1.0 obscures real differences.
    # We cap at this value so the time-decay multiplier still has room.
    confidence_max_cap: float = 0.85
    # Diversification: max trades per (city, date) per cycle.
    max_trades_per_city_day: int = 2

    # ── Market types to scan (comma-separated) ───────────────────────────────
    # Options: temperature, precipitation, snowfall
    enabled_market_types: str = "temperature,precipitation,snowfall,wind_speed"

    # ── Forecast ────────────────────────────────────────────────────────────
    ensemble_models: str = "icon_seamless,gfs_seamless,ecmwf_ifs025"
    bias_correction_days: int = 30

    # ── Concurrency ─────────────────────────────────────────────────────────
    # Max parallel city/forecast workers (increase for 50+ city scans)
    max_concurrency: int = 10

    # ── Scheduler ───────────────────────────────────────────────────────────
    scan_interval_minutes: int = 30

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v.upper()

    @field_validator("enabled_market_types")
    @classmethod
    def validate_market_types(cls, v: str) -> str:
        types = {t.strip().lower() for t in v.split(",") if t.strip()}
        invalid = types - VALID_MARKET_TYPES
        if invalid:
            raise ValueError(f"Invalid market types: {invalid}. Valid: {VALID_MARKET_TYPES}")
        return v

    @property
    def ensemble_model_list(self) -> List[str]:
        return [m.strip() for m in self.ensemble_models.split(",") if m.strip()]

    @property
    def enabled_market_type_set(self) -> set:
        return {t.strip().lower() for t in self.enabled_market_types.split(",") if t.strip()}

    @property
    def city_allowlist_set(self) -> set:
        """
        Lower-cased set of allowed cities; empty set means "no filter".
        Used by scanner.run_scan to drop markets early, saving Open-Meteo quota.
        """
        return {c.strip().lower() for c in self.city_allowlist.split(",") if c.strip()}

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def has_polymarket_key(self) -> bool:
        return bool(self.poly_private_key)

    @property
    def has_clob_creds(self) -> bool:
        """True when the credentials for whichever execution path is selected
        are populated. The SDK path needs the Builder API trio (+ deposit wallet
        in poly_funder_address); the legacy py-clob path needs the CLOB API key.
        Both always need the signer private key."""
        if not self.poly_private_key:
            return False
        if self.use_sdk_executor:
            return bool(
                self.poly_funder_address
                and self.poly_builder_api_key
                and self.poly_builder_secret
                and self.poly_builder_passphrase
            )
        return bool(self.poly_api_key)


# Singleton — import this everywhere
settings = Settings()
