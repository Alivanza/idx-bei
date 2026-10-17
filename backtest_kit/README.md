# IDX Shortlist Backtest Kit

Answers: does the daily shortlist's LastPrice rise more than the market (^JKSE)
over the following 5 / 10 / 20 trading days?

Last full run: 16/10/2026. Results and sensitivities live in the Google Sheet
tab `Backtest_Results`; this folder holds the code and inputs needed to rebuild
every number in it.

## Why the fetch runs on your PC, not in Claude's sandbox

The cloud sandbox blocks Yahoo Finance (`connect_rejected` on every Yahoo
host), so step 1 below must run on a machine with normal internet access.
GOOGLEFINANCE formulas inside the Sheet were also ruled out: at this scale a
script-driven mass write throttles and most cells come back blank.

## Files

Tracked in git:

- `fetch_idx_prices.py` -- downloads daily Open / High / Low / Close / Volume
  (unadjusted, Yahoo `.JK` suffix) for every ticker in `tickers_needed.csv`,
  plus `^JKSE` as the benchmark. Close stays in column 3 so
  `compute_backtest.py` is unaffected by the extra columns.
- `compute_backtest.py` -- forward returns, hit rate, mean / median, excess
  versus ^JKSE. Standard library only. Formula, worked example and assumptions
  are in its docstring: read that before trusting any number.
- `tickers_needed.csv` -- every ticker that ever appeared in Screen_Log
  (373 incl. `TRUE` = Triniti Dinamik) plus `^JKSE`, with earliest / latest
  screen date. Documentation of why the fetch window was chosen.
- `fetch_coverage.csv` -- per-ticker fetch status (OK / NO_DATA / ERROR).
  Check it after every fetch.
- `backtest_summary.csv` -- per Source and horizon: N, hit rate, mean, median,
  mean benchmark, mean excess.

Git-ignored (large or regenerable; see `.gitignore`):

- `screen_log_export.csv` -- Screen_Log exported from the Sheet
  (RunDate, LastTradingDate, Ticker, Name, Sector, LastPrice, Source).
  5,267 rows on the last run: 5,252 Backfill + 15 Live.
- `idx_price_history.csv` -- output of the fetch (~7 MB).
- `backtest_row_detail.csv` -- one row per Screen_Log row per horizon, so any
  single number can be audited.

## Steps (from this folder, PowerShell, plain `python`)

    cd C:\Users\alrhu\OneDrive\Documents\idx-bei\backtest_kit
    pip install yfinance --upgrade     # skip if already installed
    python fetch_idx_prices.py         # ~4 minutes for ~375 symbols
    python compute_backtest.py

1. Re-export Screen_Log to `screen_log_export.csv` first if the Sheet has new
   rows since the last run.
2. Open `fetch_coverage.csv` and look at Status. NO_DATA or ERROR tickers get
   no forward return.
3. `compute_backtest.py` needs no packages beyond the standard library.
4. Known quirk: rows with NO_PRICE_DATA / ENTRY_DATE_NOT_FOUND are written
   with fewer columns than priced rows. Parse the detail file with
   `csv.reader` (Reason is the last column), not `csv.DictReader`.

## Assumptions that matter most (full list in the compute docstring)

- Backfill rows (2025-04-16 onward) are the only sample large enough to read;
  Live is one date and is reported separately. Backfill may carry hindsight
  bias if ADTV20 / High20 / ATR14_pct were not computed point-in-time -- not
  verified.
- Unadjusted closes, no split or rights adjustment. Rows where the Yahoo entry
  price differs from the sheet LastPrice by more than 1% are flagged in
  `EntryPriceDiffPct_vs_Sheet`, not removed (85 of 5,267 on the last run).
- Fills assumed at the stated price with no costs, slippage or liquidity
  limit. Entry at the screen-day close (the headline) is not executable if the
  screen runs after the close; Open(D+1) is the first executable price.

## History

- The `TRUE` ticker bug (Triniti Dinamik's code read as boolean TRUE by Google
  Sheets) was fixed at the source in `python/AppsScript_Code.gs`
  (`forceTextColumn` before `setValues`). `TRUE.JK` is now fetched and priced.
