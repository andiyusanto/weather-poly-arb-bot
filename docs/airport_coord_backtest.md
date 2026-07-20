# Airport-coordinate relocation — KILLED (2026-07-20)

**Hypothesis tested:** the ~2.4–2.7°F station-vs-grid divergence that dominates
settlement error is a recoverable *spatial* term — i.e. forecasting the airport
gridpoint (ICAO → lat/lon from IEM) instead of the city-center geocode would
shrink the settlement-error sigma.

**Result: falsified for every city we trade. Do not change the forecast geocode.**

## Method
For each of 48 station-mapped cities: fetch OM `historical-forecast` archived
daily tmax at (a) our city-center geocode and (b) the airport coords IEM returns
via `latlon=yes`, over each city's observed date range. Error = `om_tmax − station_tmax`,
scored against the station-observed daily max (truth, `bias_corrections.db`
`model='station'` rows, n=727). Backtest script: scratchpad `station_coord_bt.py`
(not committed — one-shot).

## Numbers
Global: σ_center **2.71** → σ_airport **2.56** (−5.5%), and relocating *added* a
cold bias (−0.52 → −1.02°F). Not the large spatial recovery we were testing for.

Allowlist (the cities we actually trade) — neutral-to-harmful:

| city | moved | σ_center | σ_airport | reduction |
|---|---|---|---|---|
| chengdu | 5 km | 2.25 | 2.59 | −15% |
| guangzhou | 30 km | 2.22 | 2.51 | −13% |
| jeddah | 13 km | 2.46 | 2.53 | −3% |
| manila | 10 km | 1.68 | 2.07 | −23% |
| mexico city | 7 km | 2.47 | 2.47 | 0% |
| moscow | 39 km | 1.30 | 1.27 | +2% |

Four of six degrade at the airport; the other two are flat.

## Where airport *did* win — all non-traded cities with pathological geocodes
istanbul 72%, toronto 68%, helsinki 41%, miami 32%, shanghai 20%, dallas 19%,
austin 18%. These are cities whose city-center geocode lands on water/terrain
(Bosphorus, lake shoreline). The airport rescues them — but none are in our
allowlist.

## Verdict
The divergence is **intrinsic grid-vs-point noise for our cities, not a
recoverable relocation term.** The per-city settlement offset already shipped
(`data/settlement_offsets.json`) captures the mean shift; the residual sigma is
exactly what the station-fit EMOS sigma already models. This *validates* the
current stack (station-fit EMOS σ + per-city offset) rather than replacing it.

**Actionable nugget (future, not now):** if the allowlist ever expands to
Istanbul / Toronto / Helsinki / Miami, forecast *those* cities at airport coords
— per-city, not global. Do not touch the geocode during the Aug 1 verdict freeze.

## Where this sits vs the other backtests
Three backtests now converge:
- bias-recorder end-to-end (station-fit EMOS): the win (coverage 51%→68%,
  log-score −2.178→−1.863, top1 21.9%→24.7%, Brier 0.869→0.821).
- lead-decay / same_day sigma: marginal (4–8% on trading sigma).
- airport relocation: **≤0% for traded cities — dead.**

The station-fit EMOS + per-city offset stack is the win; the follow-on
divergence candidates are marginal-to-dead. Aug 1 pre-registered verdict remains
the gate. This note changes nothing in the frozen funnel.
