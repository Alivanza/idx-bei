"""
One-time historical load -> posts full-universe daily OHLCV into the Google Sheet's
"Daily All Tickers" tab, replacing its previous content (a Screen_Log self-join formula
that only ever showed a ticker on days it happened to get shortlisted -- blank for the vast
majority of ticker/date cells by construction, not by failure; see the investigation that
led here).

READ THIS FIRST -- what this is and isn't:
  - Source data is stock_summary_full_history.parquet (shipped alongside this script), not
    your repo's live data/parquet/stock_summary.parquet. The live file currently only
    covers 2026-08-13 -> 2026-09-11 (~1 month) -- the copy here was recovered from an
    earlier commit (8d499e9) in your repo's own git history and covers 2025-03-11 ->
    2026-09-11 (339,408 rows, 980 tickers, 354 trading days). Verified: every row in the
    2026-08-13 -> 2026-09-11 overlap between the two matches exactly (19,260/19,260 rows,
    zero differing Close values) -- so this file is a strict superset of what's live now,
    not a conflicting alternate version.
  - I don't know for certain what produced that older commit's data (it predates the
    day-by-day ingestion pipeline currently in this repo -- the partitioned
    data/timeseries/stock_summary/ store only ever had 20 date-partitions, all from
    2026-08-13 onward, so this deeper history was never built one day at a time the way
    everything since has been). Treat the historical-fact/provenance distinction
    accordingly: the row-for-row match against verified-live data for the last month is
    solid evidence it's the same underlying feed, not proof of how the earlier stretch was
    produced.
  - This script only touches the "Daily All Tickers" tab. It does not affect Screen_Log,
    Shortlist, or anything else -- those are untouched.

Usage:
    uv run python backfill_daily_prices.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--dry-run] [--batch-size N]

    --start / --end   optional; restrict to a sub-range of what's in the parquet file.
                       Defaults to the earliest/latest date on file.
    --dry-run         compute and print row counts, but don't POST anything.
    --batch-size      trading days per POST (default 5). ~950 tickers/day, so 5 days is
                       ~4,750 rows/chunk -- keeps each Apps Script call comfortably under
                       its execution-time cap without needing hundreds of tiny requests.

This is resumable, not idempotent-by-overwrite like backfill_screen_log.py: the Apps Script
side (appendDailyPricesInit) does NOT scan for or delete pre-existing rows before appending,
because doing that against what will become a 339,000+ row sheet on every one of dozens of
chunk-POSTs would be slow for no benefit in the normal case (nothing to collide with,
because each date is only ever loaded once). Instead, THIS script tracks progress in
.backfill_daily_prices_checkpoint (same folder) -- every date it has successfully POSTed is
recorded there, and a re-run skips dates already checkpointed rather than re-sending them
(which would otherwise duplicate rows, since the Apps Script side won't dedupe them for
you). Delete that checkpoint file if you deliberately want to reload everything from
scratch -- but clear the "Daily All Tickers" tab by hand first, or you'll get duplicates.
"""
import argparse
import json
import os
import sys

import pandas as pd

import daily_screen as ds

HERE = os.path.dirname(os.path.abspath(__file__))
PARQUET_PATH = os.path.join(HERE, "stock_summary_full_history.parquet")
CHECKPOINT_PATH = os.path.join(HERE, ".backfill_daily_prices_checkpoint")


def load_checkpoint():
    if not os.path.exists(CHECKPOINT_PATH):
        return set()
    with open(CHECKPOINT_PATH) as f:
        return set(line.strip() for line in f if line.strip())


def append_checkpoint(dates):
    with open(CHECKPOINT_PATH, "a") as f:
        for d in dates:
            f.write(d + "\n")


def post_bulk(cfg, rows):
    """POST one chunk of [Ticker, Name, Date, Open, High, Low, Close, Volume] rows.
    Returns (status, body_text) or raises. Mirrors backfill_screen_log.py's post_bulk()."""
    import urllib.request
    bulk = {"secret": cfg.get("shared_secret", ""), "mode": "prices_init", "rows": rows}
    data = json.dumps(bulk).encode("utf-8")
    req = urllib.request.Request(cfg["webapp_url"], data=data, headers={"Content-Type": "application/json"})
    # 420s: see backfill_screen_log.py's post_bulk() for why -- safely above Apps Script's
    # own ~360s (6 min) execution cap.
    with urllib.request.urlopen(req, timeout=420) as resp:
        return resp.status, resp.read().decode("utf-8")


def rows_for_dates(df, dates):
    """Builds compact [Ticker, Name, Date, Open, High, Low, Close, Volume] rows for the
    given dates, sorted by Date then Ticker (deterministic, easy to spot-check)."""
    sub = df[df["Date"].isin(dates)].sort_values(["Date", "StockCode"])
    out = []
    for r in sub.itertuples(index=False):
        out.append([
            r.StockCode,
            r.StockName,
            pd.Timestamp(r.Date).date().isoformat(),
            None if pd.isna(r.OpenPrice) else float(r.OpenPrice),
            None if pd.isna(r.High) else float(r.High),
            None if pd.isna(r.Low) else float(r.Low),
            None if pd.isna(r.Close) else float(r.Close),
            None if pd.isna(r.Volume) else float(r.Volume),
        ])
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default=None, help="YYYY-MM-DD, inclusive (default: earliest on file)")
    p.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive (default: latest on file)")
    p.add_argument("--dry-run", action="store_true", help="Compute and print, but don't POST")
    p.add_argument("--batch-size", type=int, default=5, help="Trading days per POST (default 5)")
    args = p.parse_args()

    if not os.path.exists(PARQUET_PATH):
        print(f"ERROR: {PARQUET_PATH} not found. It should ship alongside this script -- "
              f"if it's missing, the historical data isn't available to load.")
        sys.exit(1)

    cfg = ds.load_config()
    df = pd.read_parquet(PARQUET_PATH)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date if not pd.api.types.is_datetime64_any_dtype(df["Date"]) else df["Date"]

    all_dates = sorted(pd.to_datetime(df["Date"].unique()))
    if args.start:
        start_ts = pd.Timestamp(args.start)
        all_dates = [d for d in all_dates if d >= start_ts]
    if args.end:
        end_ts = pd.Timestamp(args.end)
        all_dates = [d for d in all_dates if d <= end_ts]

    done = load_checkpoint()
    all_date_strs = [pd.Timestamp(d).date().isoformat() for d in all_dates]
    pending = [d for d in all_date_strs if d not in done]

    print(f"{len(all_date_strs)} trading day(s) in range "
          f"({all_date_strs[0] if all_date_strs else 'n/a'} -> {all_date_strs[-1] if all_date_strs else 'n/a'}). "
          f"{len(done & set(all_date_strs))} already checkpointed, {len(pending)} pending.")

    if not pending:
        print("Nothing to do.")
        return

    batch_size = max(1, args.batch_size)
    n_batches = (len(pending) + batch_size - 1) // batch_size
    print(f"Processing in {n_batches} batch(es) of up to {batch_size} trading day(s) each.\n")

    for i in range(0, len(pending), batch_size):
        batch_dates = pending[i:i + batch_size]
        batch_ts = [pd.Timestamp(d) for d in batch_dates]
        rows = rows_for_dates(df, batch_ts)
        batch_num = i // batch_size + 1

        if args.dry_run:
            print(f"[batch {batch_num}/{n_batches}] {batch_dates[0]} -> {batch_dates[-1]}: "
                  f"{len(rows)} row(s) (dry-run, not posted)")
            continue

        print(f"[batch {batch_num}/{n_batches}] {batch_dates[0]} -> {batch_dates[-1]}: "
              f"posting {len(rows)} row(s)...", end=" ", flush=True)
        try:
            status, body = post_bulk(cfg, rows)
            print(f"HTTP {status}: {body.strip()}")
            append_checkpoint(batch_dates)
        except Exception as e:
            print(f"FAILED: {repr(e)}")
            print(f"Stopping here -- {len(pending) - i} date(s) still pending. "
                  f"Re-run this script to resume from this point (already-checkpointed "
                  f"batches will be skipped).")
            sys.exit(1)

    if not args.dry_run:
        print(f"\nDone. {len(pending)} trading day(s) posted this run.")


if __name__ == "__main__":
    main()
