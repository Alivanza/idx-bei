"""
compute_backtest.py

Purpose
-------
Computes forward returns for every Screen_Log row (Live and Backfill) using
the price history fetched by fetch_idx_prices.py, and aggregates hit
rate / mean / median return per horizon -- for the shortlist, and against
holding the Jakarta Composite Index (^JKSE) over the same windows.

Formula (apply this by hand to check any single number this script prints)
----------------------------------------------------------------------------
For a Screen_Log row with ticker T and LastTradingDate D, and horizon of
N trading days:

    EntryPrice        = Close(T, D)                     -- from idx_price_history.csv
    ExitPrice         = Close(T, the N-th trading day AFTER D in T's own price series)
    ForwardReturn_N   = (ExitPrice - EntryPrice) / EntryPrice

"N-th trading day after D" is found positionally: sort T's available trading
days ascending, find D's position i, take the price at position i+N. This is
why the fetch script pulls whole daily series per ticker rather than just a
price N calendar days later -- weekends and IDX holidays are irregular, so
"N trading days" is not "N calendar days."

Worked example
---------------
Screen_Log row: DOOH, LastTradingDate=2026-09-11, LastPrice=400 (from the
sheet). Suppose idx_price_history.csv has DOOH.JK trading on 2026-09-11,
09-14, 09-15, 09-16, 09-17, 09-18 (first 5 trading days after) at closes
400, 410, 405, 420, 415, 430.
  EntryPrice (day 0)      = 400
  ExitPrice, N=5          = 430  (the 5th row after day 0)
  ForwardReturn_5         = (430 - 400) / 400 = +7.50%

Assumptions and known limitations (stated explicitly, not fixed silently)
---------------------------------------------------------------------------
1. Price source is Yahoo Finance (.JK suffix), not the IDX-direct feed the
   Python pipeline itself uses for LastPrice. EntryPriceDiffPct in the
   output flags rows where the two sources disagree by more than 1%, so
   silent data-quality gaps are visible rather than absorbed into the
   return math.
2. No corporate-action (split/rights issue) adjustment. A forward return
   spanning such an event will be wrong; not detected or flagged separately
   here -- would need each ticker's corporate-action history to catch.
3. Backfill rows (2025-04-16 to 2026-09-10, ~370 tickers, ~333 dates) are
   the only population large enough for a statistical read right now --
   Live has a single date (2026-09-25 run) and 15 tickers, reported
   separately, not pooled with Backfill. The sheet's own Guide tab flags a
   hindsight-bias risk for Backfill rows (unclear whether ADTV20/High20/
   ATR14_pct were computed using only data available as of that historical
   date, or with retroactive/full-history data) -- this script does not
   resolve that; it is a property of how Backfill was generated upstream,
   not of this return calculation.
4. Rows whose forward window extends past the last available trading day
   (i.e., N trading days after D hasn't happened yet, or Yahoo has no more
   recent data) get a blank ForwardReturn_N with Reason="insufficient
   future data" -- not zero, not dropped silently.
5. Baseline is ^JKSE (index level, i.e., "just hold the market") over the
   identical [D, D+N] window for each row -- a standard alpha benchmark.
   It is NOT "the average of the 369 ever-shortlisted tickers," which would
   overstate the baseline (that set is itself pre-filtered by having passed
   liquidity/momentum screens at least once).

Inputs
------
screen_log_export.csv   -- RunDate, LastTradingDate, Ticker, Name, Sector,
                            LastPrice, Source (already exported from the
                            live sheet)
idx_price_history.csv   -- Ticker, Date, Close (from fetch_idx_prices.py;
                            includes a ^JKSE row for the benchmark)

Output
------
backtest_row_detail.csv     -- one row per Screen_Log row per horizon, with
                                EntryPrice, ExitPrice, ForwardReturn_N,
                                BenchmarkReturn_N, Excess_N, Reason
backtest_summary.csv        -- per Source (Live/Backfill) per horizon:
                                N (count with valid data), HitRate (%),
                                MeanReturn (%), MedianReturn (%),
                                MeanBenchmarkReturn (%), MeanExcess (%)

Usage
-----
    python compute_backtest.py
"""

import csv
from collections import defaultdict

SCREEN_LOG_FILE = "screen_log_export.csv"
PRICE_FILE = "idx_price_history.csv"
BENCHMARK_TICKER = "^JKSE"
HORIZONS = [5, 10, 20]

ROW_DETAIL_OUT = "backtest_row_detail.csv"
SUMMARY_OUT = "backtest_summary.csv"


def load_price_series(path):
    """Ticker -> sorted list of (date_str, close), ascending by date."""
    series = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            series[row["Ticker"]].append((row["Date"], float(row["Close"])))
    for t in series:
        series[t].sort(key=lambda r: r[0])
    return series


def find_index(dates, target_date):
    """Position of target_date in an ascending sorted date-string list, or -1."""
    lo, hi = 0, len(dates) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] == target_date:
            return mid
        elif dates[mid] < target_date:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def main():
    price_series = load_price_series(PRICE_FILE)
    price_dates = {t: [d for d, _ in rows] for t, rows in price_series.items()}
    price_close = {t: [c for _, c in rows] for t, rows in price_series.items()}

    bench_dates = price_dates.get(BENCHMARK_TICKER, [])
    bench_close = price_close.get(BENCHMARK_TICKER, [])
    if not bench_dates:
        print(f"WARNING: no data for benchmark {BENCHMARK_TICKER} in {PRICE_FILE} "
              f"-- benchmark columns will be blank.")

    detail_rows = []
    # aggregation[(source, horizon)] = list of (return, benchmark_return_or_None)
    aggregation = defaultdict(list)

    with open(SCREEN_LOG_FILE, newline="") as f:
        reader = csv.DictReader(f)
        screen_rows = list(reader)

    print(f"Loaded {len(screen_rows)} Screen_Log rows, "
          f"{len(price_series)} tickers with price data "
          f"(including benchmark: {'yes' if bench_dates else 'no'})")

    for row in screen_rows:
        ticker = row["Ticker"]
        d = row["LastTradingDate"]
        sheet_price = row["LastPrice"]
        source = row["Source"]

        dates = price_dates.get(ticker)
        closes = price_close.get(ticker)
        if not dates:
            for n in HORIZONS:
                detail_rows.append([*row.values(), n, "", "", "", "", "", "NO_PRICE_DATA"])
            continue

        idx0 = find_index(dates, d)
        if idx0 == -1:
            for n in HORIZONS:
                detail_rows.append([*row.values(), n, "", "", "", "", "", "ENTRY_DATE_NOT_FOUND"])
            continue

        entry_price = closes[idx0]
        try:
            sheet_price_f = float(str(sheet_price).replace(".", "").replace(",", "."))
        except ValueError:
            sheet_price_f = None
        entry_diff_pct = (
            round((entry_price - sheet_price_f) / sheet_price_f * 100, 2)
            if sheet_price_f else ""
        )

        bench_idx0 = find_index(bench_dates, d) if bench_dates else -1

        for n in HORIZONS:
            reason = "OK"
            exit_price = ""
            fwd_return = ""
            bench_return = ""
            excess = ""

            if idx0 + n >= len(dates):
                reason = "INSUFFICIENT_FUTURE_DATA"
            else:
                exit_price = closes[idx0 + n]
                fwd_return = round((exit_price - entry_price) / entry_price * 100, 3)

                if bench_idx0 != -1 and bench_idx0 + n < len(bench_dates):
                    b_entry = bench_close[bench_idx0]
                    b_exit = bench_close[bench_idx0 + n]
                    bench_return = round((b_exit - b_entry) / b_entry * 100, 3)
                    excess = round(fwd_return - bench_return, 3)

                aggregation[(source, n)].append((fwd_return, bench_return if bench_return != "" else None))

            detail_rows.append([
                row["RunDate"], d, ticker, row["Name"], row["Sector"], sheet_price, source,
                n, entry_price, entry_diff_pct, exit_price, fwd_return, bench_return, excess, reason
            ])

    with open(ROW_DETAIL_OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "RunDate", "LastTradingDate", "Ticker", "Name", "Sector", "SheetLastPrice", "Source",
            "HorizonTradingDays", "EntryPrice_Yahoo", "EntryPriceDiffPct_vs_Sheet",
            "ExitPrice_Yahoo", "ForwardReturnPct", "BenchmarkReturnPct_JKSE", "ExcessReturnPct", "Reason"
        ])
        w.writerows(detail_rows)

    summary_rows = []
    for (source, n), vals in sorted(aggregation.items()):
        returns = [v[0] for v in vals]
        bench_returns = [v[1] for v in vals if v[1] is not None]
        count = len(returns)
        hit_rate = round(100 * sum(1 for r in returns if r > 0) / count, 1) if count else ""
        mean_r = round(sum(returns) / count, 3) if count else ""
        median_r = round(sorted(returns)[count // 2], 3) if count else ""
        mean_bench = round(sum(bench_returns) / len(bench_returns), 3) if bench_returns else ""
        mean_excess = round(mean_r - mean_bench, 3) if (mean_r != "" and mean_bench != "") else ""
        summary_rows.append([source, n, count, hit_rate, mean_r, median_r, mean_bench, mean_excess])

    with open(SUMMARY_OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Source", "HorizonTradingDays", "N_ValidRows", "HitRatePct",
            "MeanReturnPct", "MedianReturnPct", "MeanBenchmarkReturnPct_JKSE", "MeanExcessReturnPct"
        ])
        w.writerows(summary_rows)

    print(f"\nWrote {len(detail_rows)} detail rows to {ROW_DETAIL_OUT}")
    print(f"Wrote {len(summary_rows)} summary rows to {SUMMARY_OUT}")
    for r in summary_rows:
        print(r)


if __name__ == "__main__":
    main()
