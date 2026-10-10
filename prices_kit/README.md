# Daily All Tickers — full-universe OHLCV population

Replaces the "Daily All Tickers" tab's current content (a `Screen_Log` self-join formula
that only ever shows a ticker on days it happened to get shortlisted) with real daily
Open/High/Low/Close/Volume for the full ~950-980 ticker universe, sourced from your own
pipeline's `stock_summary` data — not GOOGLEFINANCE, not Yahoo Finance.

## What's in this folder

- `AppsScript_Addition.gs` — code to paste into your existing `Code.gs` in the Apps Script
  editor. Adds two new tab-writing functions and two new `doPost` modes
  (`prices_init`, `prices_daily`). Full instructions are in the file's header comment.
  Tested: 8/8 passing against a mock Sheets environment (`test_addition.js`, included for
  your own reference — `node test_addition.js` to re-run it).
- `backfill_daily_prices.py` — run **once** to load history. Drop into your repo's
  `python/` folder alongside `daily_screen.py` (it imports that module for config loading).
- `stock_summary_full_history.parquet` — the data `backfill_daily_prices.py` reads. See
  "Where this data came from" below before trusting it.
- `daily_screen_addition.py` — code to paste into your existing `daily_screen.py`, so each
  day's screen run also posts that day's prices going forward. Instructions in the file.

## Where this data came from (read before running)

Your repo's *live* `stock_summary.parquet` currently only covers 2026-08-13 → 2026-09-11
(~1 month) — that's genuinely all there is in the day-by-day ingestion pipeline right now.
But an earlier commit in your repo's own git history (`8d499e9`, "Backfill: full historical
stock_summary.parquet") had a version covering **2025-03-11 → 2026-09-11, 980 tickers,
339,408 rows** — recovered here with `git show 8d499e9:data/parquet/stock_summary.parquet`,
no external re-fetch involved.

I checked it against your live data before shipping it: every row in the 2026-08-13 →
2026-09-11 overlap matches exactly — 19,260/19,260 rows, zero differing `Close` values.
That's solid evidence it's the same underlying feed for the period both files share. What I
*can't* verify is how the pre-2026-08-13 portion was originally produced — your repo's raw
day-by-day partition store (`data/timeseries/stock_summary/`) only ever had 20 date-partitions,
all from 2026-08-13 onward, so this deeper history predates that pipeline and was evidently
loaded some other way, once. Treat the recent portion as verified-consistent fact, and the
pre-08-13 portion as recovered historical data of unconfirmed provenance, not independently
re-validated row by row.

## Design choices, since OHLCV doesn't fit the old tab's shape

The old tab was a single ticker × date grid — one value per cell. OHLCV is 5 values per
ticker/day, so it can't fit that shape. The new tab is **long format**: one row per
(Ticker, Date), columns `Ticker | Name | Date | Open | High | Low | Close | Volume`. If you
want a wide, eyeball-able grid for one specific field (say, Close only), that's a `QUERY` or
`FILTER` formula on top of this tab — spreadsheet-native, no script needed, and exactly the
kind of thing this project's "spreadsheet-first" approach is for.

## Steps

1. **Paste `AppsScript_Addition.gs`'s two pieces into your existing `Code.gs`** (in the Apps
   Script editor — wherever your actual current file stands right now; this doesn't assume
   any particular starting version). Redeploy the same way you always do: Deploy > Manage
   deployments > pencil icon > Version: **New version** > Deploy.

2. **Drop `backfill_daily_prices.py` and `stock_summary_full_history.parquet` into your
   `python/` folder** (same folder as `daily_screen.py` and `config.json`).

3. **Dry-run first** to see what it'll do without posting anything:
   ```
   uv run python backfill_daily_prices.py --dry-run
   ```
   Expect ~354 trading days, ~71 batches at the default `--batch-size 5`.

4. **Run it for real**:
   ```
   uv run python backfill_daily_prices.py
   ```
   This takes a while (71 sequential POSTs, each writing ~4,800 rows) — that's expected, not
   a hang. If it fails partway (network hiccup, Apps Script timeout), just re-run the same
   command — it checkpoints progress in `.backfill_daily_prices_checkpoint` and resumes
   rather than re-posting or duplicating already-landed days.

5. **Paste `daily_screen_addition.py`'s two pieces into your existing `daily_screen.py`**
   so each future daily run keeps "Daily All Tickers" current automatically. This part only
   takes effect once the ingestion freeze (diagnosed earlier — `stock_summary` stuck at
   2026-09-11) is actually fixed; until then there's no new date for it to post.

6. Open the Sheet and check "Daily All Tickers" — header should read `Ticker | Name | Date
   | Open | High | Low | Close | Volume`, ~339,000+ rows.
