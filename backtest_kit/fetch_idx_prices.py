"""
fetch_idx_prices.py

Purpose
-------
Downloads daily closing prices from Yahoo Finance for every ticker that has
ever appeared in the "IDX Swing-Trade Stock Screen" Google Sheet's Screen_Log
tab (both Source=Live and Source=Backfill rows), covering the date range
needed to compute forward returns (LastPrice at LastTradingDate -> price N
trading days later).

Why this runs on YOUR machine, not in Claude's sandbox
-------------------------------------------------------
The cloud sandbox this analysis was planned in blocks outbound network calls
to arbitrary domains (allowlist covers only package registries and
Anthropic's own API) -- confirmed by testing yfinance there and getting
`connect_rejected` from the sandbox's own egress proxy on every
finance.yahoo.com host. Your own machine has normal internet access, so the
fetch has to happen here.

Inputs
------
tickers_needed.csv (same folder) -- 369 stock tickers, each with the
earliest and latest LastTradingDate it was seen at in Screen_Log, plus one
extra row for ^JKSE (the Jakarta Composite Index) used as a market
benchmark -- "did the shortlist beat just holding the index" is a more
standard comparison than "beat the average of only the stocks that ever
got shortlisted," which is a biased comparison set on its own. This file is
mostly documentation of *why* the date range below was chosen; the actual
fetch uses one shared date window for every symbol (simpler, and the extra
rows outside a given ticker's own window are harmless -- they just don't
get used downstream).

Method
------
1. Ticker convention: IDX tickers on Yahoo Finance take a ".JK" suffix
   (e.g. BBCA -> BBCA.JK). This is a documented Yahoo convention, not
   something specific to this workbook.
2. Date window: 2025-04-01 through today+1 day. Lower bound has a few weeks
   of slack before the earliest LastTradingDate (2025-04-16); upper bound is
   "as much as Yahoo has" -- it will simply return no rows past the most
   recent trading day, which is expected and not an error.
3. One yf.download() call per ticker (not a single multi-ticker batch call),
   wrapped in try/except, with a short delay between requests to avoid
   tripping Yahoo's rate limiting. This is slower (expect several minutes for
   369 tickers) but makes partial failures easy to isolate and retry.
4. Price field: unadjusted "Close" (not "Adj Close"). IDX-listed companies
   occasionally do rights issues/stock splits; unadjusted close matches what
   LastPrice in the sheet represents (a traded price on that day), but it
   means a forward return computed across a split/rights-issue date would be
   wrong. This script does not attempt to detect or adjust for corporate
   actions -- flagged here as a known limitation, not silently handled.

Outputs
-------
idx_price_history.csv  -- long format: Ticker, Date, Close
                           (one row per ticker per trading day with data)
fetch_coverage.csv      -- one row per requested ticker: rows returned,
                           first/last available date, and a Status column
                           (OK / NO_DATA / ERROR) so gaps are visible rather
                           than silently dropped.

Usage
-----
    pip install yfinance --upgrade
    python fetch_idx_prices.py

Then send back idx_price_history.csv and fetch_coverage.csv (both land in
this same folder) so the forward-return backtest can be computed from them.
"""

import csv
import datetime
import time

import yfinance as yf

TICKERS_FILE = "tickers_needed.csv"
OUTPUT_PRICES = "idx_price_history.csv"
OUTPUT_COVERAGE = "fetch_coverage.csv"

FETCH_START = "2025-04-01"
FETCH_END = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
SLEEP_BETWEEN_REQUESTS_SEC = 0.6  # politeness delay; raise if you hit rate-limit errors


def load_tickers(path):
    tickers = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tickers.append(row["Ticker"])
    return tickers


def fetch_one(ticker):
    # ^JKSE (Jakarta Composite Index) is a market-benchmark row added to
    # tickers_needed.csv for comparing the shortlist's forward returns
    # against "just held the market" -- Yahoo index symbols are used as-is,
    # no .JK suffix.
    yf_symbol = ticker if ticker.startswith("^") else f"{ticker}.JK"
    df = yf.download(
        yf_symbol,
        start=FETCH_START,
        end=FETCH_END,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    return df


def main():
    tickers = load_tickers(TICKERS_FILE)
    print(f"Loaded {len(tickers)} tickers from {TICKERS_FILE}")
    print(f"Fetch window: {FETCH_START} to {FETCH_END}")

    price_rows = []
    coverage_rows = []

    for i, ticker in enumerate(tickers, 1):
        status = "OK"
        n_rows = 0
        first_date = ""
        last_date = ""
        try:
            df = fetch_one(ticker)
            if df is None or df.empty:
                status = "NO_DATA"
            else:
                # yfinance sometimes returns a MultiIndex column set even for
                # a single ticker; normalize to a flat "Close" column.
                if isinstance(df.columns, __import__("pandas").MultiIndex):
                    close = df["Close"].iloc[:, 0]
                else:
                    close = df["Close"]
                close = close.dropna()
                n_rows = len(close)
                if n_rows == 0:
                    status = "NO_DATA"
                else:
                    first_date = close.index.min().strftime("%Y-%m-%d")
                    last_date = close.index.max().strftime("%Y-%m-%d")
                    for dt, val in close.items():
                        price_rows.append((ticker, dt.strftime("%Y-%m-%d"), float(val)))
        except Exception as exc:
            status = f"ERROR: {exc}"

        coverage_rows.append((ticker, n_rows, first_date, last_date, status))
        print(f"[{i}/{len(tickers)}] {ticker}: {status} ({n_rows} rows)")

        time.sleep(SLEEP_BETWEEN_REQUESTS_SEC)

    with open(OUTPUT_PRICES, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Ticker", "Date", "Close"])
        w.writerows(price_rows)

    with open(OUTPUT_COVERAGE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Ticker", "RowsReturned", "FirstDateAvailable", "LastDateAvailable", "Status"])
        w.writerows(coverage_rows)

    ok = sum(1 for r in coverage_rows if r[4] == "OK")
    no_data = sum(1 for r in coverage_rows if r[4] == "NO_DATA")
    errors = sum(1 for r in coverage_rows if r[4].startswith("ERROR"))
    print("\n--- Summary ---")
    print(f"OK: {ok}   NO_DATA: {no_data}   ERROR: {errors}   Total: {len(coverage_rows)}")
    print(f"Wrote {len(price_rows)} price rows to {OUTPUT_PRICES}")
    print(f"Wrote coverage report to {OUTPUT_COVERAGE}")


if __name__ == "__main__":
    main()
