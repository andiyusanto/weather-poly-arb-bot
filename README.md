# Polymarket Weather Arbitrage Bot

A production-ready Python bot that exploits systematic edge in Polymarket's daily weather bucket markets across **four categories**:

| Category | Markets | Example | Open-Meteo variable |
|---|---|---|---|
| 🌡️ Temperature | "Highest temperature in Miami on Apr 18" | 85–90°F bucket | `temperature_2m_max` |
| 🌧️ Precipitation | "Total precipitation in Hong Kong on Apr 18" | ≥ 10 mm bucket | `precipitation_sum` |
| ❄️ Snowfall | "Snowfall in Chicago on Apr 18" | 5–10 cm bucket | `snowfall_sum` |
| 💨 Wind Speed | "Max wind speed in Tokyo on Apr 18" | 15–20 mph bucket | `wind_speed_10m_max` |

The bot fetches multi-model ensemble forecasts, computes calibrated bucket probabilities, and surfaces (or auto-executes) only the highest expected-value bets with fractional Kelly sizing.

---

## The Edge

Polymarket weather markets are priced by retail traders, not meteorologists. This creates persistent mispricings in all four categories:

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
| 💨 Wind Speed | 63–68% | 20–28% | 2.0–2.6 | 2–4 |
| **Combined** | **65–70%** | **22–30%** | **2.1–2.9** | **8–16** |

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
│   ├── forecast.py          # KDE (temp/wind) + empirical (precip/snow) probability engine
│   ├── polymarket_client.py # MarketType enum, WeatherBucket, Gamma + CLOB client
│   ├── strategy.py          # Duck-typed EV calc + Kelly sizing for all market types
│   ├── backtester.py        # Monte Carlo + grid-search, per-type breakdown
│   ├── trader.py            # Execution + Telegram alerts + trade recording
│   └── utils.py             # Logging, geocache, trade DB, helpers
├── tests/
│   └── test_wind_forecast.py  # Unit tests for wind speed forecast logic
├── data/                    # SQLite databases (auto-created)
├── logs/                    # Rotating log files
├── setup.py                 # One-time credential generator (pre_setup.env → .env)
├── approve_usdc.py          # On-chain USDC approval for Polymarket spenders
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

### tmux (quick start, no root required)

tmux is the fastest way to get the bot running persistently on a fresh VPS — no systemd config, no root, survives SSH disconnects.

**Install tmux**

```bash
sudo apt install -y tmux
```

**First-time session setup**

```bash
# Create a named session for the bot
tmux new -s polybot

# Inside the session: activate venv and start in shadow mode
cd /opt/weather-poly-arb-bot
source .venv/bin/activate
python run.py trade --shadow    # records decisions, no real orders
```

Detach with `Ctrl+B D`. The bot keeps running after you close SSH.

**Reconnect later**

```bash
tmux attach -t polybot
```

**Useful tmux commands**

| Command | What it does |
|---|---|
| `Ctrl+B D` | Detach (leave bot running) |
| `Ctrl+B [` | Scroll mode — read logs with arrow keys; `Q` to exit |
| `Ctrl+B C` | New window (e.g. for `python run.py scan`) |
| `Ctrl+B 0` / `1` | Switch between windows |
| `tmux ls` | List all sessions |
| `tmux kill-session -t polybot` | Stop the bot and close session |

**Split pane: bot + live logs side by side**

```bash
# Start the main bot pane
tmux new -s polybot

# Split vertically: Ctrl+B %
# In the new pane, tail the log
tail -f logs/bot_$(date +%Y%m%d).log
```

**Persistent tmux config (optional)**

```bash
cat >> ~/.tmux.conf << 'EOF'
set -g mouse on              # scroll with mouse wheel
set -g history-limit 50000   # keep more scrollback
set -g status-right "%H:%M"
EOF
tmux source ~/.tmux.conf
```

> Use tmux for manual/development runs. For production (unattended, auto-restart on crash), prefer the systemd service below.

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

### 2. Generate credentials (one-time)

Create `pre_setup.env` with your wallet details:

```env
POLY_PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...
```

Then run the setup script to derive Level 2 API credentials and write them to `.env`:

```bash
python setup.py
```

This calls `ClobClient.create_or_derive_api_creds()` and writes `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, and `POLY_SIG_TYPE` to `.env`.

### 3. Approve USDC on-chain (one-time)

`setup.py` calls `update_balance_allowance()` which is informational only — it does not submit an on-chain transaction. Run this to do the real ERC-20 approval for both Polymarket spender contracts:

```bash
python approve_usdc.py
```

This submits `approve(MAX_UINT256)` to both CTF Exchange (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`) and NegRisk CTF Exchange (`0xC5d563A36AE78145C45a50134d48A1215220f80a`) on Polygon mainnet. Falls back across five public RPCs automatically.

### 4. Configure `.env`

Key settings written by `setup.py` + manual options:

| Variable | Description | Default |
|---|---|---|
| `POLY_PRIVATE_KEY` | Polygon wallet private key (written by `setup.py`) | required for live |
| `POLY_FUNDER_ADDRESS` | Funder wallet address (written by `setup.py`) | optional |
| `POLY_API_KEY` | CLOB Level 2 API key (written by `setup.py`) | required for live |
| `POLY_API_SECRET` | CLOB API secret (written by `setup.py`) | required for live |
| `POLY_API_PASSPHRASE` | CLOB API passphrase (written by `setup.py`) | required for live |
| `POLY_SIG_TYPE` | Signature type: `0` = EOA, `2` = Gnosis Safe (written by `setup.py`) | `0` |
| `ENABLED_MARKET_TYPES` | Comma-separated: `temperature,precipitation,snowfall,wind_speed` | first three |
| `TELEGRAM_BOT_TOKEN` | From @BotFather | optional |
| `TELEGRAM_CHAT_ID` | Your chat/group ID | optional |
| `DRY_RUN` | `true` = no real orders | `true` |
| `MIN_EV_THRESHOLD` | Min EV to trade | `0.20` |
| `KELLY_FRACTION` | Kelly multiplier | `0.25` |
| `MAX_TRADE_USDC` | Max per trade | `50.0` |
| `DAILY_MAX_USDC` | Daily cap | `500.0` |
| `MIN_CONFIDENCE` | Min ensemble confidence | `0.55` |
| `MAX_HOURS_TO_RESOLUTION` | Only trade within N hours | `720` |
| `ENSEMBLE_MODELS` | Open-Meteo model list | `icon_seamless,gfs_seamless,ecmwf_ifs025` |
| `MIN_MODEL_PROB` | Side-prob gate: minimum probability for the bet side | `0.55` |
| `CONTRARIAN_YES_INVERSION` | If `true`, every YES pick is bought as NO on the same bucket at the real NO ask. See **Contrarian YES Inversion (Option F)** under Analytics & Edge Validation. | `false` |

---

## How Automatic City Discovery Works

Every scan:
1. **Gamma API** — searches `"highest temperature"`, `"precipitation in"`, `"total precipitation"`, `"rain in"`, `"snowfall in"`, `"total snowfall"`, `"inches of snow"`, `"wind speed"`, and more
2. **Market classifier** — regex extracts market type, city, and date from each title
3. **Bucket parser** — per-type parsers handle °F (temp), mm (precip, with inch conversion), cm (snow, with inch conversion), mph/km/h (wind)
4. **Geocoding** — Nominatim + Open-Meteo timezone lookup, cached permanently in SQLite
5. **Forecast dispatch** — routes to `get_ensemble_forecast` / `get_precip_forecast` / `get_snow_forecast` / `get_wind_forecast`

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

### 💨 Wind Speed
- Variables: `wind_speed_10m_max` per model (daily max at 10 m height)
- Method: **Gaussian KDE** — wind is continuous and not zero-inflated, same approach as temperature
- Unit conversion: Open-Meteo returns km/h; all probabilities computed in mph (`KPH_TO_MPH = 0.621371`)
- Bucket parsing: handles `"10–15 mph"`, `"less than 10 mph"`, `"25 mph or higher"`, `"30–50 km/h"`, etc.
- Confidence: `_wind_confidence(std_mph, n_models)` — maps spread [5, 25 mph] → confidence [0.9, 0.1] with a small model-count bonus
- KDE integration: uses `scipy gaussian_kde.integrate_box_1d(lo, hi)` (avoids `np.trapz` which was removed in NumPy 2.0); falls back to empirical counting when ensemble spread is near zero

All four use:
- Multi-model weighting (ECMWF=1.1, ICON=1.0, GFS=0.9, GEM=0.8)
- Historical bias correction (per city/model/variable, 30-day rolling mean)
- Same EV formula, fractional Kelly sizing, and daily risk limits

---

## Usage

### Scan all four types (default)

```bash
python run.py scan
```

### Scan specific types only

```bash
# Only precipitation and snowfall (most underpriced right now)
ENABLED_MARKET_TYPES=precipitation,snowfall python run.py scan

# Only temperature
ENABLED_MARKET_TYPES=temperature python run.py scan --min-ev 0.15

# Include wind speed
ENABLED_MARKET_TYPES=temperature,precipitation,snowfall,wind_speed python run.py scan
```

### Auto-trade mode

| Command | Orders placed? | Tracked in DB? | Outcome resolved? |
|---|---|---|---|
| `--dry-run` | No | No | No |
| `--shadow` | No | **Yes** | **Yes** |
| `--live` | **Yes** | Yes | Manual |

```bash
python run.py trade --dry-run          # log only, nothing recorded
python run.py trade --shadow           # record to DB, no real orders (recommended first step)
python run.py trade --live             # real orders via CLOB

python run.py trade --shadow --once    # single shadow cycle and exit
python run.py trade --dry-run --once   # single dry-run cycle and exit
```

### Shadow mode — edge validation workflow

Shadow mode sits between dry-run and live. It submits no orders but records every decision to `trades.db`, then resolves each position once the market closes. Use it to confirm your model has real edge on live Polymarket prices before risking capital.

```bash
# Step 1 — run shadow continuously (records decisions, no orders)
python run.py trade --shadow

# Step 2 — close out resolved markets, compute P&L, feed the bias/calibration tables (run daily)
python run.py resolve-shadow

# Step 3 — view win rate, total P&L, per-city breakdown
python run.py shadow-pnl
```

#### `python run.py trade --shadow` — paper-trading loop

Runs the full scan→evaluate→size→record pipeline, but never submits an order to the CLOB.

Each cycle:
1. **Scan** — fetch active weather markets from Gamma; filter by `MAX_HOURS_TO_RESOLUTION` and `MIN_BUCKET_VOLUME_USDC`.
2. **Forecast** — pull ensemble forecasts (ECMWF / ICON / GFS) for each (city, target_date) and apply rolling bias correction.
3. **Evaluate** — compute EV, confidence, and Kelly-sized stake per bucket; keep those clearing `MIN_EV_THRESHOLD` and `MIN_CONFIDENCE`.
4. **Apply daily limit** — cap by `DAILY_MAX_USDC`. Shadow trades are **not** counted toward this cap.
5. **"Execute"** — `execute_opportunity()` short-circuits: it returns `{"status": "shadow", "order_id": "SHADOW"}` instead of calling `place_market_order`.
6. **Record** — inserts a row into `trades.db` with `shadow=1`, `outcome=NULL`, `pnl=NULL`, plus `condition_id`, `forecast_mean`, and the market price at entry (needed for later resolution and P&L).
7. **Telegram alert** tagged 🟡 SHADOW.
8. Sleeps `--interval` minutes and repeats (use `--once` to run a single cycle).

Difference vs `--dry-run`: dry-run logs and forgets; shadow **persists** the decision so the eventual outcome can be matched against the model's prediction.

#### `python run.py resolve-shadow` — close out paper trades

Looks at every `shadow=1, outcome IS NULL` row and asks Gamma whether the underlying market has resolved.

For each open shadow trade:
1. `fetch_market_resolution(condition_id)` → `"yes"`, `"no"`, or `None` (still open).
2. If resolved, P&L is computed from the entry ask price:
   - Win: `size_usdc * (1/market_price - 1)` (e.g., bought YES at 20¢, won → +4× stake)
   - Loss: `-size_usdc`
3. Writes `outcome`, `pnl`, and `resolved_at` back to the row.
4. **Bias recorder** (`bias_recorder.py`) fetches the actual observed weather from Open-Meteo's archive endpoint and stores `(observed − forecast)` into `bias_corrections.db`. Without this, the rolling correction in `forecast.py` has no data and every forecast keeps using `bias=+0.0`.
5. **Calibration rebuild** refreshes the empirical curve in `calibration.db`.

> ⚠️ **Run this on a cron.** If you skip it, `bias_corrections.db` and `calibration.db` stay empty, forecasts run uncorrected, and shadow EVs become meaningless. A daily run after the prior day's markets settle (e.g. 06:00 UTC) is the minimum.

#### `python run.py shadow-pnl` — edge-validation dashboard

Read-only report against `trades.db`. Prints:
- **Header stats** — total trades, resolved vs open, win rate, total P&L, avg EV, avg confidence.
- **By-city table** — per-city trade count, resolved count, win rate, P&L (sorted by P&L so the worst cities surface first).
- **Recent shadow trades** — last 20 rows with model%, ask price, EV, size, outcome, and color-coded P&L.

If avg realised P&L per trade is far below what avg EV predicted, the model is overconfident — fix calibration before going live.

#### Typical operating rhythm

```bash
# Continuous (systemd / tmux):
python run.py trade --shadow --interval 60

# Daily cron at e.g. 06:00 UTC:
python run.py resolve-shadow && python run.py shadow-pnl
```

#### Setting up the daily cron

Pick **one** of the options below.

**Option A — user crontab (simplest, works on any VPS)**

```bash
# 1. Create a wrapper that activates the venv and runs both commands.
cat > ~/resolve-shadow.sh << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /home/yusantoandi/weather-poly-arb-bot                # adjust to your install path
source .venv/bin/activate
python run.py resolve-shadow >> logs/cron_resolve.log 2>&1
python run.py shadow-pnl     >> logs/cron_resolve.log 2>&1
EOF
chmod +x ~/resolve-shadow.sh

# 2. Install the cron entry (06:00 UTC daily).
( crontab -l 2>/dev/null; echo "0 6 * * * /home/yusantoandi/resolve-shadow.sh" ) | crontab -

# 3. Verify it's registered.
crontab -l
```

> Cron runs with a minimal environment. Always `cd` into the project and `source .venv/bin/activate` inside the wrapper — never rely on the cron daemon to pick up your shell's `$PATH` or virtualenv.

**Option B — systemd timer (preferred on the production VM alongside the `polymarket-bot.service`)**

```ini
# /etc/systemd/system/polymarket-resolve.service
[Unit]
Description=Polymarket weather bot — resolve shadow trades & print P&L
After=network-online.target

[Service]
Type=oneshot
User=polybot
WorkingDirectory=/opt/weather-poly-arb-bot
EnvironmentFile=/opt/weather-poly-arb-bot/.env
ExecStart=/opt/weather-poly-arb-bot/.venv/bin/python run.py resolve-shadow
ExecStart=/opt/weather-poly-arb-bot/.venv/bin/python run.py shadow-pnl
StandardOutput=append:/opt/weather-poly-arb-bot/logs/cron_resolve.log
StandardError=append:/opt/weather-poly-arb-bot/logs/cron_resolve.log
```

```ini
# /etc/systemd/system/polymarket-resolve.timer
[Unit]
Description=Run resolve-shadow daily at 06:00 UTC

[Timer]
OnCalendar=*-*-* 06:00:00 UTC
Persistent=true                # catches up if the VM was off at trigger time
Unit=polymarket-resolve.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-resolve.timer
systemctl list-timers polymarket-resolve.timer   # confirm next run
journalctl -u polymarket-resolve.service -n 50   # last run output
```

**Why 06:00 UTC?** Polymarket weather markets settle shortly after midnight local time. By 06:00 UTC the prior-day's APAC and European markets are resolved; the US-evening markets will be picked up by the following day's run. Adjust if your `ENABLED_CITIES` skew elsewhere.

**Go live only when** `shadow-pnl` shows:
- Win rate ≥ 55% on ≥ 30 resolved trades
- Total P&L positive
- No single city driving all the edge (diversification check)

#### `python run.py side-pnl` — per-side performance & CI verdict

`shadow-pnl` aggregates everything. `side-pnl` splits **YES vs NO**, computes the 95% CI on each side's win rate, and tells you at a glance whether the CI lower bound clears the avg ask (i.e. break-even). This is the decisive metric — most casual eyeballing of "win rate vs break-even" is wrong because it ignores sample-size uncertainty.

```bash
python run.py side-pnl                  # both sides, post-gate (id > 130) by default
python run.py side-pnl --side yes       # YES only
python run.py side-pnl --side no        # NO only
python run.py side-pnl --all            # include pre-gate history
python run.py side-pnl --since 200      # custom cutoff
```

The verdict line per side reads:
- 🟢 **edge confirmed at 95%** — CI lower bound clears avg ask. Real, measured edge.
- 🔴 **no edge** — CI upper bound below avg ask. Confidently losing.
- 🟡 **inconclusive** — break-even sits inside the CI. Keep collecting.

Use this instead of staring at the raw `shadow-pnl` win-rate number — it tells you *how confident* the win-rate estimate is, which matters more than the number itself when n is small.

#### `python run.py slice-dash` — find where edge actually lives

Aggregate numbers hide where the edge is. `slice-dash` cuts your resolved trades by **ask range, bucket type, model-prob band, lead time, and city**, plus a 2-D `ask × bucket-type` view. Read-only — does not affect trading.

```bash
python run.py slice-dash                # YES side, post-gate (default)
python run.py slice-dash --side no      # NO side
python run.py slice-dash --all          # full history
python run.py slice-dash --since 250    # custom cutoff
```

Each row shows `n`, win rate, **gap vs break-even** (green=+, red=−), P&L, ROI. Reading guide:
- **n < 5** → anecdote, ignore
- **n ≥ 10 with green gap** → candidate edge pattern, worth deeper look
- **Green gap + n ≥ 30** → strong signal in this slice

Useful for spotting *behavioural mispricing* — e.g. whether edge concentrates in cheap-longshot YES bets ("the market under-prices outcomes nobody bets *for*") or in a specific city or bucket shape.

#### `python run.py yes-score` — quality-score prototype (analysis only)

Trains a transparent additive log-odds score on existing YES trades — per-feature lift table, in-sample top/bottom split, and **leave-one-out cross-validated accuracy** (honest, not in-sample). **Does NOT deploy** — pure analysis tool so you can see whether the score has predictive power *before* wiring it into the strategy.

```bash
python run.py yes-score                 # train on post-gate YES history
python run.py yes-score --all           # train on full YES history
```

Reading the LOO accuracy row:
- **At base rate (~50%)** → features have no predictive power, score is noise
- **5–10pp above base** → suggestive, keep collecting data
- **15pp+ above base, stable across 2–3 reruns** → real signal, candidate for deployment

Re-run every ~20 new YES resolves. When LOO accuracy stabilises meaningfully above base rate, the score is ready to wire into the strategy as a YES-side filter — a separate, post-validation decision.

#### `python run.py contrarian-pnl` — validate the contrarian-inversion strategy

Three-way comparison of resolved shadow trades — **contrarian (YES→NO flipped)** vs **natural NO** vs **natural YES baseline** — plus a weekly cohort split so you can tell a real edge from a single-cohort lucky streak. See the **Contrarian YES Inversion (Option F)** section below for the strategy rationale.

```bash
python run.py contrarian-pnl                  # all resolved rows
python run.py contrarian-pnl --since 1500     # only id > 1500 (e.g. since the flag went live)
```

Reading the output:

- **🟢 EDGE CONFIRMED** — contrarian CI lower bound clears its avg ask (= break-even). Deploy with small live size.
- **🟡 inconclusive** — break-even sits inside CI. Keep collecting; check again every 20–30 new resolves.
- **🔴 EDGE REJECTED** — contrarian CI upper bound is below break-even. The in-sample edge didn't survive forward. Flip the flag off, stop YES bets via min_model_prob = 1.0, fall back to NO-only.
- **All weeks positive** in the cohort table is the robust-edge signal — the same pattern that distinguished Option F from earlier false positives (which all looked great in aggregate but had at least one losing cohort).

### Backtest with per-type breakdown

```bash
python run.py backtest --n-sims 2000
python run.py backtest --n-sims 1000 --no-grid   # skip grid search
```

### View trade history

```bash
python run.py show-trades --n 50   # shows mode: SHADOW / DRY / LIVE with colored P&L
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

### Wind speed (new market type)
```
ENABLED_MARKET_TYPES=wind_speed
MIN_EV_THRESHOLD=0.20
MIN_CONFIDENCE=0.55
MAX_HOURS_TO_RESOLUTION=48
```

---

## Bias Correction

Record actual outcomes to improve future calibration:

```python
from src.forecast import (
    record_observed_temp, record_observed_precip,
    record_observed_snow, record_observed_wind,
)
from datetime import date

# Temperature
record_observed_temp("Miami", "gfs_seamless", date(2026, 4, 17), 88.2, 91.0)

# Precipitation
record_observed_precip("Hong Kong", "ecmwf_ifs025", date(2026, 4, 17), 8.4, 12.1)

# Snowfall
record_observed_snow("Chicago", "icon_seamless", date(2026, 4, 17), 2.1, 0.0)

# Wind speed (forecast and observed both in mph)
record_observed_wind("Tokyo", "icon_seamless", date(2026, 4, 17), 18.5, 21.3)
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
    ┌──────┼──────────┬──────────┐
    │      │          │          │
   temp  precip     snow       wind
    │      │          │          │
temperature_2m_max  precipitation_sum  snowfall_sum  wind_speed_10m_max
(per model ensemble)(per model ensemble)(per model)(per model ensemble)
    │      │          │          │
  KDE   empirical  empirical   KDE
  (°F)  counts(mm) counts(cm) (mph, kph→mph)
    │      │          │          │
    └──────┴──────────┴──────────┘
           │
  all_bucket_probabilities()  ← uniform interface
           │
   normalize to sum=1
           │
   EV = model_prob / ask - 1
           │
   Kelly sizing → position
```

The `all_bucket_probabilities()` method is implemented on all four forecast types, making `strategy.py` completely type-agnostic — adding a 5th market type requires only a new forecast class + bucket parser.

---

## Analytics & Edge Validation

This bot is built around the principle that **measurement comes before optimization**. The path from "scan shows +100% EV everywhere" to "real, measured edge" runs through a strict validation discipline. The analytics commands above (`side-pnl`, `slice-dash`, `yes-score`) and the patterns below are the framework.

### The reliability of self-reported EV

Raw EV from the scanner is meaningless until calibration data exists. A freshly-deployed bot will *always* show inflated EVs because:

- The ensemble KDE is chronically under-dispersed (~1.5× too narrow vs. realized error)
- Without resolved trades the calibration curve is identity passthrough
- Stored model probabilities therefore equal raw KDE probabilities — already 2× overconfident

The corrections needed (variance inflation in `forecast.py`, Laplace-smoothed empirical calibration in `calibration.py`, the `min_model_prob` gate in `strategy.py`) only engage once shadow data accumulates. **Treat EV from the first ~30 resolved trades as cosmetic, not real.** After that the curve has enough data to meaningfully deflate overconfident picks.

### The binary-options payoff asymmetry (read this before sizing)

A bet of $S at ask price `p` has structural asymmetry:

```
On win   :  profit = $S × (1/p − 1)        (bounded; smaller when p is larger)
On loss  :  loss   = −$S                   (whole stake, always)
Break-even win rate = p exactly
```

Implications:

- Buying NO at ask 0.65 → risk $50 to win ~$27. Need 65%+ win rate just to break even. Hard.
- Buying YES at ask 0.30 → risk $50 to win ~$117. Need 30% win rate to break even. Easier and bigger payouts on hits.

**Edge in prediction markets tends to live where price is asymmetric in your favour.** Cheap-longshot YES bets win less often *but pay more when they hit* — exactly the behavioural mispricing zone where retail flow doesn't bet.

### The market-efficiency check

When your calibrated model probability ≈ the market's ask price for a given pattern, **the market already knows what you know**. Polymarket weather markets are watched by people running the same publicly available models (ECMWF, GFS, ICON via Open-Meteo). Once your bot is correctly calibrated, the NO-side of confident bets tends to collapse to that fair-priced regime: win rate ≈ ask, ROI ≈ 0%.

That's not a failure — it's *correctness*. It tells you to look elsewhere for edge:
1. **Better data** (paid weather feed, station-level observations vs. gridded model output)
2. **Better processing** (regional high-res models — HRRR for US, AROME/ICON-D2 for Europe, JMA-MSM for Japan — typically beat the public global stack)
3. **Behavioural mispricing** (where retail flow systematically misprices)
4. **Speed/access** (faster reaction to news than the market)

For this codebase, #3 is the most empirically supported path: YES longshots in `ask 0.20–0.40` and open-ended buckets ("X°F or higher") show consistent positive lift over break-even.

### Validation workflow — the gauntlet a strategy must pass

Run these checks in order. Any single failure is reason to *not* go live.

**1. Mechanism validation (does the plumbing work?)** — verify with logs and the analytics commands:
- Bias correction values non-zero per city
- Variance inflation firing (`dispersion inflated X→Y°F` debug lines)
- Calibration curve has ≥ 30 samples and uses Laplace smoothing (no hard 0.0 or 1.0 bin)
- No persistent Open-Meteo rate limiting
- Per-bucket dedup in effect (no duplicate `(city, target_date, bucket_label, side)`)

**2. Aggregate-statistical validation** — `side-pnl`:
- 95% CI lower bound on win rate clears the avg ask paid
- ROI positive at n ≥ 100 (per side)
- Cohort trajectory (split sample into thirds) shows the recent cohort isn't worst

**3. Concentration check** — `slice-dash` and manual:
- No single city > 40% of total P&L
- Top-3 trades (by P&L) < 60% of total profit
- Edge holds in the largest sub-slice (n ≥ 30) of `ask × bucket-type`

**4. Out-of-sample stability** — `yes-score` (or hand-coded equivalent):
- Leave-one-out classification accuracy ≥ 15pp above base rate
- LOO accuracy stable across 2–3 consecutive reruns spanning ≥ 20 new resolves each
- Score's top-half-vs-bottom-half ROI gap holds in LOO, not just in-sample

If all four levels pass at n ≥ 100 (per side), you have a defensible edge. Anything less is "suggestive, keep shadowing."

### Reading the cohort trajectory honestly

Splitting the resolved sample into chronological cohorts is the single most informative view:

```
cohort 1 (earliest)  →  immature calibration, may show negative ROI even on real edge
cohort 2             →  calibration warming up
cohort 3 (mature)    →  the trajectory you can trust
```

Aggregate P&L on a fresh deployment is *always* dragged by the early-cohort warm-up bleed. The signal to act on is the **most recent cohort's** behaviour, not the all-time aggregate. The `slice-dash` chronological cohort split surfaces this directly.

⚠️ **Cohort-4 peak is variance, not the new normal.** If one cohort prints 70%+ win rate, expect regression to mean in the next. Don't size up off the peak — confirm via *sustained* performance across multiple cohorts.

### Fat-tail discipline for longshot strategies

Strategies that rely on cheap-longshot YES bets are inherently fat-tailed: most bets lose, a few pay 4×+ stake. Properties to expect:

- **Variance is large.** ROI in any 30-trade window may swing ±20pp from the true edge.
- **Top 3 wins may carry > 50% of total P&L.** Not pathological — it's the *shape* of the strategy.
- **Judge by 50-trade rolling cohorts, not by week-by-week or day-by-day.** Short windows will whip you around emotionally.
- **Drawdowns are normal even with real edge.** A losing 20-trade window is consistent with a +15% true ROI strategy.

### Common ways validation fails — and what they mean

| Symptom | Likely cause | Action |
|---|---|---|
| Aggregate P&L stays negative as n grows past 100 | No real edge at current config; market efficient on what you're betting | Reconsider strategy: which side, which patterns, model upgrades |
| Recent cohort regresses to break-even after a strong cohort | Earlier peak was variance | Keep collecting, don't size up |
| Edge concentrated in one city or one bucket type | May be real local mispricing OR overfit | Stress-test: does it hold for that city alone across n ≥ 30? |
| Win rate ≈ avg ask perfectly | Calibration is correct AND market is efficient | "Market knows what you know" — find a different edge source |
| One side massively positive, other negative | Strong asymmetric edge | Drop the losing side; isolate the experiment on the winning side |
| LOO accuracy stuck at base rate | Features have no predictive power | Engineer better features OR accept no exploitable pattern |

### The discipline of waiting

The validation workflow can take 10–15 days of shadowing per ~100-trade chunk per side. During that wait:

- **Build analytics tools** (the commands above are examples) — no contamination
- **Audit losses for patterns** — note findings, do not act on them
- **Resist all config changes** — every change resets the validation clock and forces you to discard your sample
- **Do not reset `trades.db`** — the calibration curve currently fit on N samples is an asset; throwing it away costs you days

The single most common failure mode of this kind of bot is **changing strategy mid-shadow because of psychological discomfort with a temporary drawdown.** The cohort split tells you whether the drawdown is the early-cohort drag (acceptable) or a real regression (signal). Trust the metric, not the gut.

### Contrarian YES Inversion (Option F)

A finding from deep-diving the post-gate YES sample at n=95: the bot's YES picks have a **stable -5.5% win-rate gap below break-even, consistent across four weekly cohorts.** That's not noise around fair — it's a structural fingerprint of the temperature KDE being overconfident exactly in the band where the strategy chooses to act. The mirror of that fingerprint is a +5.5% gap on the **NO side of those same markets**.

Mechanism in plain words:

1. The model's KDE assigns ~55–65% probability to "moderate-tail" temperature buckets (1–3°F from the bias-corrected forecast mean).
2. A naive Gaussian centered at the same forecast mean with σ = empirical error std (~2.0°F) assigns 10–25% to those same buckets.
3. The market prices them at ~33%.
4. Reality lands at ~24% (very close to the Gaussian and the market, not the KDE).
5. Buying YES at the market ask therefore overpays for forecast confidence the model can't actually deliver.
6. Buying **NO** on the same bucket at the real NO ask captures the mirror — pay `1 − p` for the bet that wins `1 − p + 0.055` of the time.

Mathematically:

```
YES side  :  ask = p, realized win rate = p − 0.055        ROI ≈ −5.5%
NO side   :  ask = 1−p, realized win rate = (1−p) + 0.055  ROI ≈ +5.5%
```

The strategy is implemented as a config flag, not a code branch:

```bash
# .env
CONTRARIAN_YES_INVERSION=true
```

When set, [src/strategy.py](src/strategy.py) runs the normal decision logic unchanged — same markets scanned, same buckets evaluated, same `min_ev` and `min_model_prob` gates applied — and only flips the SIDE at the very end if the chosen side is YES. Natural NO picks pass through untouched. Each inverted trade is recorded with a `contrarian=1` flag in `trades.db` so analytics can isolate them.

**Why this is unusual but defensible:** the contrarian play profits from your own model's known broken-ness. It's not a strategy you keep forever; it's a strategy you deploy while you investigate or fix the root cause (KDE bandwidth, ensemble outlier handling, intra-day staleness). The moment the underlying YES overconfidence disappears, the contrarian edge dies too.

**Validation checklist before letting it run live:**

1. Run `python run.py contrarian-pnl` after ~10–15 days of shadow data
2. The contrarian row must show 🟢 EDGE CONFIRMED (CI lower bound clears its avg ask)
3. **All weekly cohorts must be positive** — this is the test that prior false-positives (open-ended drop, mid-band gate, lead-time filter) all failed
4. Contrarian ROI must be noticeably better than natural-YES baseline ROI (otherwise the flag isn't helping)
5. NO side P&L must not have degraded (the natural NO row is the control group — it should stay near fair)

**If validation fails**, the right pivot is Option E: set `MIN_MODEL_PROB=1.0` to stop all YES picks. NO-only is roughly break-even and produces zero further bleed. Then investigate forecast-pipeline fixes (KDE replacement, ensemble outlier handling) before re-enabling YES bets in any form.

---

## Risk Management

- **Validation ladder** — dry-run → shadow (≥1 week, ≥30 resolved trades) → live. Never skip shadow.
- **Shadow exit criteria** — win rate ≥ 55%, positive total P&L, no single city > 50% of trades before going live.
- **Quarter Kelly (0.25)** is recommended — reduces variance vs full Kelly
- **Precip/snow liquidity** — these markets often have lower volume; respect `max_usdc` limits
- **Nominatim rate limit** — 1 req/sec enforced; don't run multiple instances simultaneously
- **Model skill varies** — ECMWF is best for 1–3 day temperature; GFS often better for US precip extremes
- Not financial advice — prediction markets carry full capital risk

---

## License

MIT. Use at your own risk.
