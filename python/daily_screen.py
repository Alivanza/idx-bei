"""
Daily IDX swing screener -> posts results to a Google Sheet via Apps Script Web App.

Run this AFTER `uv run idx daily` (and ideally `uv run idx corporate`) have refreshed
data/parquet/stock_summary.parquet and data/parquet/corporate_actions.parquet.

Usage:
    uv run python daily_screen.py

Config lives in config.json (same folder) -- edit that, not this file, once you have
your Apps Script Web App URL.
"""
import json
import os
import sys
import urllib.request
import urllib.error
import datetime

import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
# idx.core.utils.DATA_DIR resolves to the REPO ROOT's data/ folder (one level above
# this python/ folder), not python/data/ -- this was wrong in the first version and
# is why "not found" showed up after a real `uv run idx daily` run.
PARQUET_DIR = os.path.abspath(os.path.join(HERE, "..", "data", "parquet"))

# ---- Screening thresholds (same as the original brief) ----
MIN_ADTV = 5_000_000_000       # IDR
PRICE_MIN, PRICE_MAX = 50, 3000
MIN_ATR_PCT = 0.03
MAX_DIST_TO_HIGH = 0.05
TOP_N = 8
WINDOW_ADTV_HIGH = 20   # trading days
WINDOW_ATR = 14         # trading days
CA_LOOKAHEAD_DAYS = 10  # trading days


def load_config():
    """Env vars WEBAPP_URL / SHARED_SECRET win when set (GitHub Actions secrets land here
    without ever being committed to the repo). Falls back to config.json for local runs."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    cfg["webapp_url"] = os.environ.get("WEBAPP_URL", cfg.get("webapp_url", ""))
    cfg["shared_secret"] = os.environ.get("SHARED_SECRET", cfg.get("shared_secret", ""))

    if not cfg.get("webapp_url") or "PASTE_YOUR" in cfg["webapp_url"]:
        print("ERROR: no webapp_url found. Set it in config.json (local) or as the WEBAPP_URL "
              "repo secret (GitHub Actions).")
        sys.exit(1)
    return cfg


def run_screen():
    ss_path = os.path.join(PARQUET_DIR, "stock_summary.parquet")
    if not os.path.exists(ss_path):
        print(f"ERROR: {ss_path} not found. Run `uv run idx daily` first.")
        sys.exit(1)

    ss = pd.read_parquet(ss_path)
    ss = ss.sort_values(["StockCode", "Date"]).reset_index(drop=True)
    ss["rank_desc"] = ss.groupby("StockCode")["Date"].rank(method="first", ascending=False).astype(int) - 1
    ss["PrevClose"] = ss.groupby("StockCode")["Close"].shift(1)
    ss["TrueRange"] = np.maximum.reduce([
        (ss["High"] - ss["Low"]).values,
        (ss["High"] - ss["PrevClose"]).abs().values,
        (ss["Low"] - ss["PrevClose"]).abs().values,
    ])

    last_date = ss["Date"].max()

    rows = []
    for code, g in ss.groupby("StockCode"):
        g = g.sort_values("rank_desc")
        last_rows = g[g["rank_desc"] == 0]
        if last_rows.empty:
            continue
        last = last_rows.iloc[0]
        last_price = last["Close"]
        if not last_price or pd.isna(last_price):
            continue

        win = g[g["rank_desc"] <= WINDOW_ADTV_HIGH - 1]
        adtv = win["Value"].mean()
        high_n = win["High"].max()

        win_atr = g[(g["rank_desc"] <= WINDOW_ATR - 1) & g["TrueRange"].notna()]
        atr = win_atr["TrueRange"].mean() if len(win_atr) else np.nan
        atr_pct = atr / last_price if (pd.notna(atr) and last_price) else np.nan

        dist_to_high = (high_n - last_price) / high_n if high_n else np.nan

        recent3_high = g[g["rank_desc"] <= 2]["High"].max()
        prior_high_ex3 = g[(g["rank_desc"] >= 3) & (g["rank_desc"] <= WINDOW_ADTV_HIGH - 1)]["High"].max()
        breakout = (pd.notna(recent3_high) and pd.notna(prior_high_ex3) and recent3_high > prior_high_ex3)

        last3_vol = g[g["rank_desc"] <= 2]["Volume"].sum()
        last3_freq = g[g["rank_desc"] <= 2]["Frequency"].sum()
        zero_recent = (last3_vol == 0 and last3_freq == 0)

        rows.append(dict(
            Ticker=code, Name=last["StockName"], LastPrice=float(last_price),
            LotCost=float(last_price) * 100, ADTV20=float(adtv) if pd.notna(adtv) else None,
            ATR14_pct=float(atr_pct) if pd.notna(atr_pct) else None,
            High20=float(high_n) if pd.notna(high_n) else None,
            DistToHigh20=float(dist_to_high) if pd.notna(dist_to_high) else None,
            BreakoutLast3=bool(breakout), ZeroTradeLast3=bool(zero_recent),
        ))

    df = pd.DataFrame(rows)
    df["F1_Liquidity"] = df["ADTV20"] >= MIN_ADTV
    df["F2_PriceRange"] = df["LastPrice"].between(PRICE_MIN, PRICE_MAX)
    df["F3_VolMomentum"] = (df["ATR14_pct"] >= MIN_ATR_PCT) & (
        (df["DistToHigh20"] <= MAX_DIST_TO_HIGH) | df["BreakoutLast3"]
    )
    df["F4_NotZeroTradeFlag"] = ~df["ZeroTradeLast3"]

    # Corporate actions: best-effort, caveat always sent alongside the data
    ca_path = os.path.join(PARQUET_DIR, "corporate_actions.parquet")
    ca_caveat = "corporate_actions.parquet not found -- F5 not checked"
    pending_tickers = set()
    if os.path.exists(ca_path):
        ca = pd.read_parquet(ca_path)
        ca_latest = ca["TanggalPencatatan"].max()
        window_end = pd.Timestamp(last_date) + pd.tseries.offsets.BDay(CA_LOOKAHEAD_DAYS)
        pending = ca[(ca["TanggalPencatatan"] >= last_date) & (ca["TanggalPencatatan"] <= window_end)]
        pending_tickers = set(pending["KodeEmiten"])
        ca_caveat = (f"corporate_actions data last updated through {ca_latest.date()} "
                     f"-- treat F5 as informational only if that date is more than a few days old")
    df["F5_NoPendingCA"] = ~df["Ticker"].isin(pending_tickers)

    df["PassesAll"] = (df["F1_Liquidity"] & df["F2_PriceRange"] & df["F3_VolMomentum"] &
                        df["F4_NotZeroTradeFlag"] & df["F5_NoPendingCA"])

    df_sorted = df.sort_values("ADTV20", ascending=False, na_position="last")
    passers = df_sorted[df_sorted["PassesAll"]]

    top = passers.head(TOP_N).copy()
    near_misses = pd.DataFrame()
    if len(passers) < 3:
        # closest near-misses: highest ADTV among those failing exactly one filter
        fail_count = (~df_sorted[["F1_Liquidity", "F2_PriceRange", "F3_VolMomentum",
                                   "F4_NotZeroTradeFlag", "F5_NoPendingCA"]]).sum(axis=1)
        near = df_sorted[(fail_count == 1)].head(5).copy()
        near_misses = near

    return df, top, near_misses, last_date, ca_caveat


def to_records(top_df):
    out = []
    for _, r in top_df.iterrows():
        out.append(dict(
            Ticker=r["Ticker"], Name=r.get("Name", ""), LastPrice=r["LastPrice"], LotCost=r["LotCost"],
            ADTV20=r["ADTV20"], ATR14_pct=round(r["ATR14_pct"], 4) if pd.notna(r["ATR14_pct"]) else None,
            DistToHigh20=round(r["DistToHigh20"], 4) if pd.notna(r["DistToHigh20"]) else None,
            BreakoutLast3=bool(r["BreakoutLast3"]),
        ))
    return out


def failed_filters(row):
    names = {"F1_Liquidity": "Liquidity", "F2_PriceRange": "Price range", "F3_VolMomentum": "Vol+Momentum",
             "F4_NotZeroTradeFlag": "Not zero-trade(possible suspension)", "F5_NoPendingCA": "No pending CA"}
    return [label for key, label in names.items() if not bool(row[key])]


def main():
    cfg = load_config()
    df, top, near_misses, last_date, ca_caveat = run_screen()

    payload = {
        "secret": cfg.get("shared_secret", ""),
        "run_date": datetime.date.today().isoformat(),
        "last_trading_date": pd.Timestamp(last_date).date().isoformat(),
        "universe_count": int(len(df)),
        "pass_count": int(df["PassesAll"].sum()),
        "top": to_records(top),
        "near_misses": [
            {"Ticker": r["Ticker"], "Name": r.get("Name", ""), "ADTV20": r["ADTV20"],
             "FailedFilters": failed_filters(r)}
            for _, r in near_misses.iterrows()
        ] if len(near_misses) else [],
        "caveats": [
            ca_caveat,
            "F4 (ARA/ARB/UMA) uses a same-day zero-volume proxy, not IDX's official suspension/UMA feed -- verify manually before acting.",
        ],
    }

    print(json.dumps(payload, indent=2, default=str))

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cfg["webapp_url"], data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("Web app response:", resp.status, resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("HTTP error posting to sheet:", e.code, e.read().decode("utf-8"))
        sys.exit(1)
    except Exception as e:
        print("Error posting to sheet:", repr(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
