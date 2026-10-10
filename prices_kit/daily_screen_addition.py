# ADD THIS to your existing daily_screen.py -- do not replace the whole file. This is the
# ongoing (not one-time) half of populating "Daily All Tickers": after backfill_daily_prices.py
# has loaded history once, this keeps it current by posting each day's new row the same way
# every other tab already gets updated.
#
# WHERE IT GOES:
#
# 1. Add this function anywhere near post_payload() (it follows the exact same shape):

def build_daily_prices_payload(cfg, date_iso):
    """Reads stock_summary.parquet's rows for exactly one date and returns them as the
    compact [Ticker, Name, Date, Open, High, Low, Close, Volume] rows the Apps Script side's
    appendDailyPrices() expects. Returns (rows, payload_dict) -- payload_dict is what
    post_daily_prices() below actually POSTs."""
    import pandas as pd
    ss_path = os.path.join(PARQUET_DIR, "stock_summary.parquet")
    df = pd.read_parquet(ss_path)
    day = df[pd.to_datetime(df["Date"]).dt.date.astype(str) == date_iso].sort_values("StockCode")
    rows = []
    for r in day.itertuples(index=False):
        rows.append([
            r.StockCode, r.StockName, date_iso,
            None if pd.isna(r.OpenPrice) else float(r.OpenPrice),
            None if pd.isna(r.High) else float(r.High),
            None if pd.isna(r.Low) else float(r.Low),
            None if pd.isna(r.Close) else float(r.Close),
            None if pd.isna(r.Volume) else float(r.Volume),
        ])
    payload = {"secret": cfg.get("shared_secret", ""), "mode": "prices_daily", "date": date_iso, "rows": rows}
    return rows, payload


def post_daily_prices(cfg, date_iso):
    """POSTs one day's full-universe OHLCV to the Daily All Tickers tab. Deliberately
    separate from post_payload()/the main screen payload -- different body schema (mode
    field), and a failure here should not be treated as a failure of the actual screen run
    (see the try/except around its call in main(), below)."""
    rows, payload = build_daily_prices_payload(cfg, date_iso)
    if not rows:
        print(f"No stock_summary rows for {date_iso} -- skipping Daily All Tickers post "
              f"(this is normal on a day ingest_daily() itself found no data for).")
        return None
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cfg["webapp_url"], data=data, headers={"Content-Type": "application/json"})
    # Longer than post_payload()'s 30s -- this posts ~950 rows (vs. a few dozen for the
    # normal screen payload), and Apps Script also does a full Date-column scan first
    # (appendDailyPrices's dedupe check) before writing.
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.status, resp.read().decode("utf-8")


# 2. In main(), right after the existing post_payload(cfg, payload) call succeeds, add:

    try:
        status, body = post_daily_prices(cfg, payload["last_trading_date"])
        if status is not None:
            print("Daily All Tickers post response:", status, body)
    except Exception as e:
        # Deliberately does not sys.exit(1) here -- a failure populating Daily All Tickers
        # is not a failure of the actual daily screen (Shortlist/Screen_Log/etc. already
        # posted fine above). Surface it, but don't turn a healthy screen run red over it.
        print("Error posting Daily All Tickers (non-fatal, screen data above still posted):", repr(e))

# That's the whole change -- build_daily_prices_payload() and post_daily_prices() are new
# top-level functions (same style as build_payload()/post_payload() already in the file),
# and main() gets three new lines after its existing post_payload() call.
