"""
One-time historical pull -> replays the CURRENT screen logic over past trading days and
bulk-appends the results into the Google Sheet's Screen_Log tab, so you have data to
backtest against instead of waiting for Screen_Log to accumulate live, one day at a time.

READ THIS FIRST -- what this is and isn't:
  - It re-runs run_screen() with the price-data window truncated to each historical date,
    so F1-F4 (liquidity, price range, ATR/breakout, zero-trade) only see what would have
    been available on that date. That part is a faithful replay.
  - F5 (pending corporate actions) and Sector are checked against TODAY's snapshot of
    corporate_actions.parquet / financial_ratios.parquet, because no historical snapshot
    of either exists. That is real hindsight bias on those two dimensions specifically.
    Every backfilled row is tagged Source="Backfill" (vs "Live" for the real daily run)
    and carries a caveat saying so, precisely so you can tell the two apart later and
    weight backtest conclusions accordingly.
  - It needs at least WINDOW_ADTV_HIGH (20) trading sessions of price history on or before
    a date to compute a valid ADTV20/High20 for that date -- dates before that in your
    stock_summary.parquet are skipped outright (not computed with a truncated, biased
    window). Run `uv run idx status` to see how many trading days you actually have before
    running this, or just let this script report what it skipped.

Usage:
    uv run python backfill_screen_log.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--dry-run] [--batch-size N]

    --start / --end   optional; restrict to a sub-range of what's in stock_summary.parquet.
                       Defaults to the earliest/latest date on file.
    --dry-run         compute and print what would be sent, but don't POST it.
    --batch-size      how many days per POST (default 30). See "Why batched" below --
                       don't set this to your full day count, that's the thing it protects against.

This is idempotent: Screen_Log's dedupe-by-LastTradingDate (in AppsScript_Code.gs) means
re-running this script just replaces the same historical rows rather than duplicating them,
so it's safe to re-run after a deeper `idx backfill` extends your history further back, or
to resume after an interrupted run (already-posted days just get overwritten again).

Why batched, not one giant POST: Apps Script's doPost has a hard execution-time cap (6 min
on a consumer Google account, 30 min on Workspace). Each day in the "backfill" mode loop
does a full-column scan of Screen_Log plus several row writes, so a several-hundred-day
single request can walk right into that cap and die partway through with no clean signal
about which days made it in. Posting in batches of --batch-size keeps each Apps Script
invocation short and gives you incremental progress; if one batch's HTTP call fails or times
out, just rerun the script (or narrow --start/--end to the remaining range) -- already-landed
batches are overwritten harmlessly by the LastTradingDate dedupe, not duplicated.

Note on runtime: computing each day's payload re-reads stock_summary.parquet (and the sector/
corporate-actions files) from disk and re-runs the full per-ticker screen from scratch --
there's no caching across days. For a few hundred days this can take several minutes total;
that's expected, not a hang.
"""
import argparse
import json
import sys
import time

import pandas as pd

import daily_screen as ds


def post_bulk(cfg, runs):
    """POST one batch of day-payloads. Returns (status, body_text) or raises."""
    import urllib.request
    import urllib.error
    bulk = {"secret": cfg.get("shared_secret", ""), "mode": "backfill", "runs": runs}
    data = json.dumps(bulk).encode("utf-8")
    req = urllib.request.Request(cfg["webapp_url"], data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status, resp.read().decode("utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default=None, help="YYYY-MM-DD, inclusive (default: earliest on file)")
    p.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive (default: latest on file)")
    p.add_argument("--dry-run", action="store_true", help="Compute and print, but don't POST to the Sheet")
    p.add_argument("--batch-size", type=int, default=30,
                    help="Days per POST (default 30) -- keeps each Apps Script doPost call well "
                         "under its execution-time limit. See module docstring.")
    args = p.parse_args()

    cfg = ds.load_config()

    ss = pd.read_parquet(ds.PARQUET_DIR + "/stock_summary.parquet")
    all_dates = sorted(ss["Date"].unique())
    if len(all_dates) < ds.WINDOW_ADTV_HIGH:
        print(f"ERROR: only {len(all_dates)} trading days on file; need at least "
              f"{ds.WINDOW_ADTV_HIGH} for even one valid backtest day. Backfill more history "
              f"first: uv run idx backfill --start <earlier> --end <today> --type stock")
        sys.exit(1)

    # A date needs WINDOW_ADTV_HIGH-1 prior sessions ON FILE plus itself -- i.e. it must be
    # at index >= WINDOW_ADTV_HIGH-1 in the sorted unique-date list.
    valid_dates = all_dates[ds.WINDOW_ADTV_HIGH - 1:]
    skipped = len(all_dates) - len(valid_dates)

    if args.start:
        start_ts = pd.Timestamp(args.start)
        valid_dates = [d for d in valid_dates if pd.Timestamp(d) >= start_ts]
    if args.end:
        end_ts = pd.Timestamp(args.end)
        valid_dates = [d for d in valid_dates if pd.Timestamp(d) <= end_ts]

    print(f"{len(all_dates)} trading days on file ({pd.Timestamp(all_dates[0]).date()} -> "
          f"{pd.Timestamp(all_dates[-1]).date()}). Skipping the first {skipped} "
          f"(insufficient {ds.WINDOW_ADTV_HIGH}-day lookback). "
          f"{len(valid_dates)} valid backtest day(s) to process"
          + (f" within --start/--end" if (args.start or args.end) else "") + ".")

    if not valid_dates:
        print("Nothing to do.")
        sys.exit(0)

    batch_size = max(1, args.batch_size)
    n_batches = (len(valid_dates) + batch_size - 1) // batch_size
    print(f"Processing in {n_batches} batch(es) of up to {batch_size} day(s) each.\n")

    first_payload_shown = False
    total_posted = 0
    t_start = time.monotonic()

    for b in range(n_batches):
        batch_dates = valid_dates[b * batch_size:(b + 1) * batch_size]
        runs = []
        for d in batch_dates:
            d_iso = pd.Timestamp(d).date().isoformat()
            try:
                payload = ds.build_payload(cfg, as_of_date=d, run_date=d_iso, source="Backfill")
            except Exception as e:
                print(f"\n  ERROR building payload for {d_iso}: {repr(e)}")
                print(f"  {total_posted} day(s) posted successfully before this. Fix the "
                      f"underlying issue, then resume with: --start {d_iso}")
                sys.exit(1)
            runs.append(payload)
            print(f"  [{len(runs) + b * batch_size}/{len(valid_dates)}] {d_iso}: "
                  f"universe={payload['universe_count']} pass={payload['pass_count']} "
                  f"shortlist={len(payload['top'])}")

        if args.dry_run:
            if not first_payload_shown:
                print("\n--dry-run: not posting. First run's payload:")
                print(json.dumps(runs[0], indent=2, default=str))
                first_payload_shown = True
            continue

        print(f"  -> posting batch {b + 1}/{n_batches} ({len(runs)} day(s))...")
        try:
            status, body = post_bulk(cfg, runs)
            print(f"     Web app response: {status} {body}")
            total_posted += len(runs)
        except Exception as e:
            print(f"     ERROR posting batch {b + 1}: {repr(e)}")
            print(f"     {total_posted} day(s) posted successfully before this failure. "
                  f"Safe to rerun -- e.g. narrow --start to the first date in the failed "
                  f"batch ({pd.Timestamp(batch_dates[0]).date()}) to resume from there.")
            sys.exit(1)

    elapsed = time.monotonic() - t_start
    if args.dry_run:
        print(f"\n--dry-run complete: {len(valid_dates)} day(s) computed in {elapsed:.0f}s, nothing posted.")
    else:
        print(f"\nDone: {total_posted}/{len(valid_dates)} day(s) posted in {elapsed:.0f}s across {n_batches} batch(es).")


if __name__ == "__main__":
    main()
