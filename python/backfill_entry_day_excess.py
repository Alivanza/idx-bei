"""
One-off back-fill of Screen_Log's EntryDayExcess column (column O) for rows written before the
column existed.

  EntryDayExcess = (Close[D] / Close[D-1] - 1) - (^JKSE return on D)      [a fraction: 0.0375 = +3.75pp]

It computes that for EVERY ticker on every trading day on file and POSTs it, in batches of dates, to
the Apps Script web app (mode "screenlog_ede"). The web app matches each existing Screen_Log row on
(LastTradingDate, Ticker) and writes ONLY that row's EntryDayExcess cell. It never appends, deletes or
re-sorts rows, never touches any other column, and leaves a row blank when no value exists for it.

Why not re-run backfill_screen_log.py instead: that replays today's screen logic over history. Today's
corporate_actions / financial_ratios snapshots differ from the ones the existing rows were built with,
so a re-run could change which names were shortlisted on a past date and would re-tag Live rows as
Backfill. This script cannot change membership: it only adds a number to rows that already exist.

Order of operations: deploy the updated AppsScript_Code.gs first (new web-app version), THEN run this.
Safe to re-run: the same values are written over the same cells.

Usage (from python/):
    uv run python backfill_entry_day_excess.py [--batch-size N] [--dry-run]
"""
import argparse
import json
import sys
import urllib.request

import pandas as pd

import daily_screen as ds


def build_ede_by_date():
    """{ 'YYYY-MM-DD': {ticker: ede} } for every date with a ^JKSE return and a prior close on file."""
    ss = pd.read_parquet(ds.PARQUET_DIR + "/stock_summary.parquet", columns=["Date", "StockCode", "Close"])
    ss = ss.sort_values(["StockCode", "Date"])
    ss["prev"] = ss.groupby("StockCode")["Close"].shift(1)
    ss["ret"] = ss["Close"] / ss["prev"] - 1.0
    bench = ds.load_benchmark_returns()
    ss["bench"] = ss["Date"].map(lambda d: bench.get(pd.Timestamp(d)))
    ss["ede"] = ss["ret"] - ss["bench"].astype(float)
    ss = ss[ss["ede"].notna() & ss["Close"].gt(0) & ss["prev"].gt(0)]
    out = {}
    for d, g in ss.groupby("Date"):
        out[pd.Timestamp(d).date().isoformat()] = {
            t: round(float(v), 4) for t, v in zip(g["StockCode"], g["ede"])
        }
    return out


def post(cfg, ede_batch):
    body = {"secret": cfg.get("shared_secret", ""), "mode": "screenlog_ede", "ede": ede_batch}
    req = urllib.request.Request(cfg["webapp_url"], data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=330) as resp:
        return resp.status, resp.read().decode("utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch-size", type=int, default=40, help="dates per POST (default 40)")
    p.add_argument("--dry-run", action="store_true", help="compute and report, post nothing")
    args = p.parse_args()

    cfg = ds.load_config()
    ede = build_ede_by_date()
    dates = sorted(ede)
    n_vals = sum(len(v) for v in ede.values())
    print(f"EntryDayExcess computed for {len(dates)} date(s) ({dates[0]} -> {dates[-1]}), {n_vals:,} ticker-day values.")
    if args.dry_run:
        print("--dry-run: nothing posted.")
        return
    n_batches = (len(dates) + args.batch_size - 1) // args.batch_size
    for b in range(n_batches):
        chunk = dates[b * args.batch_size:(b + 1) * args.batch_size]
        try:
            status, text = post(cfg, {d: ede[d] for d in chunk})
        except Exception as e:
            print(f"batch {b + 1}/{n_batches} ({chunk[0]} -> {chunk[-1]}) FAILED: {e!r}. Safe to re-run.")
            sys.exit(1)
        print(f"batch {b + 1}/{n_batches} ({chunk[0]} -> {chunk[-1]}): HTTP {status} {text.strip()}")


if __name__ == "__main__":
    main()
