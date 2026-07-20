# Edge candidates beyond the point forecast (2026-07-20)

Once the point forecast was squeezed (airport relocation dead — see
[airport_coord_backtest.md](airport_coord_backtest.md); lead stratification
marginal), three further levers were tested offline against the collected
station-truth data (`bias_corrections.db`, model='station', n=727 residuals;
2026-05-27 → 2026-07-19). All are **research-only until the Aug 1 verdict** and
none touch the frozen live funnel.

**Scope note — fit on all, apply on allowlist.** The allowlist gate runs in
`scanner.py` *before* any forecast is computed, so real-money impact is
allowlist-only (currently 7: Mexico City, Wuhan, Guangzhou, Moscow, Jeddah,
Manila, Chengdu) by construction. But #1 and #2 should be *fit* on the full
~48–52-city logged pool (per-city n=20–45 is too thin to fit a skew or a spread
coefficient per city) and *applied* only to allowlist trades. Extra cities are
training data, not trading targets.

---

## #1 Spread-skill conditional sigma — REAL, ships behind EMOS flag

Cross-model ensemble spread (weighted std of ecmwf/gfs/icon/gem archived tmax)
predicts forecast error. Fetched per-model archived daily tmax from OM
historical-forecast; error vs station truth. n=727 city-days.

| spread tercile | n | model spread | RMSE | bias |
|---|---|---|---|---|
| LOW (agree)  | 242 | 0.15–1.10°F | **1.90** | −0.48 |
| MID          | 242 | 1.11–1.90°F | **2.60** | −0.44 |
| HIGH (scatter) | 243 | 1.90–6.93°F | **3.20** | −0.81 |

Pearson r(spread, |err|)=+0.23, Spearman +0.18 (noisy point-to-point, strong in
aggregate). OOS (even/odd split): conditional σ²=5.0+0.66·spread² beats flat σ,
NLL 2.317 vs 2.349 (gain +0.031 nats). Flat log-score gain understates the
trading value, which is asymmetric: on the ~⅓ low-spread days real RMSE 1.90 vs
the flat 2.73 we assume — 30% sharper than we price, i.e. size up / take more
buckets; on high-spread days shrink or skip.

**Allowlist per-city r:** Mexico City +0.28, Manila +0.23, Moscow +0.22,
Chengdu +0.17, Guangzhou +0.08 — but **Jeddah −0.13** with abnormal 3.1°F mean
spread (desert; spread is model artifact, not real uncertainty). ⇒ conditional
sigma needs a per-city guard so Jeddah's fake spread never widens its bets.

**Action (post-Aug-1, flag-gated):** EMOS σ = f(climatological σ, today's
ensemble spread), with a per-city guard.

---

## #2 Residual shape — REAL, ships behind EMOS flag

The 727 settlement residuals (forecast − station truth, after per-city offsets)
are not Gaussian:

| metric | value | Gaussian |
|---|---|---|
| mean | +0.36°F | — |
| sigma | 2.95°F | — |
| skew | **−0.61** | 0 |
| excess kurtosis | **+1.00** | 0 |
| \|resid\|>3σ | **1.2%** | 0.3% |

Left skew ⇒ the big misses are *hotter-than-forecast* days (missed heat spikes);
a symmetric Gaussian underweights the warm buckets and is overconfident in the
central bucket. Fat tails (3σ mass ~4× Gaussian) ⇒ the wings, where thin bucket
markets misprice most, carry more probability than the Gaussian assigns.

Per-city heterogeneous: Austin (skew −1.42, kurt +3.0) and Paris (−1.11, +2.1)
strongly non-Gaussian; Guangzhou/Helsinki near-symmetric and thin-tailed — so a
blanket skew fudge is wrong. Note **Guangzhou residual mean +3.05°F** even after
offsets ⇒ stale/underfit per-city offset, worth a separate fix.

**Action (post-Aug-1, flag-gated):** score buckets against the pooled empirical
residual distribution (shifted by the per-city offset), or a skew-normal
likelihood, instead of a Gaussian.

---

## #3 Peak-passed intraday ceiling — biggest upside, edge UNPROVEN

In daily-max markets the max is locked once the afternoon peak passes, yet
markets trade to local midnight. IEM hourly, n=2,122 station-days.

| | value |
|---|---|
| local hour of daily max | median 14:00 |
| confirmed-lock hour (temps stay < max−0.5°F after) | median 15.9 (~4pm) |
| deterministic window to local midnight | **median 8.1h** |

Locked by 16:00 local: 59% (≥8h); by 18:00: 90% (≥6h). Allowlist windows
uniform: 7.4–9.2h.

**Caveat (built into the test):** meteorological half only — how knowable the
outcome is, NOT whether the market misprices during the window. The market half
needs intraday order books we don't have historically. "Lock" uses hindsight;
real-time-knowable window is conservatively ~6h (the 18:00/90% row). Thin
evening books may cap fillable size ($500-volume rule).

**Action:** this is a *new trading mode* (same-day intraday), needs its own
validation window — cannot ride the Aug-1 EMOS flip. The one thing to do now:
**forward-capture the book during the post-lock window (observation only, no
trades)** to collect the market half, so the mode can be proven or killed later.

---

## Ranking

| candidate | status | value | path |
|---|---|---|---|
| #1 spread-skill | proven OOS | sizing edge on the ⅓ sharp days | EMOS flag, post-Aug-1 |
| #2 residual shape | proven (moments) | reprices tails / warm buckets | EMOS flag, post-Aug-1 |
| #3 peak-passed | ceiling only | median ~8h (≈6h real-time) window/day | new mode; forward-capture now |

#1 and #2 stack into the planned EMOS flip. #3 is the biggest prize on the
longest path; forward book-capture (observation-only) de-risks it without
touching the frozen funnel. Aug 1 pre-registered verdict remains the gate.
