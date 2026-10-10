"""
One-off: extend stock_summary_full_history.parquet with newly-ingested trading days from
the live data/parquet/stock_summary.parquet, so backfill_daily_prices.py's existing,
already-tested machinery (checkpointing, batched POSTs, "OK "-prefix success checking) can
post them into "Daily All Tickers" -- rather than writing a second, parallel posting path.

Why this is needed: stock_summary_full_history.parquet is a static snapshot (see its own
module's docstring in backfill_daily_prices.py) covering 2025-03-11 -> 2026-09-11. It does
NOT grow on its own as new days get ingested. This script is the bridge: run it after
`uv run idx backfill --start ... --end ... --type stock` + `uv run idx parquet` have pulled
new days into the live parquet, and before re-running backfill_daily_prices.py.

Only appends rows strictly after the full-history file's current max date -- so re-running
this script is harmless (it'll just find nothing new to add on a second run with no new
ingestion in between).

Usage:
    uv run python merge_recent_prices_into_full_history.py [--dry-run]
"""
import argparse
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
FULL_PATH = os.path.join(HERE, "stock_summary_full_history.parquet")
LIVE_PATH = os.path.join(HERE, "..", "data", "parquet", "stock_summary.parquet")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Report what would be merged, but don't write")
    args = p.parse_args()

    if not os.path.exists(FULL_PATH):
        print(f"ERROR: {FULL_PATH} not found.")
        return 1
    if not os.path.exists(LIVE_PATH):
        print(f"ERROR: {LIVE_PATH} not found. Run `uv run idx parquet` first.")
        return 1

    full = pd.read_parquet(FULL_PATH)
    live = pd.read_parquet(LIVE_PATH)

    full["Date"] = pd.to_datetime(full["Date"])
    live["Date"] = pd.to_datetime(live["Date"])

    cutoff = full["Date"].max()
    new_rows = live[live["Date"] > cutoff].copy()

    print(f"stock_summary_full_history.parquet currently covers through {cutoff.date()} "
          f"({len(full)} rows).")
    print(f"Live parquet has {len(new_rows)} row(s) after that cutoff, across "
          f"{new_rows['Date'].nunique()} trading day(s)"
          + (f": {sorted(d.date().isoformat() for d in new_rows['Date'].unique())}"
             if len(new_rows) else "."))

    if new_rows.empty:
        print("Nothing new to merge.")
        return 0

    if args.dry_run:
        print("--dry-run: not writing.")
        return 0

    merged = pd.concat([full, new_rows], ignore_index=True)
    merged = merged.sort_values(["Date", "StockCode"]).reset_index(drop=True)
    merged.to_parquet(FULL_PATH, index=False)
    print(f"Merged. stock_summary_full_history.parquet now covers through "
          f"{merged['Date'].max().date()} ({len(merged)} total rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
