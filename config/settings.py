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

VALID_MARKET_TYPES = {"temperature", "precipitation", "snowfall"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Polymarket ──────────────────────────────────────────────────────────
    polymarket_private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    clob_host: str = "https://clob.polymarket.com"
    gamma_api_host: str = "https://gamma-api.polymarket.com"

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
    max_hours_to_resolution: float = 48.0

    # ── Market types to scan (comma-separated) ───────────────────────────────
    # Options: temperature, precipitation, snowfall
    enabled_market_types: str = "temperature,precipitation,snowfall"

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
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def has_polymarket_key(self) -> bool:
        return bool(self.polymarket_private_key)


# Singleton — import this everywhere
settings = Settings()
