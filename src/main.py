"""
Main entry point — Typer CLI with scan / trade / backtest commands.
Also hosts the APScheduler loop for continuous trading mode.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Optional

import typer
from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger
from rich.console import Console

from config.settings import settings
from src.utils import setup_logging

app = typer.Typer(
    name="weather-arb-bot",
    help="Polymarket weather temperature arbitrage bot.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _banner() -> None:
    console.print(
        "\n[bold cyan]╔══════════════════════════════════════════╗\n"
        "║   Polymarket Weather Arbitrage Bot       ║\n"
        "║   Temperature Bucket Edge Finder         ║\n"
        "╚══════════════════════════════════════════╝[/bold cyan]\n"
    )
    if settings.dry_run:
        console.print("[bold yellow]  ⚠️  DRY RUN MODE — no real orders will be placed[/bold yellow]\n")


def _log_startup_state(mode: str) -> None:
    """
    Emit a single sworn-statement line that proves what code + config is running.

    Solves the "is the running process actually the latest?" mystery: every restart
    visibly prints the git commit, key source file mtimes, and the gating settings
    in the first few log lines. If a stale process is ever serving traffic, this
    line tells you immediately. Logged at INFO so it lands in the rotated log files.
    """
    import os
    import subprocess
    from pathlib import Path

    def _git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=2, check=False
            )
            return (out.stdout or "").strip() or "?"
        except Exception:
            return "?"

    commit = _git("rev-parse", "--short=8", "HEAD")
    dirty = _git("status", "--porcelain")
    dirty_flag = "DIRTY" if dirty and dirty != "?" else "clean"

    # Source mtimes for the modules most likely to drift on this project.
    project_root = Path(__file__).resolve().parent.parent
    def _mtime(rel: str) -> str:
        p = project_root / rel
        if not p.exists():
            return "missing"
        return datetime.utcfromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(
        "STARTUP "
        f"mode={mode} "
        f"git={commit} ({dirty_flag}) "
        f"contrarian_yes_inversion={settings.contrarian_yes_inversion} "
        f"min_model_prob={settings.min_model_prob} "
        f"min_ev={settings.min_ev_threshold} "
        f"kelly={settings.kelly_fraction} "
        f"max_trade_usdc={settings.max_trade_usdc} "
        f"daily_max_usdc={settings.daily_max_usdc} "
        f"max_hours_to_resolution={settings.max_hours_to_resolution} "
        f"dry_run={settings.dry_run} "
        f"pid={os.getpid()}"
    )
    logger.info(
        "STARTUP source-mtimes "
        f"strategy.py={_mtime('src/strategy.py')} "
        f"trader.py={_mtime('src/trader.py')} "
        f"utils.py={_mtime('src/utils.py')} "
        f"forecast.py={_mtime('src/forecast.py')} "
        f"calibration.py={_mtime('src/calibration.py')}"
    )


@app.command()
def scan(
    min_ev: float = typer.Option(settings.min_ev_threshold, "--min-ev", help="Min EV threshold (0–1)"),
    min_conf: float = typer.Option(settings.min_confidence, "--min-conf", help="Min confidence (0–1)"),
    max_hours: float = typer.Option(settings.max_hours_to_resolution, "--max-hours", help="Max hours to resolution"),
    top_n: int = typer.Option(20, "--top-n", help="Number of opportunities to display"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose debug output"),
) -> None:
    """Scan for high-EV weather opportunities and display them."""
    setup_logging()
    if verbose:
        logger.remove()
        import sys
        logger.add(sys.stderr, level="DEBUG")

    _banner()
    from src.scanner import display_opportunities, run_scan

    result = run_scan(min_ev=min_ev, min_confidence=min_conf, max_hours=max_hours)
    display_opportunities(result, top_n=top_n)

    if not result.opportunities:
        console.print("[yellow]No opportunities found. Try lowering --min-ev or --min-conf.[/yellow]")
        raise typer.Exit(0)


@app.command()
def trade(
    min_ev: float = typer.Option(settings.min_ev_threshold, "--min-ev"),
    min_conf: float = typer.Option(settings.min_confidence, "--min-conf"),
    max_hours: float = typer.Option(settings.max_hours_to_resolution, "--max-hours"),
    top_n: int = typer.Option(10, "--top-n", help="Max trades per cycle"),
    bankroll: float = typer.Option(1000.0, "--bankroll", help="Bankroll for Kelly sizing (USDC)"),
    dry_run: bool = typer.Option(settings.dry_run, "--dry-run/--live", help="Dry run or live trading"),
    shadow: bool = typer.Option(False, "--shadow", help="Shadow mode: record trades for outcome tracking, no real orders"),
    once: bool = typer.Option(False, "--once", help="Run one cycle then exit (default: run on schedule)"),
    interval: int = typer.Option(settings.scan_interval_minutes, "--interval", help="Scan interval (minutes)"),
) -> None:
    """
    Auto-execute trades for high-EV opportunities.

    Modes (in order of risk):
      --dry-run   Log only, no DB record.  Safe for initial testing.
      --shadow    Record to DB, no real orders.  Use this to validate edge before going live.
      --live      Real orders via CLOB.  Requires POLY_* credentials in .env.

    ⚠️  WARNING: --live places REAL ORDERS. Run --shadow for ≥1 week first.
    """
    setup_logging()
    _banner()

    if shadow and not dry_run:
        # --shadow implies no live order, but we still need CLOB prices
        console.print("[bold yellow]  🟡 SHADOW MODE — recording trades for outcome validation[/bold yellow]\n")
        dry_run = True  # prevent any accidental live order path

    # Sworn-statement startup log so any future "is the bot running stale code?"
    # question is a 2-second answer — search for STARTUP in the day's log.
    _mode = "shadow" if shadow else ("dry_run" if dry_run else "live")
    _log_startup_state(_mode)

    if not dry_run and not shadow:
        if not settings.has_clob_creds:
            console.print("[bold red]ERROR: POLY_PRIVATE_KEY / POLY_API_KEY not set. Run setup.py first.[/bold red]")
            raise typer.Exit(1)
        console.print("[bold red]⚠️  LIVE TRADING MODE[/bold red]")
        confirm = typer.confirm("Are you sure you want to place real orders?")
        if not confirm:
            raise typer.Exit(0)

    from src.trader import run_trading_cycle

    def _cycle() -> None:
        try:
            run_trading_cycle(
                min_ev=min_ev,
                min_confidence=min_conf,
                max_hours=max_hours,
                dry_run=dry_run,
                shadow=shadow,
                top_n=top_n,
                bankroll=bankroll,
            )
        except Exception as e:
            logger.error(f"Trading cycle error: {e}", exc_info=True)

    if once:
        _cycle()
        return

    # Scheduled mode
    console.print(f"[green]Scheduling scan every {interval} minutes...[/green]")
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(_cycle, "interval", minutes=interval, next_run_time=datetime.utcnow())
    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped.[/yellow]")
        scheduler.shutdown()


@app.command()
def backtest(
    n_sims: int = typer.Option(1000, "--n-sims", help="Monte Carlo simulation count"),
    n_opps: int = typer.Option(500, "--n-opps", help="Synthetic opportunities to generate"),
    days: int = typer.Option(30, "--days", help="Simulated period in days"),
    bankroll: float = typer.Option(1000.0, "--bankroll"),
    edge: float = typer.Option(0.08, "--edge", help="Synthetic model edge above market price"),
    grid: bool = typer.Option(True, "--grid/--no-grid", help="Run grid-search param optimizer"),
    top_n: int = typer.Option(10, "--top-n", help="Top results to display"),
) -> None:
    """
    Run Monte Carlo backtests with optional grid-search hyperparameter optimization.
    Uses synthetic opportunities when real historical data isn't yet available.
    """
    setup_logging()
    _banner()

    from src.backtester import (
        display_backtest_results,
        display_mc_percentiles,
        generate_synthetic_opportunities,
        grid_search_params,
        monte_carlo_backtest,
    )

    console.print(f"[cyan]Generating {n_opps} synthetic opportunities over {days} days...[/cyan]")
    specs = generate_synthetic_opportunities(n=n_opps, edge_pct=edge, days=days)

    if grid:
        console.print(f"[cyan]Running grid search (this may take a minute)...[/cyan]")
        results = grid_search_params(specs, n_simulations=max(100, n_sims // 5), bankroll=bankroll)
        display_backtest_results(results, top_n=top_n)

        # Also run full MC on the best params
        best_params = results[0][1] if results else {}
        if best_params:
            console.print(f"[cyan]Full Monte Carlo on best params ({n_sims} sims)...[/cyan]")
            mean_m, all_m = monte_carlo_backtest(
                specs,
                n_simulations=n_sims,
                bankroll=bankroll,
                min_ev=best_params["min_ev"],
                kelly_mult=best_params["kelly_mult"],
                max_usdc_per_trade=best_params["max_usdc"],
            )
            display_mc_percentiles(all_m)
    else:
        console.print(f"[cyan]Running {n_sims} Monte Carlo simulations...[/cyan]")
        mean_m, all_m = monte_carlo_backtest(
            specs, n_simulations=n_sims, bankroll=bankroll
        )
        display_mc_percentiles(all_m)
        console.print(f"Mean: {mean_m}")


@app.command()
def show_trades(
    n: int = typer.Option(50, "--n", help="Number of recent trades to show"),
) -> None:
    """Show recent trade history from the database."""
    setup_logging()
    from rich.table import Table
    from src.utils import TradeStore
    from config.settings import TRADES_DB

    store = TradeStore(TRADES_DB)
    trades = store.recent_trades(n)

    if not trades:
        console.print("[yellow]No trades recorded yet.[/yellow]")
        return

    table = Table(header_style="bold magenta", border_style="dim")
    for col in ["id", "mode", "city", "bucket_label", "model_prob", "market_price", "ev", "size_usdc", "outcome", "pnl", "timestamp"]:
        table.add_column(col, no_wrap=True)

    for t in trades:
        if t.get("shadow"):
            mode_str = "[yellow]SHADOW[/yellow]"
        elif t.get("dry_run"):
            mode_str = "[blue]DRY[/blue]"
        else:
            mode_str = "[green]LIVE[/green]"

        outcome = t.get("outcome")
        outcome_str = (
            "[green]YES[/green]" if outcome == "yes"
            else "[red]NO[/red]" if outcome == "no"
            else "[dim]open[/dim]"
        )
        pnl = t.get("pnl")
        pnl_str = (
            f"[green]${pnl:.2f}[/green]" if pnl and pnl >= 0
            else f"[red]${pnl:.2f}[/red]" if pnl is not None
            else "-"
        )
        table.add_row(
            str(t.get("id", "")),
            mode_str,
            str(t.get("city", "")),
            str(t.get("bucket_label", "")),
            f"{t.get('model_prob', 0):.1%}",
            f"{t.get('market_price', 0):.1%}",
            f"{t.get('ev', 0):.1%}",
            f"${t.get('size_usdc', 0):.2f}",
            outcome_str,
            pnl_str,
            str(t.get("timestamp", ""))[:19],
        )

    console.print(table)


@app.command()
def resolve_shadow(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show status for each unresolved trade"),
) -> None:
    """
    Check Gamma API for resolution of open shadow trades and update P&L.

    Run this periodically (e.g. daily) to close out shadow positions as
    markets resolve. Each resolved trade gets an outcome (yes/no) and a
    computed P&L based on the ask price at entry.
    """
    setup_logging()
    _banner()
    from src.trader import resolve_shadow_trades

    resolved = resolve_shadow_trades(verbose=verbose)

    if not resolved:
        console.print("[yellow]No new resolutions found.[/yellow]")
        return

    console.print(f"\n[green]Resolved {len(resolved)} shadow trade(s):[/green]")
    for t in resolved:
        outcome = (t.get("outcome") or "?")
        side = (t.get("side") or "yes").lower()
        pnl = t.get("pnl", 0.0)
        # A win is when the side we BET matches the resolution — not just outcome=yes.
        won = (side == "yes" and outcome == "yes") or (side == "no" and outcome == "no")
        sign = "✅" if won else "❌"
        color = "green" if pnl >= 0 else "red"
        console.print(
            f"  {sign} #{t['id']} {t.get('city')} {t.get('bucket_label')} "
            f"[{side.upper()}] → {outcome.upper()} | [{color}]P&L ${pnl:.2f}[/{color}]"
        )


@app.command()
def shadow_pnl() -> None:
    """
    Display shadow mode performance: win rate, total P&L, per-city breakdown.

    This is your edge validation dashboard. Run it after resolve-shadow
    to see whether the model's EV predictions are materialising in practice.
    """
    setup_logging()
    _banner()
    from src.trader import shadow_performance_report
    shadow_performance_report()


@app.command()
def side_pnl(
    side: str = typer.Option("both", "--side", help="'yes', 'no', or 'both' (default)."),
    since_id: int = typer.Option(
        130, "--since", help="Only include trades with id > this. Default 130 = post-gate."
    ),
    all_history: bool = typer.Option(
        False, "--all", help="Include ALL trades (overrides --since)."
    ),
) -> None:
    """
    Per-side win rate, P&L, and edge-vs-breakeven for resolved shadow trades.

    Default scope is post-gate trades (id > 130). Use --all for the full history
    or --since N to choose a different cutoff. Shows the decisive metric: whether
    the 95% CI lower bound on win rate clears the avg ask price (= break-even).
    """
    setup_logging()
    _banner()

    import sqlite3
    import math
    from rich.table import Table
    from config.settings import TRADES_DB

    side = side.lower().strip()
    if side not in ("yes", "no", "both"):
        console.print("[red]--side must be 'yes', 'no', or 'both'[/red]")
        raise typer.Exit(code=1)

    where = "outcome IS NOT NULL"
    if not all_history:
        where += f" AND id > {int(since_id)}"
    sides = ("yes", "no") if side == "both" else (side,)

    def _row(s: str) -> dict:
        with sqlite3.connect(TRADES_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT outcome, market_price, pnl, size_usdc "
                f"FROM trades WHERE {where} AND side = ?",
                (s,),
            ).fetchall()
            pending = conn.execute(
                f"SELECT COUNT(*) FROM trades WHERE outcome IS NULL "
                f"{'AND id > ' + str(int(since_id)) if not all_history else ''} "
                f"AND side = ?",
                (s,),
            ).fetchone()[0]
        n = len(rows)
        if n == 0:
            return dict(side=s, n=0, pending=pending)
        won = sum(1 for r in rows if r["outcome"] == s)
        pnl = sum(r["pnl"] or 0 for r in rows)
        size = sum(r["size_usdc"] or 0 for r in rows)
        avg_ask = sum(r["market_price"] for r in rows) / n
        p = won / n
        se = math.sqrt(p * (1 - p) / n) if n > 1 else 0.0
        return dict(
            side=s, n=n, pending=pending, won=won, win_rate=p,
            ci_lo=max(0.0, p - 1.96 * se), ci_hi=min(1.0, p + 1.96 * se),
            avg_ask=avg_ask, gap=p - avg_ask,
            pnl=pnl, size=size, roi=pnl / size if size else 0.0,
        )

    scope = "ALL trades" if all_history else f"trades with id > {since_id} (post-gate)"
    console.print(f"\n[bold cyan]Per-side performance — {scope}[/bold cyan]\n")

    table = Table(header_style="bold magenta", border_style="dim")
    table.add_column("metric", no_wrap=True)
    if "yes" in sides:
        table.add_column("YES", justify="right")
    if "no" in sides:
        table.add_column("NO", justify="right")

    results = {s: _row(s) for s in sides}

    def cell(s: str, fmt) -> str:
        d = results[s]
        if d["n"] == 0:
            return "—"
        return fmt(d)

    def add(label: str, fmt) -> None:
        cells = [cell(s, fmt) for s in sides]
        table.add_row(label, *cells)

    add("resolved", lambda d: f"{d['n']}")
    add("pending",  lambda d: f"{d['pending']}")
    add("wins",     lambda d: f"{d['won']}")
    add("win rate", lambda d: f"{d['win_rate']:.1%}")
    add("  95% CI", lambda d: f"[{d['ci_lo']:.1%}, {d['ci_hi']:.1%}]")
    add("avg ask (break-even)", lambda d: f"{d['avg_ask']:.3f}")
    add("win vs break-even", lambda d: f"{d['gap']:+.1%}")
    add("cumulative P&L", lambda d: f"${d['pnl']:+,.2f}")
    add("deployed", lambda d: f"${d['size']:,.0f}")
    add("ROI", lambda d: f"{d['roi']:+.1%}")

    console.print(table)

    # Decisive interpretation line per side.
    for s in sides:
        d = results[s]
        if d["n"] == 0:
            console.print(f"  [{s.upper()}] no resolved trades in scope.")
            continue
        verdict = (
            "[green]edge confirmed at 95%[/green]" if d["ci_lo"] > d["avg_ask"]
            else "[red]no edge — CI lower bound below break-even[/red]" if d["ci_hi"] < d["avg_ask"]
            else "[yellow]inconclusive — break-even sits inside CI[/yellow]"
        )
        console.print(f"  [{s.upper()}] n={d['n']}: {verdict}")
    console.print()


@app.command()
def slice_dash(
    side: str = typer.Option("yes", "--side", help="'yes' (default) or 'no'."),
    since_id: int = typer.Option(
        130, "--since", help="Only include trades with id > this. Default 130 = post-gate."
    ),
    all_history: bool = typer.Option(
        False, "--all", help="Include ALL trades (overrides --since)."
    ),
) -> None:
    """
    Slice resolved shadow trades by ask range, bucket type, city, lead time, and
    model_prob band — to find which patterns actually carry the edge. Read-only:
    does NOT affect trading. Run anytime; useful for spotting where edge lives
    in your post-gate sample without running CI math in your head.
    """
    setup_logging()
    _banner()

    import sqlite3
    import math
    from rich.table import Table
    from config.settings import TRADES_DB

    side = side.lower().strip()
    if side not in ("yes", "no"):
        console.print("[red]--side must be 'yes' or 'no'[/red]")
        raise typer.Exit(code=1)

    where = "outcome IS NOT NULL AND side = ?"
    if not all_history:
        where += f" AND id > {int(since_id)}"

    with sqlite3.connect(TRADES_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, city, bucket_label, target_date, model_prob, market_price, "
            f"size_usdc, outcome, pnl, timestamp "
            f"FROM trades WHERE {where} ORDER BY id",
            (side,),
        ).fetchall()
        rows = [dict(r) for r in rows]

    if not rows:
        console.print(f"[yellow]No resolved {side.upper()} trades in scope.[/yellow]")
        return

    def _ask_bucket(p: float) -> str:
        if p < 0.20: return "A: <0.20 deep-longshot"
        if p < 0.40: return "B: 0.20-0.40 longshot"
        if p < 0.60: return "C: 0.40-0.60 middle"
        return "D: >=0.60 favorite"

    def _bucket_kind(label: str) -> str:
        l = (label or "").lower()
        if "higher" in l or "below" in l or "or more" in l or "or less" in l:
            return "open-ended"
        if "between" in l or "-" in l:
            return "range"
        return "single-degree"

    def _model_bin(p: float) -> str:
        if p < 0.60: return "0.55-0.60"
        if p < 0.70: return "0.60-0.70"
        if p < 0.80: return "0.70-0.80"
        if p < 0.90: return "0.80-0.90"
        return "0.90-1.00"

    def _lead_bucket(target: str, ts: str) -> str:
        try:
            from datetime import datetime
            t = datetime.fromisoformat(target)
            placed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hrs = (t - placed.replace(tzinfo=None)).total_seconds() / 3600
            if hrs < 12: return "<12h"
            if hrs < 24: return "12-24h"
            if hrs < 36: return "24-36h"
            return "36-48h"
        except Exception:
            return "?"

    def _aggregate(rows: list, key_fn) -> list[dict]:
        from collections import defaultdict
        agg = defaultdict(lambda: dict(n=0, won=0, pnl=0.0, size=0.0, ask=0.0))
        for r in rows:
            k = key_fn(r)
            a = agg[k]
            a["n"] += 1
            a["won"] += 1 if r["outcome"] == side else 0
            a["pnl"] += r["pnl"] or 0
            a["size"] += r["size_usdc"] or 0
            a["ask"] += r["market_price"]
        out = []
        for k, a in agg.items():
            n = a["n"]
            avg_ask = a["ask"] / n
            wr = a["won"] / n
            se = math.sqrt(wr * (1 - wr) / n) if n > 1 else 0
            out.append(dict(
                key=k, n=n, won=a["won"], wr=wr,
                ci_lo=max(0, wr - 1.96 * se), ci_hi=min(1, wr + 1.96 * se),
                pnl=a["pnl"], size=a["size"],
                roi=a["pnl"] / a["size"] if a["size"] else 0,
                avg_ask=avg_ask, gap=wr - avg_ask,
            ))
        return out

    def _render(title: str, agg: list[dict], sort_key: str = "key") -> None:
        agg = sorted(agg, key=lambda d: d[sort_key])
        t = Table(title=title, header_style="bold magenta", border_style="dim")
        for col in ("slice", "n", "won", "win%", "avg ask", "gap", "P&L", "ROI"):
            t.add_column(col, no_wrap=True)
        for d in agg:
            gap_color = "green" if d["gap"] > 0 else "red"
            roi_color = "green" if d["roi"] >= 0 else "red"
            t.add_row(
                str(d["key"]),
                str(d["n"]),
                str(d["won"]),
                f"{d['wr']:.0%}",
                f"{d['avg_ask']:.3f}",
                f"[{gap_color}]{d['gap']:+.1%}[/{gap_color}]",
                f"${d['pnl']:+,.0f}",
                f"[{roi_color}]{d['roi']:+.1%}[/{roi_color}]",
            )
        console.print(t)

    scope = "ALL trades" if all_history else f"id > {since_id} (post-gate)"
    console.print(f"\n[bold cyan]Slice dashboard — {side.upper()} side — {scope}  (n={len(rows)})[/bold cyan]\n")

    _render("By ask range", _aggregate(rows, lambda r: _ask_bucket(r["market_price"])))
    _render("By bucket type", _aggregate(rows, lambda r: _bucket_kind(r["bucket_label"])))
    _render("By model_prob band", _aggregate(rows, lambda r: _model_bin(r["model_prob"])))
    _render("By lead time", _aggregate(rows, lambda r: _lead_bucket(r["target_date"] or "", r["timestamp"] or "")))
    by_city = _aggregate(rows, lambda r: r["city"] or "?")
    _render("By city (top profit)", sorted(by_city, key=lambda d: -d["pnl"])[:8], "pnl")
    _render("By city (bottom)", sorted(by_city, key=lambda d: d["pnl"])[:5], "pnl")

    # Two-way: ask range × bucket type — where edge concentrates.
    twoway = _aggregate(rows, lambda r: f"{_ask_bucket(r['market_price']).split(':')[0]} × {_bucket_kind(r['bucket_label'])}")
    _render("By ask × bucket-type (where edge concentrates)", sorted(twoway, key=lambda d: -d["pnl"]))

    # Two-way: model_prob band × bucket type — isolates whether the mid-band
    # overconfidence bleed is concentrated in a single bucket shape (likely
    # open-ended tail mass) or spread across types (= a calibration-only issue).
    twoway2 = _aggregate(
        rows,
        lambda r: f"{_model_bin(r['model_prob'])} × {_bucket_kind(r['bucket_label'])}",
    )
    _render(
        "By model_prob × bucket-type (calibration pocket vs tail-mass)",
        sorted(twoway2, key=lambda d: -d["pnl"]),
    )

    console.print(
        "[dim]Reading guide: green 'gap' = won more than break-even; large green ROI on a "
        "slice with n>=10 is a candidate edge pattern. Treat n<5 slices as anecdote.[/dim]\n"
    )


@app.command()
def yes_score(
    since_id: int = typer.Option(
        130, "--since", help="Train on YES trades with id > this. Default 130 = post-gate."
    ),
    all_history: bool = typer.Option(
        False, "--all", help="Train on ALL YES history."
    ),
) -> None:
    """
    Prototype YES-quality score — does NOT deploy or affect trading.

    Computes per-feature win-rate lift over the base rate (which features predict
    a YES win?), fits a transparent additive log-odds score on top features, and
    reports leave-one-out cross-validated accuracy so you can judge how much of
    the apparent edge is real vs. overfit to the small sample.
    """
    setup_logging()
    _banner()

    import sqlite3
    import math
    from rich.table import Table
    from config.settings import TRADES_DB

    where = "outcome IS NOT NULL AND side = 'yes'"
    if not all_history:
        where += f" AND id > {int(since_id)}"

    with sqlite3.connect(TRADES_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, city, bucket_label, target_date, model_prob, market_price, "
            f"outcome, pnl, size_usdc, timestamp "
            f"FROM trades WHERE {where} ORDER BY id"
        ).fetchall()
        rows = [dict(r) for r in rows]

    n = len(rows)
    if n < 5:
        console.print(f"[yellow]Only {n} YES trades available — need ~5+ for any score. Wait.[/yellow]")
        return

    base_rate = sum(1 for r in rows if r["outcome"] == "yes") / n

    # Feature extractors (categorical, all available at trade time).
    def _ask_bucket(p: float) -> str:
        if p < 0.20: return "deep_longshot"
        if p < 0.40: return "longshot"
        if p < 0.60: return "middle"
        return "favorite"

    def _bucket_kind(label: str) -> str:
        l = (label or "").lower()
        if "higher" in l or "below" in l: return "open_ended"
        if "between" in l: return "range"
        return "single_degree"

    def _model_bin(p: float) -> str:
        if p < 0.65: return "low"
        if p < 0.80: return "mid"
        return "high"

    features = {
        "ask_range": lambda r: _ask_bucket(r["market_price"]),
        "bucket_type": lambda r: _bucket_kind(r["bucket_label"]),
        "model_prob": lambda r: _model_bin(r["model_prob"]),
    }

    # Per-feature-value lift table.
    console.print(f"\n[bold cyan]YES Quality Score — prototype[/bold cyan]")
    console.print(f"  training sample: n={n}  base win rate: {base_rate:.1%}\n")

    from collections import defaultdict

    log_odds: dict[tuple, float] = {}  # (feature_name, value) -> log-odds vs base
    for fname, fn in features.items():
        cnt = defaultdict(lambda: [0, 0])
        for r in rows:
            v = fn(r)
            won = 1 if r["outcome"] == "yes" else 0
            cnt[v][0] += won
            cnt[v][1] += 1

        t = Table(
            title=f"Feature: {fname}",
            header_style="bold magenta", border_style="dim",
        )
        for col in ("value", "n", "win%", "lift", "log-odds"):
            t.add_column(col, no_wrap=True)
        for v, (w, k) in sorted(cnt.items(), key=lambda x: -x[1][0] / x[1][1] if x[1][1] else 0):
            wr = (w + 1) / (k + 2)  # Laplace-smoothed to avoid 0/1 with tiny bins
            lift = wr - base_rate
            # log-odds ratio vs base
            eps = 1e-6
            lo = math.log(max(eps, wr / (1 - wr + eps)) / max(eps, base_rate / (1 - base_rate + eps)))
            log_odds[(fname, v)] = lo
            color = "green" if lift > 0 else "red"
            t.add_row(
                v, f"{k}", f"{w/k:.0%}",
                f"[{color}]{lift:+.1%}[/{color}]",
                f"{lo:+.2f}",
            )
        console.print(t)

    # Build a per-trade score by summing log-odds across features.
    def _score(r: dict) -> float:
        return sum(log_odds.get((fname, fn(r)), 0.0) for fname, fn in features.items())

    scored = sorted(rows, key=_score, reverse=True)
    half = max(1, len(scored) // 2)
    top, bot = scored[:half], scored[half:]

    def _stats(group: list[dict]) -> dict:
        if not group: return {}
        n = len(group)
        won = sum(1 for r in group if r["outcome"] == "yes")
        pnl = sum(r["pnl"] or 0 for r in group)
        size = sum(r["size_usdc"] or 0 for r in group)
        return dict(n=n, won=won, wr=won/n, pnl=pnl, roi=pnl/size if size else 0)

    s_top, s_bot = _stats(top), _stats(bot)
    console.print(f"[bold]Score split (in-sample, will be optimistic)[/bold]")
    console.print(f"  top half  : n={s_top['n']:<3} win {s_top['wr']:.0%}  P&L ${s_top['pnl']:+,.2f}  ROI {s_top['roi']:+.1%}")
    console.print(f"  bottom    : n={s_bot['n']:<3} win {s_bot['wr']:.0%}  P&L ${s_bot['pnl']:+,.2f}  ROI {s_bot['roi']:+.1%}")
    console.print()

    # Leave-one-out cross-validation — honest accuracy estimate.
    correct = 0
    loo_top_won, loo_top_n = 0, 0
    for i in range(n):
        train = rows[:i] + rows[i+1:]
        # Refit log-odds on train.
        loo_logodds: dict[tuple, float] = {}
        base = sum(1 for r in train if r["outcome"] == "yes") / len(train)
        for fname, fn in features.items():
            cnt = defaultdict(lambda: [0, 0])
            for r in train:
                v = fn(r); won = 1 if r["outcome"] == "yes" else 0
                cnt[v][0] += won; cnt[v][1] += 1
            for v, (w, k) in cnt.items():
                wr = (w + 1) / (k + 2)
                eps = 1e-6
                loo_logodds[(fname, v)] = math.log(
                    max(eps, wr / (1 - wr + eps)) / max(eps, base / (1 - base + eps))
                )
        # Score held-out
        test = rows[i]
        score = sum(loo_logodds.get((fn_n, fn(test)), 0.0) for fn_n, fn in features.items())
        predict_win = 1 if score > 0 else 0
        actual = 1 if test["outcome"] == "yes" else 0
        if predict_win == actual:
            correct += 1
        if score > 0:
            loo_top_n += 1
            if actual: loo_top_won += 1

    console.print(f"[bold]Leave-one-out CV (honest)[/bold]")
    console.print(f"  classification accuracy : {correct/n:.0%}  (base rate would be ~{max(base_rate, 1-base_rate):.0%})")
    if loo_top_n:
        console.print(f"  score>0 cohort          : n={loo_top_n}/{n}  win {loo_top_won/loo_top_n:.0%}")
    console.print()
    console.print(
        "[dim]Caveat: with n=20-30 the LOO estimate is itself noisy. Treat as 'is the "
        "direction promising', not 'is the score deploy-ready'. Re-run after each new "
        "10-20 YES resolves to see if accuracy stabilises above the base rate.[/dim]\n"
    )


@app.command()
def contrarian_pnl(
    since_id: int = typer.Option(
        0, "--since", help="Only include trades with id > this. Default 0 = all rows since the flag went live."
    ),
) -> None:
    """
    Validate the contrarian-YES-inversion strategy (Option F) against natural NO bets.

    Splits resolved shadow trades into three buckets and compares performance:
      • contrarian=1     — YES picks that were flipped to NO at the real NO ask
      • natural NO       — NO picks the strategy made on its own
      • natural YES      — YES picks (pre-flag, or if the flag is off in current state)

    For the contrarian strategy to be validated forward, three things should hold:
      1. contrarian win rate clearly above its avg break-even (positive 'gap')
      2. positive ROI across multiple weekly cohorts, not just total
      3. contrarian ROI noticeably better than the natural-YES baseline
    """
    setup_logging()
    _banner()

    import sqlite3
    import math
    from datetime import datetime
    from collections import defaultdict
    from rich.table import Table
    from config.settings import TRADES_DB

    with sqlite3.connect(TRADES_DB) as conn:
        conn.row_factory = sqlite3.Row
        # Make sure the column exists (auto-migrated on TradeStore init).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        if "contrarian" not in cols:
            console.print(
                "[red]No 'contrarian' column in trades table — restart the trade service "
                "once to trigger the schema migration, then re-run this command.[/red]"
            )
            raise typer.Exit(code=1)

        rows = conn.execute(
            f"""SELECT id, side, contrarian, market_price, model_prob, size_usdc,
                       outcome, pnl, timestamp
                FROM trades
                WHERE outcome IS NOT NULL AND id > {int(since_id)}
                ORDER BY id"""
        ).fetchall()
        rows = [dict(r) for r in rows]

    if not rows:
        console.print(f"[yellow]No resolved trades with id > {since_id}.[/yellow]")
        return

    def _bucket(r: dict) -> str:
        if r.get("contrarian"):
            return "contrarian (YES→NO)"
        return "natural NO" if r["side"] == "no" else "natural YES"

    def _summarise(subset: list[dict]) -> dict:
        n = len(subset)
        if n == 0:
            return dict(n=0, wins=0, win_pct=0.0, avg_ask=0.0, gap=0.0,
                        pnl=0.0, deployed=0.0, roi=0.0, ci_lo=0.0, ci_hi=0.0)
        wins = sum(1 for r in subset if r["outcome"] == r["side"])
        ask = sum(r["market_price"] for r in subset) / n
        wr = wins / n
        se = math.sqrt(wr * (1 - wr) / n) if n > 1 else 0
        pnl = sum(r["pnl"] or 0 for r in subset)
        dep = sum(r["size_usdc"] or 0 for r in subset)
        return dict(
            n=n, wins=wins, win_pct=wr * 100, avg_ask=ask, gap=(wr - ask) * 100,
            pnl=pnl, deployed=dep, roi=(pnl / dep * 100) if dep else 0.0,
            ci_lo=max(0, wr - 1.96 * se) * 100, ci_hi=min(1, wr + 1.96 * se) * 100,
        )

    groups = defaultdict(list)
    for r in rows:
        groups[_bucket(r)].append(r)

    contrarian = _summarise(groups.get("contrarian (YES→NO)", []))
    natural_no = _summarise(groups.get("natural NO", []))
    natural_yes = _summarise(groups.get("natural YES", []))

    scope = "ALL resolved trades" if since_id == 0 else f"id > {since_id}"
    console.print(f"\n[bold cyan]Contrarian P&L review — {scope}[/bold cyan]\n")

    if contrarian["n"] == 0:
        console.print(
            "[yellow]No resolved contrarian trades yet.[/yellow]\n"
            "[dim]Either the flag was just turned on (give the resolve cron time), or "
            "no YES picks have triggered since enabling. Check `grep CONTRARIAN logs/bot_*.log`.[/dim]\n"
        )

    table = Table(title="Strategy comparison", header_style="bold magenta", border_style="dim")
    for col in ("metric", "contrarian (YES→NO)", "natural NO", "natural YES (baseline)"):
        table.add_column(col, no_wrap=True)
    rows_render = [
        ("resolved",      contrarian["n"],        natural_no["n"],        natural_yes["n"]),
        ("wins",          contrarian["wins"],     natural_no["wins"],     natural_yes["wins"]),
        ("win rate",      f'{contrarian["win_pct"]:.1f}%', f'{natural_no["win_pct"]:.1f}%', f'{natural_yes["win_pct"]:.1f}%'),
        ("  95% CI",      f'[{contrarian["ci_lo"]:.1f}%, {contrarian["ci_hi"]:.1f}%]',
                          f'[{natural_no["ci_lo"]:.1f}%, {natural_no["ci_hi"]:.1f}%]',
                          f'[{natural_yes["ci_lo"]:.1f}%, {natural_yes["ci_hi"]:.1f}%]'),
        ("avg ask (BE)",  f'{contrarian["avg_ask"]:.3f}', f'{natural_no["avg_ask"]:.3f}', f'{natural_yes["avg_ask"]:.3f}'),
        ("win vs BE",     f'{contrarian["gap"]:+.1f}%',   f'{natural_no["gap"]:+.1f}%',   f'{natural_yes["gap"]:+.1f}%'),
        ("P&L",           f'${contrarian["pnl"]:+,.2f}',  f'${natural_no["pnl"]:+,.2f}',  f'${natural_yes["pnl"]:+,.2f}'),
        ("deployed",      f'${contrarian["deployed"]:,.0f}', f'${natural_no["deployed"]:,.0f}', f'${natural_yes["deployed"]:,.0f}'),
        ("ROI",           f'{contrarian["roi"]:+.1f}%',   f'{natural_no["roi"]:+.1f}%',   f'{natural_yes["roi"]:+.1f}%'),
    ]
    for r in rows_render:
        table.add_row(*[str(c) for c in r])
    console.print(table)

    # Verdict line on contrarian alone — the critical "edge or no edge" call.
    if contrarian["n"] >= 10:
        if contrarian["ci_lo"] > contrarian["avg_ask"] * 100:
            verdict = "🟢 EDGE CONFIRMED — CI lower bound clears break-even"
        elif contrarian["ci_hi"] < contrarian["avg_ask"] * 100:
            verdict = "🔴 EDGE REJECTED — CI upper bound below break-even"
        else:
            verdict = "🟡 inconclusive — break-even sits inside CI; need more samples"
        console.print(f"  [contrarian] n={contrarian['n']}: {verdict}\n")
    elif contrarian["n"] > 0:
        console.print(f"  [contrarian] n={contrarian['n']}: too few to judge — need ≥10 resolved\n")

    # Cohort robustness: weekly split of contrarian P&L.
    if contrarian["n"] >= 5:
        weekly = defaultdict(list)
        for r in groups["contrarian (YES→NO)"]:
            try:
                w = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).strftime("%Y-W%U")
            except Exception:
                w = "?"
            weekly[w].append(r)
        ctab = Table(title="Contrarian — weekly cohort split (positive in every week = robust edge)",
                     header_style="bold magenta", border_style="dim")
        for col in ("week", "n", "wins", "win%", "avg ask", "gap", "P&L", "ROI"):
            ctab.add_column(col, no_wrap=True)
        for w in sorted(weekly):
            s = _summarise(weekly[w])
            gap_color = "green" if s["gap"] > 0 else "red"
            roi_color = "green" if s["roi"] > 0 else "red"
            ctab.add_row(
                w, str(s["n"]), str(s["wins"]),
                f'{s["win_pct"]:.1f}%', f'{s["avg_ask"]:.3f}',
                f'[{gap_color}]{s["gap"]:+.1f}%[/{gap_color}]',
                f'${s["pnl"]:+.2f}',
                f'[{roi_color}]{s["roi"]:+.1f}%[/{roi_color}]',
            )
        console.print(ctab)
        positive_weeks = sum(1 for w in weekly if _summarise(weekly[w])["roi"] > 0)
        console.print(
            f"  [dim]positive cohorts: {positive_weeks}/{len(weekly)}. "
            f"All-weeks-positive is the robust-edge signal; mixed weeks = wait/variance.[/dim]\n"
        )


if __name__ == "__main__":
    app()
