"""
One-off purge of the two phantom live trades (orders that failed on-chain but
were recorded as filled before the failed-order guard existed).

  id 1616  2026-06-29  Chengdu   '30°C or higher' YES — FOK killed, never filled
  id 1636  2026-07-04  Guangzhou '32°C'           NO  — insufficient balance, never placed

Both later "resolved" as wins (+$2.65 and +$1.01) that never happened,
overstating live P&L by $3.66. This script matches each row on id AND its
attributes (never id alone), prints what it found, and deletes only exact
matches. Idempotent: safe to re-run.

Usage:
    python scripts/purge_phantom_trades.py            # dry-run: show matches
    python scripts/purge_phantom_trades.py --execute  # actually delete
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trades.db"

# (id, city, target_date, bucket_label, side) — all must match.
PHANTOMS = [
    (1616, "Chengdu", "2026-06-30", "30°C or higher", "yes"),
    (1636, "Guangzhou", "2026-07-04", "32°C", "no"),
]

WHERE = "id=? AND city=? AND target_date=? AND bucket_label=? AND side=? AND shadow=0"


def main() -> None:
    execute = "--execute" in sys.argv
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        found = []
        for phantom in PHANTOMS:
            row = conn.execute(
                f"SELECT id, timestamp, city, bucket_label, side, size_usdc, outcome, pnl "
                f"FROM trades WHERE {WHERE}", phantom,
            ).fetchone()
            if row:
                found.append(phantom)
                print(f"MATCH  #{row['id']} {row['timestamp'][:16]} {row['city']} "
                      f"{row['bucket_label']} {row['side'].upper()} "
                      f"size=${row['size_usdc']:.2f} outcome={row['outcome']} pnl={row['pnl']}")
            else:
                print(f"absent #{phantom[0]} {phantom[1]} {phantom[3]} — already purged or id mismatch")

        if not found:
            print("Nothing to delete.")
            return
        if not execute:
            print(f"\nDry-run: {len(found)} row(s) would be deleted. Re-run with --execute.")
            return

        for phantom in found:
            conn.execute(f"DELETE FROM trades WHERE {WHERE}", phantom)
        conn.commit()
        print(f"\nDeleted {len(found)} phantom row(s).")


if __name__ == "__main__":
    main()
