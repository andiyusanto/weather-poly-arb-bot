#!/usr/bin/env python3
"""
Convenience launcher. Equivalent to: python -m src.main <args>

Usage:
    python run.py scan
    python run.py scan --min-ev 0.15 --max-hours 24
    python run.py trade --dry-run
    python run.py trade --live --bankroll 2000
    python run.py backtest --n-sims 2000
    python run.py show-trades
"""

import sys
from pathlib import Path

# Ensure project root is on PYTHONPATH regardless of working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.main import app

if __name__ == "__main__":
    app()
