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
    once: bool = typer.Option(False, "--once", help="Run one cycle then exit (default: run on schedule)"),
    interval: int = typer.Option(settings.scan_interval_minutes, "--interval", help="Scan interval (minutes)"),
) -> None:
    """
    Auto-execute trades for high-EV opportunities.

    ⚠️  WARNING: This command places REAL ORDERS if --live is passed.
    Make sure you have tested with --dry-run first.
    """
    setup_logging()
    _banner()

    if not dry_run and not settings.has_polymarket_key:
        console.print("[bold red]ERROR: POLYMARKET_PRIVATE_KEY not set. Cannot trade live.[/bold red]")
        raise typer.Exit(1)

    if not dry_run:
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
    for col in ["id", "city", "bucket_label", "model_prob", "market_price", "ev", "size_usdc", "dry_run", "outcome", "pnl", "timestamp"]:
        table.add_column(col, no_wrap=True)

    for t in trades:
        table.add_row(
            str(t.get("id", "")),
            str(t.get("city", "")),
            str(t.get("bucket_label", "")),
            f"{t.get('model_prob', 0):.1%}",
            f"{t.get('market_price', 0):.1%}",
            f"{t.get('ev', 0):.1%}",
            f"${t.get('size_usdc', 0):.2f}",
            "YES" if t.get("dry_run") else "LIVE",
            str(t.get("outcome") or "open"),
            f"${t.get('pnl', 0) or 0:.2f}" if t.get("pnl") is not None else "-",
            str(t.get("timestamp", ""))[:19],
        )

    console.print(table)


if __name__ == "__main__":
    app()
