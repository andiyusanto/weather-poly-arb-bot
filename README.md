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

## GCP Infrastructure

Polymarket applies IP-based geo-blocking beyond just the US. **Use Asia-Pacific zones.**

### Blocked countries (official Polymarket policy)

Source: [docs.polymarket.com/api-reference/geoblock.md](https://docs.polymarket.com/api-reference/geoblock.md)

**Fully blocked** — orders rejected outright:
Australia, Belarus, Belgium, Burundi, Central African Republic, Congo, Cuba, Ethiopia, France, Germany, Iran, Iraq, Italy, Lebanon, Libya, Myanmar, Netherlands, Nicaragua, North Korea, Russia, Somalia, South Sudan, Sudan, Syria, **United States**, US Minor Outlying Islands, UK, Venezuela, Yemen, Zimbabwe

**Close-only** — can close existing positions but cannot open new ones:
Poland, Singapore, Taiwan, Thailand

**Blocked regions within countries:**
Ontario (Canada), Crimea / Donetsk / Luhansk (Ukraine)

### GCP zone impact

| GCP Zone | Country | Status |
|---|---|---|
| `us-*` | United States | ❌ Fully blocked |
| `europe-west1` | Belgium | ❌ Fully blocked |
| `europe-west4` | Netherlands | ❌ Fully blocked |
| `europe-west2/3/6` | UK / Germany / Finland | ❌ UK/Germany fully blocked |
| `europe-west9` | France | ❌ Fully blocked |
| `europe-west8` | Italy | ❌ Fully blocked |
| `europe-southwest1` | Spain | ✅ Not on blocked list |
| `europe-north1` | Finland | ✅ Not on blocked list |
| `asia-southeast1` | Singapore | ⚠️ Close-only |
| `asia-northeast1` | Japan | ✅ Not on blocked list |
| `asia-east1` | Taiwan | ⚠️ Close-only |
| `australia-southeast1` | Australia | ❌ Fully blocked |

> **Note:** "Close-only" zones (Singapore, Taiwan) mean the bot can **scan and read markets** but any live orders will be rejected. Use Japan (`asia-northeast1`) for full trading access.

> Use the [zone verification steps](#verifying-a-zone-is-not-blocked) below to confirm with the geoblock API directly.

### Recommended zones

| Zone | Location | Status | Notes |
|---|---|---|---|
| `asia-northeast1-b` | Tokyo, Japan | ✅ Full access | **Best choice** — unrestricted, well-connected |
| `asia-northeast3-b` | Seoul, South Korea | ✅ Full access | Good alternative |
| `europe-southwest1-a` | Madrid, Spain | ✅ Full access | EU option — Spain not on blocked list |
| `europe-north1-b` | Finland | ✅ Full access | EU option |
| `asia-southeast1-b` | Singapore | ⚠️ Close-only | Scan/read works; live orders rejected |

### Provisioning command (Tokyo, recommended)

```bash
gcloud compute instances create polymarket-bot \
  --zone=asia-northeast1-b \
  --machine-type=e2-small \
  --network-tier=STANDARD \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=10GB \
  --boot-disk-type=pd-standard
```

**Why e2-small?** scipy KDE scans peak at ~400MB RAM. e2-micro (1GB) risks OOM if scan + backtest run concurrently. e2-small (~$11/mo in asia-southeast1) is the safe minimum.

**Why STANDARD network tier?** At 30-minute scan intervals latency is not critical — STANDARD saves ~15% vs PREMIUM with no practical difference.

### Systemd service (auto-restart)

```ini
# /etc/systemd/system/polymarket-bot.service
[Unit]
Description=Polymarket Weather Arb Bot
After=network.target

[Service]
WorkingDirectory=/opt/weather-poly-arb-bot
EnvironmentFile=/opt/weather-poly-arb-bot/.env
ExecStart=/opt/weather-poly-arb-bot/.venv/bin/python run.py trade --dry-run
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now polymarket-bot
```

### Optional: spot instance (~70% cheaper)

Add `--provisioning-model=SPOT` to the provisioning command. With `Restart=always` in systemd, preemptions (typically < 1/week) are handled automatically. Brings cost to ~$3–4/mo.

### Verifying a zone is not blocked

Before provisioning a permanent instance, test the zone with a throwaway spot VM (~$0.002/hr):

**1. Create test VM**
```bash
gcloud compute instances create poly-test \
  --zone=asia-southeast1-b \
  --machine-type=e2-micro \
  --provisioning-model=SPOT \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=10GB
```

**2. SSH in and probe both APIs**
```bash
gcloud compute ssh poly-test --zone=asia-southeast1-b

curl -s -o /dev/null -w "Gamma API: %{http_code}\n" \
  "https://gamma-api.polymarket.com/markets?limit=1"

curl -s -o /dev/null -w "CLOB API:  %{http_code}\n" \
  "https://clob.polymarket.com/"
```

| Response | Meaning |
|---|---|
| `200` | Zone is accessible — safe to use |
| `403` | Geo-blocked — try a different zone |
| `000` / timeout | Full block or network issue |

**3. Delete the test VM**
```bash
gcloud compute instances delete poly-test --zone=asia-southeast1-b --quiet
```

Run steps 1–3 for each candidate zone before committing to a permanent instance.

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

## Concurrency & Performance

The bot uses `ThreadPoolExecutor` (Python stdlib) rather than asyncio because key dependencies — `geopy`, `py-clob-client`, and `sqlite3` — are synchronous-only. Wrapping them in `asyncio.run_in_executor()` would just be ThreadPoolExecutor with extra steps.

### What runs in parallel

| Layer | Mechanism | Default workers |
|---|---|---|
| Forecast dispatch (cities × types) | `ThreadPoolExecutor(max_workers=MAX_CONCURRENCY)` | 10 |
| Model fetching per forecast | `ThreadPoolExecutor` + `Semaphore(4)` | up to 4 |
| Token price enrichment (CLOB/Gamma) | `ThreadPoolExecutor` | min(MAX_CONCURRENCY, 10) |
| Geocoding (Nominatim) | Sequential — Nominatim TOS requires 1 req/s | — |

### Expected speedup

| Cities | Old (sequential) | New (10 workers) |
|---|---|---|
| 10 cities × 3 types | ~90s | ~12s |
| 30 cities × 3 types | ~270s | ~35s |
| 50 cities × 3 types | ~450s | ~55s |

### Tuning `MAX_CONCURRENCY`

```env
# .env
MAX_CONCURRENCY=10   # default — safe for any VPS
MAX_CONCURRENCY=20   # e2-small (2 vCPU) with fast network
MAX_CONCURRENCY=25   # recommended max — stays within Open-Meteo free tier
```

> **Do not exceed 25.** Open-Meteo's ensemble API will start returning 429 errors above ~30 concurrent requests, and the Nominatim geocoder must remain at 1 req/s regardless of this setting.

### VPS sizing guide

| Capital | Recommended setting | GCP machine |
|---|---|---|
| < $1k (testing) | `MAX_CONCURRENCY=10` | e2-small |
| $1k–$10k | `MAX_CONCURRENCY=20` | e2-medium |
| $10k+ | `MAX_CONCURRENCY=25` | e2-standard-2 |

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
