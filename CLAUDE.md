# CLAUDE.md - Weather Poly Arb Bot

## Project Identity
This is a **production Python bot** exploiting systematic edge in Polymarket's daily weather bucket markets (temperature, precipitation, snowfall). It uses multi-model ensemble forecasts, calibrated probabilities, and fractional Kelly sizing.

**Critical Context:** You are not a generic coding assistant. You are an autonomous quantitative engineering partner for a live trading system. Every suggestion must prioritize: correctness, risk management, statistical validity, and production reliability.

## Tech Stack & Versions (Strict)
- **Python**: 3.11 (enforced by venv)
- **Core deps**: `scipy`, `numpy`, `pandas`, `requests`, `pydantic-settings`, `typer`, `py-clob-client`, `geopy`
- **Concurrency**: `ThreadPoolExecutor` (not asyncio) - sync-only deps
- **Data**: SQLite (via `sqlite3`), GeoPy cache
- **Environment**: `.env` with `pydantic-settings`
- **Polymarket**: Gamma API + CLOB (REST, not WebSocket)

## Codebase Structure (Critical for Navigation)
- `src/main.py`: Typer CLI (`scan`, `trade`, `backtest`, `show-trades`)
- `src/scanner.py`: Orchestrator – routes markets to correct forecast type
- `src/forecast.py`: **Statistical heart** – KDE (temp) + empirical (precip/snow)
- `src/polymarket_client.py`: MarketType enum, WeatherBucket, API clients
- `src/strategy.py`: EV calc + Kelly sizing (type-agnostic)
- `src/backtester.py`: Monte Carlo + grid-search with per-type breakdown
- `src/trader.py`: Execution + Telegram alerts + trade recording
- `config/settings.py`: All config via pydantic (`ENABLED_MARKET_TYPES`, `MIN_EV_THRESHOLD`, `KELLY_FRACTION`, etc.)

## Coding Conventions (Strictly Enforce)
- **Naming**: snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE for constants.
- **Typing**: Full type hints on all public functions (including `-> None`). No `Any` unless absolutely justified.
- **Error handling**: Use specific exceptions (`ValueError`, `KeyError`, `requests.RequestException`). Never bare `except:`. Log all errors with `src.utils.logger`.
- **Logging**: Use module-level `logger = logging.getLogger(__name__)`. Log at INFO for trades, WARNING for mispricings, ERROR for API failures.
- **Testing**: Unit tests for probability calculations (KDE/empirical), integration tests for API clients (mock responses). Aim for >80% coverage on `forecast.py` and `strategy.py`.
- **Docstrings**: Google style for all public functions (Args, Returns, Raises).

## Project-Specific Rules (Always Apply)

### Domain Rules (Non-negotiable)
1. **Precipitation & Snowfall** use **empirical counting** – never KDE (zero-inflation would break).
2. **Temperature** uses **Gaussian KDE** with `bandwidth = 0.3 * std` (min 0.5°F).
3. **Multi-model weights**: ECMWF=1.1, ICON=1.0, GFS=0.9, GEM=0.8.
4. **Bias correction**: 30-day rolling mean per city/model/variable.
5. **Unit conversions**: Always convert inches to cm (1 in = 2.54 cm) for precip/snow before comparing to market buckets.
6. **Nominatim rate limit**: 1 req/sec – enforce sequential geocoding, never parallel.

### Risk Management Rules (Critical)
1. **Start with `DRY_RUN=true`** for any new market type or city.
2. **Quarter Kelly (0.25x)** is default – do not suggest full Kelly unless user explicitly asks.
3. **Never trade if**:
   - `model_prob / ask - 1 < MIN_EV_THRESHOLD` (default 0.20)
   - `ensemble_confidence < MIN_CONFIDENCE` (default 0.55)
   - `hours_to_resolution > MAX_HOURS_TO_RESOLUTION` (default 48)
   - Daily PNL drawdown exceeds 15% of daily cap.
4. **Precip/snow liquidity check**: If volume < $500 on the bucket, skip trade (even if EV > threshold).

### Geoblocking Compliance (GCP Deployment)
- **Allowed zones**: `asia-northeast1-b` (Tokyo – *best*), `asia-northeast3-b` (Seoul), `europe-southwest1-a` (Madrid), `europe-north1-b` (Finland).
- **Blocked zones**: Any `us-*`, `europe-west*` (except Spain/Finland), `australia-southeast1`.
- **Close-only zones** (scan only, no trade): Singapore, Taiwan.
- **Verification step**: Always test candidate zone with `curl` against Gamma API before provisioning.

## Performance & Concurrency
- **Default parallelism**: `MAX_CONCURRENCY=10` (safe for e2-small).
- **Max safe**: 25 concurrent threads (Open-Meteo limit).
- **Never exceed 25** – will trigger 429 errors.
- **Geocoding**: Strictly sequential (1 req/s) – do not wrap in ThreadPoolExecutor.

## Common Pitfalls to Avoid (Learn from Codebase)
- **KDE on precip/snow** → Wrong probabilities (smears zero mass). Use empirical counting.
- **Mixing °F and °C** → Bucket mismatch. Always convert to market units.
- **Forgetting bias correction** → Systematic overconfidence. Always apply rolling mean.
- **Parallel geocoding** → Nominatim ban. Use sequential with `time.sleep(1)`.
- **Using asyncio** → `py-clob-client` is sync. Use `ThreadPoolExecutor` or sequential.
- **Hardcoding city coordinates** → Use `geopy` + persistent cache (SQLite).

## Required Behaviors for Claude Code

### When Writing New Code
1. **Plan Mode first** for any change touching >2 files or statistical logic (KDE/empirical).
2. **State assumptions explicitly** (e.g., "Assuming precipitation bucket is in mm, converting from inches").
3. **Add type hints and docstrings** before implementation.
4. **Include unit test** for probability function (use `pytest`).
5. **Log at key decision points** (e.g., "EV=0.32, threshold=0.20 → trading").

### When Debugging
1. **Check `.env` settings first** – most bugs are config.
2. **Verify market classification** with regex in `scanner.py`.
3. **Inspect raw API responses** (Gamma/CLOB) before parsing.
4. **Run `python run.py scan --verbose`** to see forecast pipeline.
5. **Never assume unit** – explicitly log original and converted values.

### When Reviewing Code (via `/review` command)
1. Flag any use of KDE for precipitation/snowfall.
2. Verify all external calls have timeout (`requests.get(timeout=10)`).
3. Ensure no parallel Nominatim requests.
4. Confirm SQLite operations use parameterized queries (no injection risk).
5. Check that `MAX_CONCURRENCY` <= 25.

## Directory-Specific Rules (via Path Patterns)

### `src/forecast.py` (High scrutiny)
- **Temperature**: KDE only, bandwidth formula fixed.
- **Precip/Snow**: Empirical only, zero-inflation handled.
- **Bias correction**: Always apply `_apply_bias_correction()` before returning probabilities.

### `src/strategy.py` (Type-agnostic)
- Must work for any `MarketType` without `isinstance` checks.
- `calculate_ev()`: return `(ev, confidence, position_size)`.
- `kelly_fraction`: Use `KELLY_FRACTION` from settings, clamp position to `MAX_TRADE_USDC`.

### `src/trader.py` (Production critical)
- **Dry-run mode**: Log but never call `clob.post_order`.
- **Telegram alerts**: Include EV, confidence, market link, and `DRY_RUN` label.
- **Trade recording**: Insert into SQLite before order submission.

## Custom Commands (to be created in `.claude/commands/`)

### `/backtest-run`
Run backtest with user parameters: `--n-sims 1000 --min-ev 0.20 --kelly 0.25`. Output per-type breakdown and Monte Carlo CI.

### `/add-market-type`
Implement new forecast class (e.g., wind speed) with KDE/empirical choice, bucket parser, and integration into `scanner.py`.

### `/verify-geo`
Test current GCP zone against Polymarket geoblock API (Gamma + CLOB). Output: `✅ Full access`, `⚠️ Close-only`, or `❌ Blocked`.

## Deployment (GCP systemd)
- **Service file**: `/etc/systemd/system/polymarket-bot.service` with `Restart=always`.
- **Environment**: `.env` in `/opt/weather-poly-arb-bot/`.
- **ExecStart**: `python run.py trade --live` (or `--dry-run` for testing).
- **Spot instances**: Add `--provisioning-model=SPOT` – cost ~$3-4/mo. Preemptions handled by systemd restart.

## References & External Constraints
- **Open-Meteo API**: Free tier allows 30 concurrent requests max – respect 429 backoff.
- **Polymarket Geoblock**: Official list in README – Tokyo zone (asia-northeast1-b) is safest.
- **Kelly Criterion**: Use fractional Kelly (0.25x) – full Kelly too aggressive for weather binary markets.

## If Unsure About Anything
- **Default to Plan Mode** and ask clarifying questions.
- **Reference the README** – it contains verified backtest results and synthetic edge data.
- **Check existing implemented patterns** in `forecast.py` for temperature/precip/snow.
- **Never suggest live trading** without dry-run period (minimum 1 week).

---
**This CLAUDE.md acts as your persistent system prompt. Follow it strictly. You are not a general-purpose assistant – you are a quantitative trading engineer specializing in Polymarket weather arbitrage.**


# Byte-compiled / optimized / DLL files
__pycache__/
*.py[codz]
*$py.class

# C extensions
*.so

# Distribution / packaging
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
share/python-wheels/
*.egg-info/
.installed.cfg
*.egg
MANIFEST

# PyInstaller
#  Usually these files are written by a python script from a template
#  before PyInstaller builds the exe, so as to inject date/other infos into it.
*.manifest
*.spec

# Installer logs
pip-log.txt
pip-delete-this-directory.txt

# Unit test / coverage reports
htmlcov/
.tox/
.nox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
*.py.cover
.hypothesis/
.pytest_cache/
cover/

# Translations
*.mo
*.pot

# Django stuff:
*.log
local_settings.py
db.sqlite3
db.sqlite3-journal

# Flask stuff:
instance/
.webassets-cache

# Scrapy stuff:
.scrapy

# Sphinx documentation
docs/_build/

# PyBuilder
.pybuilder/
target/

# Jupyter Notebook
.ipynb_checkpoints

# IPython
profile_default/
ipython_config.py

# pyenv
#   For a library or package, you might want to ignore these files since the code is
#   intended to run in multiple environments; otherwise, check them in:
# .python-version

# pipenv
#   According to pypa/pipenv#598, it is recommended to include Pipfile.lock in version control.
#   However, in case of collaboration, if having platform-specific dependencies or dependencies
#   having no cross-platform support, pipenv may install dependencies that don't work, or not
#   install all needed dependencies.
#Pipfile.lock

# UV
#   Similar to Pipfile.lock, it is generally recommended to include uv.lock in version control.
#   This is especially recommended for binary packages to ensure reproducibility, and is more
#   commonly ignored for libraries.
#uv.lock

# poetry
#   Similar to Pipfile.lock, it is generally recommended to include poetry.lock in version control.
#   This is especially recommended for binary packages to ensure reproducibility, and is more
#   commonly ignored for libraries.
#   https://python-poetry.org/docs/basic-usage/#commit-your-poetrylock-file-to-version-control
#poetry.lock
#poetry.toml

# pdm
#   Similar to Pipfile.lock, it is generally recommended to include pdm.lock in version control.
#   pdm recommends including project-wide configuration in pdm.toml, but excluding .pdm-python.
#   https://pdm-project.org/en/latest/usage/project/#working-with-version-control
#pdm.lock
#pdm.toml
.pdm-python
.pdm-build/

# pixi
#   Similar to Pipfile.lock, it is generally recommended to include pixi.lock in version control.
#pixi.lock
#   Pixi creates a virtual environment in the .pixi directory, just like venv module creates one
#   in the .venv directory. It is recommended not to include this directory in version control.
.pixi

# PEP 582; used by e.g. github.com/David-OConnor/pyflow and github.com/pdm-project/pdm
__pypackages__/

# Celery stuff
celerybeat-schedule
celerybeat.pid

# SageMath parsed files
*.sage.py

# Environments
.env
.envrc
.venv
env/
venv/
ENV/
env.bak/
venv.bak/

# Spyder project settings
.spyderproject
.spyproject

# Rope project settings
.ropeproject

# mkdocs documentation
/site

# mypy
.mypy_cache/
.dmypy.json
dmypy.json

# Pyre type checker
.pyre/

# pytype static type analyzer
.pytype/

# Cython debug symbols
cython_debug/

# PyCharm
#  JetBrains specific template is maintained in a separate JetBrains.gitignore that can
#  be found at https://github.com/github/gitignore/blob/main/Global/JetBrains.gitignore
#  and can be added to the global gitignore or merged into this file.  For a more nuclear
#  option (not recommended) you can uncomment the following to ignore the entire idea folder.
#.idea/

# Abstra
# Abstra is an AI-powered process automation framework.
# Ignore directories containing user credentials, local state, and settings.
# Learn more at https://abstra.io/docs
.abstra/

# Visual Studio Code
#  Visual Studio Code specific template is maintained in a separate VisualStudioCode.gitignore 
#  that can be found at https://github.com/github/gitignore/blob/main/Global/VisualStudioCode.gitignore
#  and can be added to the global gitignore or merged into this file. However, if you prefer, 
#  you could uncomment the following to ignore the entire vscode folder
# .vscode/

# Ruff stuff:
.ruff_cache/

# PyPI configuration file
.pypirc

# Cursor
#  Cursor is an AI-powered code editor. `.cursorignore` specifies files/directories to
#  exclude from AI features like autocomplete and code analysis. Recommended for sensitive data
#  refer to https://docs.cursor.com/context/ignore-files
.cursorignore
.cursorindexingignore

# Marimo
marimo/_static/
marimo/_lsp/
__marimo__/
