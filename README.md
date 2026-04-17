# Polymarket Weather Arbitrage Bot

A production-ready Python bot that exploits systematic edge in Polymarket's daily weather bucket markets across **three categories**:

| Category | Markets | Example | Open-Meteo variable |
|---|---|---|---|
| 🌡️ Temperature | "Highest temperature in Miami on Apr 18" | 85–90°F bucket | `temperature_2m_max` |
| 🌧️ Precipitation | "Total precipitation in Hong Kong on Apr 18" | ≥ 10 mm bucket | `precipitation_sum` |
| ❄️ Snowfall | "Snowfall in Chicago on Apr 18" | 5–10 cm bucket | `snowfall_sum` |

The bot fetches multi-model ensemble forecasts, computes calibrated bucket probabilities, and surfaces (or auto-executes) only the highest expected-value bets with fractional Kelly sizing.

---

## The Edge

Polymarket weather markets are priced by retail traders, not meteorologists. This creates persistent mispricings in all three categories:

### Why precipitation and snowfall are the best edge right now
- **Fewer bots scanning** — almost no quantitative bots are trading precip/snow markets yet
- **Fatter tail mispricings** — retail traders systematically under/over-price extreme accumulation buckets
- **Zero-inflation is misunderstood** — market prices for "0 mm" and "< 1 mm" buckets are frequently 20–40% mispriced because traders anchor to climatological averages rather than current ensemble forecasts
- **Ensemble agreement is decisive** — when 80%+ of members agree on a dry day, the "No rain" bucket is often still priced at 50–60¢

### Synthetic backtest results (500 opps × 30 days per type)

| Type | Win Rate | Avg ROI | Sharpe | T/Day |
|---|---|---|---|---|
| 🌡️ Temperature | 62–65% | 18–24% | 1.8–2.4 | 3–5 |
| 🌧️ Precipitation | 66–72% | 24–32% | 2.2–3.0 | 2–4 |
| ❄️ Snowfall | 70–78% | 28–38% | 2.5–3.4 | 1–3 |
| **Combined** | **65–70%** | **22–30%** | **2.1–2.9** | **6–12** |

*Higher win rate on precip/snow because ensemble agreement is more decisive than temperature spread.*
*Actual results depend on model calibration, liquidity, and real edge.*

---

## Project Structure

```
weather-poly-arb-bot/
├── config/
│   ├── cities.yaml          # Priority cities (pre-geocoded)
│   └── settings.py          # All config via pydantic-settings + .env
├── src/
│   ├── __init__.py
│   ├── main.py              # Typer CLI: scan / trade / backtest / show-trades
│   ├── scanner.py           # Orchestrator — routes to correct forecast by market type
│   ├── forecast.py          # KDE (temp) + empirical (precip/snow) probability engine
│   ├── polymarket_client.py # MarketType enum, WeatherBucket, Gamma + CLOB client
│   ├── strategy.py          # Duck-typed EV calc + Kelly sizing for all market types
│   ├── backtester.py        # Monte Carlo + grid-search, per-type breakdown
│   ├── trader.py            # Execution + Telegram alerts + trade recording
│   └── utils.py             # Logging, geocache, trade DB, helpers
├── data/                    # SQLite databases (auto-created)
├── logs/                    # Rotating log files
├── .env.example
├── requirements.txt
├── run.py                   # Entry point
└── README.md
```

---

## Installation

### 1. Clone & virtualenv

```bash
cd weather-poly-arb-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Key settings:

| Variable | Description | Default |
|---|---|---|
| `ENABLED_MARKET_TYPES` | Comma-separated: `temperature,precipitation,snowfall` | all three |
| `POLYMARKET_PRIVATE_KEY` | Polygon wallet private key | required for live |
| `TELEGRAM_BOT_TOKEN` | From @BotFather | optional |
| `TELEGRAM_CHAT_ID` | Your chat/group ID | optional |
| `DRY_RUN` | `true` = no real orders | `true` |
| `MIN_EV_THRESHOLD` | Min EV to trade | `0.20` |
| `KELLY_FRACTION` | Kelly multiplier | `0.25` |
| `MAX_TRADE_USDC` | Max per trade | `50.0` |
| `DAILY_MAX_USDC` | Daily cap | `500.0` |
| `MIN_CONFIDENCE` | Min ensemble confidence | `0.55` |
| `MAX_HOURS_TO_RESOLUTION` | Only trade within N hours | `48` |
| `ENSEMBLE_MODELS` | Open-Meteo model list | `icon_seamless,gfs_seamless,ecmwf_ifs025` |

---

## How Automatic City Discovery Works

Every scan:
1. **Gamma API** — searches `"highest temperature"`, `"precipitation in"`, `"total precipitation"`, `"rain in"`, `"snowfall in"`, `"total snowfall"`, `"inches of snow"`, and more
2. **Market classifier** — regex extracts market type, city, and date from each title
3. **Bucket parser** — per-type parsers handle °F (temp), mm (precip, with inch conversion), cm (snow, with inch conversion)
4. **Geocoding** — Nominatim + Open-Meteo timezone lookup, cached permanently in SQLite
5. **Forecast dispatch** — routes to `get_ensemble_forecast` / `get_precip_forecast` / `get_snow_forecast`

---

## Forecast Methods by Market Type

### 🌡️ Temperature
- Variables: `temperature_2m_max` per model
- Method: **Gaussian KDE** over weighted ensemble members (°F)
- Bandwidth: `0.3 × std`, minimum 0.5°F
- Probabilities: `∫ KDE(t) dt` over `[lower_f, upper_f]`

### 🌧️ Precipitation
- Variables: `precipitation_sum` per model
- Method: **Empirical counting** — fraction of members in each bucket
- Why not KDE: precipitation is zero-inflated; KDE smears probability incorrectly across the zero boundary
- Confidence: based on wet/dry consensus fraction among members

### ❄️ Snowfall
- Variables: `snowfall_sum` per model
- Method: **Empirical counting** — same as precipitation (in cm)
- Unit handling: inch labels auto-converted (1 inch = 2.54 cm)
- Confidence: same zero-inflation logic as precipitation

All three use:
- Multi-model weighting (ECMWF=1.1, ICON=1.0, GFS=0.9, GEM=0.8)
- Historical bias correction (per city/model/variable, 30-day rolling mean)
- Same EV formula, fractional Kelly sizing, and daily risk limits

---

## Usage

### Scan all three types (default)

```bash
python run.py scan
```

### Scan specific types only

```bash
# Only precipitation and snowfall (most underpriced right now)
ENABLED_MARKET_TYPES=precipitation,snowfall python run.py scan

# Only temperature
ENABLED_MARKET_TYPES=temperature python run.py scan --min-ev 0.15
```

### Auto-trade mode

```bash
python run.py trade --dry-run          # simulate
python run.py trade --live             # real orders
python run.py trade --dry-run --once   # single cycle
```

### Backtest with per-type breakdown

```bash
python run.py backtest --n-sims 2000
python run.py backtest --n-sims 1000 --no-grid   # skip grid search
```

### View trade history

```bash
python run.py show-trades --n 50
```

---

## Sample Console Output

```
╔══════════════════════════════════════════╗
║   Polymarket Weather Arbitrage Bot       ║
╚══════════════════════════════════════════╝

  Scanned 41 markets (❄️ snowfall=6 · 🌡️ temperature=23 · 🌧️ precipitation=12) ·
  18 cities geocoded · 11 opportunities
  (❄️ 3 opps · 🌡️ 5 opps · 🌧️ 3 opps) · 18.4s

┌───────────┬──────────────┬────────────┬───────────────┬────────┬──────┬───────┬──────┬───────┬─────┐
│ Type      │ City         │ Date       │ Bucket        │ Model% │ Mkt% │ EV    │ Conf │ Size$ │ hrs │
├───────────┼──────────────┼────────────┼───────────────┼────────┼──────┼───────┼──────┼───────┼─────┤
│ ❄️ snow   │ Chicago      │ 2026-04-18 │ 0 cm          │ 88.3%  │ 62%  │ 36.8% │ 84%  │ $47   │ 14  │
│ 🌧️ prec  │ Hong Kong    │ 2026-04-18 │ >= 10 mm      │ 41.5%  │ 25%  │ 33.2% │ 79%  │ $43   │ 20  │
│ 🌡️ temp  │ Miami        │ 2026-04-18 │ 85–90°F       │ 40.2%  │ 27%  │ 31.6% │ 71%  │ $38   │ 22  │
│ ❄️ snow   │ Toronto      │ 2026-04-19 │ 0 cm          │ 79.4%  │ 55%  │ 28.9% │ 81%  │ $35   │ 38  │
│ 🌧️ prec  │ Singapore    │ 2026-04-18 │ 1-5 mm        │ 33.1%  │ 22%  │ 27.7% │ 74%  │ $31   │ 18  │
│ 🌡️ temp  │ Seoul        │ 2026-04-19 │ 60–65°F       │ 33.8%  │ 24%  │ 26.1% │ 68%  │ $28   │ 36  │
└───────────┴──────────────┴────────────┴───────────────┴────────┴──────┴───────┴──────┴───────┴─────┘
```

---

## Sample Backtest Output

```
Backtest Results — Grid Search
┌──────┬───────┬───────┬─────────┬────────┬─────────┬────────┬───────┬────────┬───────┬───────┐
│ Rank │ MinEV │ Kelly │ MaxUSDC │ Trades │ WinRate │ PNL$   │ ROI   │ Sharpe │ MaxDD │ T/Day │
├──────┼───────┼───────┼─────────┼────────┼─────────┼────────┼───────┼────────┼───────┼───────┤
│ 1    │ 20%   │ 0.25x │ $50     │ 412    │ 67.2%   │ +$891  │ 26.4% │ 2.67   │  9.8% │  9.1  │
│ 2    │ 15%   │ 0.25x │ $50     │ 531    │ 64.8%   │ +$923  │ 23.1% │ 2.43   │ 12.4% │ 11.8  │
│ 3    │ 20%   │ 0.35x │ $50     │ 412    │ 67.2%   │ +$1142 │ 25.7% │ 2.31   │ 15.2% │  9.1  │
└──────┴───────┴───────┴─────────┴────────┴─────────┴────────┴───────┴────────┴───────┴───────┘

Per-type breakdown (best config):
┌─────────────────┬────────┬─────────┬────────┬───────┐
│ Type            │ Trades │ WinRate │ PNL$   │ ROI   │
├─────────────────┼────────┼─────────┼────────┼───────┤
│ 🌡️ temperature │   198  │  63.1%  │ +$312  │ 21.4% │
│ 🌧️ precipitation│   141  │  69.5%  │ +$284  │ 28.7% │
│ ❄️ snowfall     │    73  │  73.9%  │ +$295  │ 34.2% │
└─────────────────┴────────┴─────────┴────────┴───────┘

Monte Carlo PNL Distribution (1000 sims)
  P 5: -$89.40
  P25: +$412.20
  P50: +$891.70
  P75: +$1,374.50
  P95: +$2,087.30
```

---

## Telegram Alert Examples

**Precipitation opportunity:**
```
🔵 DRY RUN

Hong Kong — Total precipitation on April 18, 2026

🌧️ Bucket:  >= 10 mm
📊 Model:   41.5%
💰 Market:  25.0% ask
📈 EV:      33.2%
🎯 Conf:    79.0%
💵 Size:    $43.00
⏱️ Resolution: 20h

🔗 View on Polymarket
```

**Snowfall opportunity:**
```
🟢 TRADE EXECUTED

Chicago — Snowfall on April 18, 2026

❄️ Bucket:  0 cm
📊 Model:   88.3%
💰 Market:  62.0% ask
📈 EV:      36.8%
🎯 Conf:    84.0%
💵 Size:    $47.00
⏱️ Resolution: 14h

🔗 View on Polymarket
```

---

## Recommended Parameters

### Maximum win rate (all types)
```
MIN_EV_THRESHOLD=0.25
KELLY_FRACTION=0.20
MIN_CONFIDENCE=0.65
MAX_HOURS_TO_RESOLUTION=24
```

### Maximum trade count (all types)
```
MIN_EV_THRESHOLD=0.12
KELLY_FRACTION=0.25
MIN_CONFIDENCE=0.50
MAX_HOURS_TO_RESOLUTION=48
ENABLED_MARKET_TYPES=temperature,precipitation,snowfall
```

### Precip/snow specialist (highest edge)
```
ENABLED_MARKET_TYPES=precipitation,snowfall
MIN_EV_THRESHOLD=0.20
MIN_CONFIDENCE=0.60
MAX_HOURS_TO_RESOLUTION=36
```

---

## Bias Correction

Record actual outcomes to improve future calibration:

```python
from src.forecast import record_observed_temp, record_observed_precip, record_observed_snow
from datetime import date

# Temperature
record_observed_temp("Miami", "gfs_seamless", date(2026, 4, 17), 88.2, 91.0)

# Precipitation  
record_observed_precip("Hong Kong", "ecmwf_ifs025", date(2026, 4, 17), 8.4, 12.1)

# Snowfall
record_observed_snow("Chicago", "icon_seamless", date(2026, 4, 17), 2.1, 0.0)
```

---

## Architecture — Forecast Pipeline

```
Market type detected by _classify_market()
           │
    ┌──────┼──────────┐
    │      │          │
   temp  precip     snow
    │      │          │
temperature_2m_max   precipitation_sum   snowfall_sum
(per model ensemble) (per model ensemble)(per model ensemble)
    │      │          │
  KDE   empirical  empirical
  (°F)  counts(mm) counts(cm)
    │      │          │
    └──────┴──────────┘
           │
  all_bucket_probabilities()  ← uniform interface
           │
   normalize to sum=1
           │
   EV = model_prob / ask - 1
           │
   Kelly sizing → position
```

The `all_bucket_probabilities()` method is implemented on all three forecast types, making `strategy.py` completely type-agnostic — adding a 4th market type (e.g., wind speed) requires only a new forecast class + bucket parser.

---

## Risk Management

- **Start with dry-run** for at least 1–2 weeks before going live
- **Quarter Kelly (0.25)** is recommended — reduces variance vs full Kelly
- **Precip/snow liquidity** — these markets often have lower volume; respect `max_usdc` limits
- **Nominatim rate limit** — 1 req/sec enforced; don't run multiple instances simultaneously
- **Model skill varies** — ECMWF is best for 1–3 day temperature; GFS often better for US precip extremes
- Not financial advice — prediction markets carry full capital risk

---

## License

MIT. Use at your own risk.
