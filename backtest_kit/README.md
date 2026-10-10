# IDX Shortlist Backtest Kit

Answers: does the daily shortlist's LastPrice actually go up more than the
market over the following 5/10/20 trading days?

## Why this exists as a local kit instead of running in the sandbox

Two things were checked and ruled out before landing here, both worth
knowing since they explain the design of what follows:

1. **yfinance from the analysis sandbox is blocked.** The sandbox's own
   network policy rejects every call to Yahoo Finance's servers
   (`connect_rejected` on `query2.finance.yahoo.com` etc. -- confirmed by
   testing, not assumed). It only allows a short list of package registries
   and Anthropic's own API. Your machine has normal internet access, so
   step 1 below runs there.
2. **Pulling this via GOOGLEFINANCE formulas inside the Sheet is unreliable
   at this scale.** Your own workbook already has a "Daily All Tickers" tab
   attempting something similar for ~250 tickers, and most cells are blank
   (checked directly: ASII, ADRO, ANTM, AALI all show zero populated days
   out of 51). That's real evidence a script-driven mass GOOGLEFINANCE
   write throttles around this size, not a theoretical concern.

## What's in this folder

- `screen_log_export.csv` -- every Screen_Log row from the live sheet
  (4,982 rows; 10 rows with a corrupted Ticker value were excluded -- see
  "Bug found along the way" below), already exported so you don't need to
  do anything with the Sheet itself.
- `tickers_needed.csv` -- the 369 unique tickers that ever appeared in
  Screen_Log, each with the earliest/latest date it was needed, plus one
  extra row for `^JKSE` (Jakarta Composite Index) used as the market
  benchmark. Documentation of *why*, not something you need to edit.
- `fetch_idx_prices.py` -- run this first. Downloads daily closes from
  Yahoo Finance for all 370 symbols.
- `compute_backtest.py` -- run this second. Computes forward returns and
  the hit-rate/mean/median summary.

## Steps

1. `pip install yfinance --upgrade` (skip if already installed)
2. `python fetch_idx_prices.py`
   Expect several minutes (369 tickers, one request each, a 0.6s pause
   between requests to stay polite to Yahoo). It prints progress per
   ticker and will not stop on an individual failure -- a ticker with no
   Yahoo coverage is recorded as `NO_DATA` and everything else continues.
   Produces `idx_price_history.csv` and `fetch_coverage.csv`.
3. **Check `fetch_coverage.csv` before trusting the results** -- open it
   and look at the `Status` column. `NO_DATA` or `ERROR` rows are tickers
   the backtest will have no forward return for at all; this is expected
   to some degree for small/illiquid or newly-listed IDX names (Yahoo's
   IDX coverage is known to be inconsistent for these), not a bug in the
   script. Worth a glance at how many rows are affected before drawing
   conclusions from the summary.
4. `python compute_backtest.py`
   Produces `backtest_row_detail.csv` (every row × every horizon, so you
   can audit or rebuild any single number) and `backtest_summary.csv`
   (the hit-rate/mean/median rollup, separately for Live and Backfill
   rows, per horizon).
5. Send back all four output files (`fetch_coverage.csv`,
   `idx_price_history.csv`, `backtest_row_detail.csv`,
   `backtest_summary.csv`) and the results get written up with the
   assumptions/sensitivities that matter, and optionally into a new
   `Backtest_Results` tab in the Sheet.

The exact forward-return formula, a worked example, and every assumption
behind it (price source, no corporate-action adjustment, why Backfill vs.
Live are reported separately, why ^JKSE rather than "average of the
shortlist tickers" is the benchmark) are documented in the docstring at the
top of `compute_backtest.py` -- read that before trusting any number it
prints.

## Bug found along the way (separate from this backtest, flagged for you)

10 Screen_Log rows for **Triniti Dinamik Tbk.** have their `Ticker` column
stored as the literal boolean `TRUE` instead of a ticker string -- almost
certainly because the real ticker for this company is the four-letter code
that Google Sheets auto-converts to a boolean when written as a bare string
(the same class of bug as writing `=TRUE` instead of `'TRUE`). Those 10 rows
were excluded from this backtest (can't join to a price series without a
real ticker) and are otherwise sitting in Screen_Log with a broken Ticker
field. Let me know if you want this fixed at the source (forcing the Ticker
column to text format in the Apps Script write, same pattern already used
elsewhere in the workbook).
