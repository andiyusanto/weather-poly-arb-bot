"""
Backtester — Monte Carlo + grid-search hyperparameter optimizer.
Supports temperature, precipitation, and snowfall opportunity specs.

Each market type has different synthetic edge profiles:
  - Temperature:    moderate edge (~8%), higher trade volume
  - Precipitation:  higher edge (~12%), fatter tail mispricings
  - Snowfall:       highest edge (~14%), very low competition
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from loguru import logger
from rich.console import Console
from rich.table import Table

from config.settings import settings
from src.polymarket_client import MarketType
from src.strategy import compute_ev, kelly_fraction, suggested_position_size

console = Console()

# Per-type synthetic edge parameters (mean edge above market price)
_TYPE_EDGE_PROFILES: Dict[str, Dict] = {
    "temperature":   {"edge": 0.08, "noise": 0.05, "market_price_range": (0.10, 0.75)},
    "precipitation": {"edge": 0.12, "noise": 0.06, "market_price_range": (0.05, 0.65)},
    "snowfall":      {"edge": 0.14, "noise": 0.07, "market_price_range": (0.05, 0.55)},
}

_TYPE_CITIES: Dict[str, List[str]] = {
    "temperature":   ["Seoul", "Tokyo", "London", "Miami", "Chicago", "Paris", "Dubai"],
    "precipitation": ["Hong Kong", "Singapore", "Miami", "Sydney", "London", "Bangkok"],
    "snowfall":      ["Chicago", "Toronto", "Warsaw", "Seoul", "Tokyo", "New York"],
}

_TYPE_BUCKET_LABELS: Dict[str, List[str]] = {
    "temperature":   ["60-65°F", "65-70°F", "70-75°F", "75-80°F", "80-85°F", "85-90°F", "90-95°F"],
    "precipitation": ["0 mm", "< 1 mm", "1-5 mm", "5-10 mm", "10-25 mm", ">= 25 mm"],
    "snowfall":      ["0 cm", "< 1 cm", "1-5 cm", "5-10 cm", ">= 10 cm"],
}


@dataclass
class BacktestTrade:
    date: date
    city: str
    bucket_label: str
    market_type: str
    model_prob: float
    market_price: float
    ev: float
    size_usdc: float
    won: bool
    pnl: float


@dataclass
class BacktestMetrics:
    n_trades: int
    win_rate: float
    total_pnl: float
    roi: float
    sharpe: float
    max_drawdown: float
    avg_ev: float
    avg_confidence: float
    trades_per_day: float
    # Per-type breakdown
    by_type: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"Trades={self.n_trades} WinRate={self.win_rate:.1%} "
            f"PNL={self.total_pnl:+.2f} ROI={self.roi:.1%} "
            f"Sharpe={self.sharpe:.2f} MaxDD={self.max_drawdown:.1%} "
            f"T/Day={self.trades_per_day:.1f}"
        )


# ── Metrics computation ───────────────────────────────────────────────────────

def _compute_metrics(
    trades: List[BacktestTrade],
    total_days: int,
    params: Dict[str, Any],
) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, {}, params)

    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    win_rate = wins / n
    total_pnl = sum(t.pnl for t in trades)
    total_risked = sum(t.size_usdc for t in trades)
    roi = total_pnl / total_risked if total_risked > 0 else 0.0
    avg_ev = float(np.mean([t.ev for t in trades]))

    # Daily PNL series for Sharpe
    daily: Dict[date, float] = {}
    for t in trades:
        daily[t.date] = daily.get(t.date, 0.0) + t.pnl
    pnl_series = list(daily.values())
    if len(pnl_series) > 1:
        mean_d = np.mean(pnl_series)
        std_d = np.std(pnl_series, ddof=1)
        sharpe = float(np.sqrt(252) * mean_d / std_d) if std_d > 0 else 0.0
    else:
        sharpe = 0.0

    cumulative = np.cumsum([t.pnl for t in trades])
    peak = np.maximum.accumulate(cumulative)
    drawdown = (peak - cumulative) / (np.abs(peak) + 1e-9)
    max_dd = float(np.max(drawdown))

    # Per-type breakdown
    by_type: Dict[str, Dict[str, Any]] = {}
    for mt in ["temperature", "precipitation", "snowfall"]:
        mt_trades = [t for t in trades if t.market_type == mt]
        if not mt_trades:
            continue
        mt_wins = sum(1 for t in mt_trades if t.won)
        by_type[mt] = {
            "n": len(mt_trades),
            "win_rate": mt_wins / len(mt_trades),
            "pnl": sum(t.pnl for t in mt_trades),
            "roi": sum(t.pnl for t in mt_trades) / max(sum(t.size_usdc for t in mt_trades), 1e-9),
        }

    return BacktestMetrics(
        n_trades=n, win_rate=win_rate, total_pnl=total_pnl,
        roi=roi, sharpe=sharpe, max_drawdown=max_dd,
        avg_ev=avg_ev, avg_confidence=0.0,
        trades_per_day=n / max(total_days, 1),
        by_type=by_type, params=params,
    )


# ── Monte Carlo ───────────────────────────────────────────────────────────────

def monte_carlo_backtest(
    opportunity_specs: List[Dict],
    n_simulations: int = 1000,
    bankroll: float = 1000.0,
    kelly_mult: float = 0.25,
    min_ev: float = 0.20,
    max_usdc_per_trade: float = 50.0,
    daily_max_usdc: float = 500.0,
) -> Tuple[BacktestMetrics, List[BacktestMetrics]]:
    all_metrics: List[BacktestMetrics] = []
    total_days = len(set(s.get("date", date.today()) for s in opportunity_specs)) or 1

    for sim_idx in range(n_simulations):
        trades: List[BacktestTrade] = []
        daily_spent: Dict[date, float] = {}

        for spec in opportunity_specs:
            mp = spec["model_prob"]
            ask = spec["market_price"]
            ev = compute_ev(mp, ask)
            if ev < min_ev:
                continue

            d = spec.get("date", date.today())
            spent = daily_spent.get(d, 0.0)
            if spent >= daily_max_usdc:
                continue

            size = min(
                suggested_position_size(mp, ask, bankroll, kelly_mult, max_usdc_per_trade),
                daily_max_usdc - spent,
            )
            if size < 1.0:
                continue

            won = random.random() < mp
            pnl = size * ((1.0 / ask) - 1.0) if won else -size

            trades.append(BacktestTrade(
                date=d, city=spec.get("city", ""),
                bucket_label=spec.get("bucket_label", ""),
                market_type=spec.get("market_type", "temperature"),
                model_prob=mp, market_price=ask, ev=ev,
                size_usdc=size, won=won, pnl=pnl,
            ))
            daily_spent[d] = spent + size

        params = dict(min_ev=min_ev, kelly_mult=kelly_mult, max_usdc=max_usdc_per_trade)
        all_metrics.append(_compute_metrics(trades, total_days, params))

    if not all_metrics:
        return BacktestMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0), []

    mean_metrics = BacktestMetrics(
        n_trades=int(np.mean([m.n_trades for m in all_metrics])),
        win_rate=float(np.mean([m.win_rate for m in all_metrics])),
        total_pnl=float(np.mean([m.total_pnl for m in all_metrics])),
        roi=float(np.mean([m.roi for m in all_metrics])),
        sharpe=float(np.mean([m.sharpe for m in all_metrics])),
        max_drawdown=float(np.mean([m.max_drawdown for m in all_metrics])),
        avg_ev=float(np.mean([m.avg_ev for m in all_metrics])),
        avg_confidence=0.0,
        trades_per_day=float(np.mean([m.trades_per_day for m in all_metrics])),
        params=dict(min_ev=min_ev, kelly_mult=kelly_mult,
                    max_usdc=max_usdc_per_trade, n_sims=n_simulations),
    )
    return mean_metrics, all_metrics


# ── Grid search ───────────────────────────────────────────────────────────────

def grid_search_params(
    opportunity_specs: List[Dict],
    n_simulations: int = 500,
    bankroll: float = 1000.0,
) -> List[Tuple[BacktestMetrics, Dict]]:
    param_grid = {
        "min_ev":    [0.10, 0.15, 0.20, 0.25, 0.30],
        "kelly_mult": [0.15, 0.25, 0.35, 0.50],
        "max_usdc":  [25.0, 50.0, 100.0],
    }

    results: List[Tuple[BacktestMetrics, Dict]] = []
    combos = list(itertools.product(
        param_grid["min_ev"], param_grid["kelly_mult"], param_grid["max_usdc"],
    ))
    logger.info(f"Grid search: {len(combos)} combos × {n_simulations} sims each")

    for min_ev, km, max_u in combos:
        params = dict(min_ev=min_ev, kelly_mult=km, max_usdc=max_u)
        metrics, _ = monte_carlo_backtest(
            opportunity_specs, n_simulations=n_simulations,
            bankroll=bankroll, kelly_mult=km,
            min_ev=min_ev, max_usdc_per_trade=max_u,
        )
        results.append((metrics, params))

    def _score(r: Tuple[BacktestMetrics, Dict]) -> float:
        m = r[0]
        if m.n_trades < 5:
            return -999.0
        return m.win_rate * 0.3 + m.roi * 0.4 + min(m.sharpe, 3.0) * 0.2 + min(m.trades_per_day, 5) * 0.02

    results.sort(key=_score, reverse=True)
    return results


# ── Synthetic opportunity generator ──────────────────────────────────────────

def generate_synthetic_opportunities(
    n: int = 500,
    edge_pct: Optional[float] = None,   # None = use per-type profiles
    noise_std: float = 0.05,
    start_date: Optional[date] = None,
    days: int = 30,
    market_types: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Generate synthetic opportunity specs across all enabled market types.
    Each type uses calibrated edge / price range / city / bucket label profiles.
    """
    if market_types is None:
        market_types = list(settings.enabled_market_type_set)

    rng = np.random.default_rng(42)
    start = start_date or (date.today() - timedelta(days=days))
    specs = []

    per_type = max(1, n // len(market_types))

    for mt in market_types:
        profile = _TYPE_EDGE_PROFILES.get(mt, _TYPE_EDGE_PROFILES["temperature"])
        cities = _TYPE_CITIES.get(mt, ["London"])
        buckets = _TYPE_BUCKET_LABELS.get(mt, ["bucket"])
        lo_p, hi_p = profile["market_price_range"]
        this_edge = edge_pct if edge_pct is not None else profile["edge"]

        for _ in range(per_type):
            offset = int(rng.integers(0, days))
            d = start + timedelta(days=offset)
            market_price = float(rng.uniform(lo_p, hi_p))
            edge = float(rng.normal(this_edge, noise_std))
            model_prob = float(np.clip(market_price + edge, 0.05, 0.95))
            specs.append(dict(
                date=d,
                city=str(rng.choice(cities)),
                bucket_label=str(rng.choice(buckets)),
                market_type=mt,
                model_prob=model_prob,
                market_price=market_price,
            ))

    return specs


# ── Rich display ──────────────────────────────────────────────────────────────

def display_backtest_results(results: List[Tuple[BacktestMetrics, Dict]], top_n: int = 10) -> None:
    console.rule("[bold cyan]Backtest Results — Grid Search[/bold cyan]")

    table = Table(header_style="bold magenta", border_style="dim")
    table.add_column("Rank",    justify="right", style="dim")
    table.add_column("MinEV",   justify="right")
    table.add_column("Kelly",   justify="right")
    table.add_column("MaxUSDC", justify="right")
    table.add_column("Trades",  justify="right")
    table.add_column("WinRate", justify="right", style="green")
    table.add_column("PNL$",    justify="right", style="bold yellow")
    table.add_column("ROI",     justify="right", style="cyan")
    table.add_column("Sharpe",  justify="right")
    table.add_column("MaxDD",   justify="right", style="red")
    table.add_column("T/Day",   justify="right")

    for rank, (m, p) in enumerate(results[:top_n], 1):
        table.add_row(
            str(rank),
            f"{p['min_ev']:.0%}",
            f"{p['kelly_mult']:.2f}x",
            f"${p['max_usdc']:.0f}",
            str(m.n_trades),
            f"{m.win_rate:.1%}",
            f"${m.total_pnl:+.2f}",
            f"{m.roi:.1%}",
            f"{m.sharpe:.2f}",
            f"{m.max_drawdown:.1%}",
            f"{m.trades_per_day:.1f}",
        )

    console.print(table)

    best_m, best_p = results[0] if results else (None, {})
    if best_m:
        console.print(f"\n[bold green]Recommended params:[/bold green] {best_p}")
        console.print(
            f"  Win rate: {best_m.win_rate:.1%}  |  "
            f"ROI: {best_m.roi:.1%}  |  Sharpe: {best_m.sharpe:.2f}"
        )

        # Per-type breakdown for the best config
        if best_m.by_type:
            console.print("\n[bold]Per-type breakdown (best config):[/bold]")
            t2 = Table(border_style="dim", header_style="bold")
            t2.add_column("Type")
            t2.add_column("Trades", justify="right")
            t2.add_column("WinRate", justify="right", style="green")
            t2.add_column("PNL$", justify="right", style="yellow")
            t2.add_column("ROI", justify="right", style="cyan")
            for mt, stats in sorted(best_m.by_type.items()):
                emoji = MarketType(mt).emoji
                t2.add_row(
                    f"{emoji} {mt}",
                    str(stats["n"]),
                    f"{stats['win_rate']:.1%}",
                    f"${stats['pnl']:+.2f}",
                    f"{stats['roi']:.1%}",
                )
            console.print(t2)

        console.print(f"\n  [dim]⚠️  Backtest results do not guarantee future performance.[/dim]\n")


def display_mc_percentiles(all_metrics: List[BacktestMetrics]) -> None:
    pnls = sorted([m.total_pnl for m in all_metrics])
    n = len(pnls)
    console.print("\n[bold]Monte Carlo PNL Distribution[/bold]")
    for pct in [5, 25, 50, 75, 95]:
        idx = max(0, int(pct / 100 * n) - 1)
        console.print(f"  P{pct:2d}: ${pnls[idx]:+.2f}")
    console.print()
