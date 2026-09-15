"""
Daily IDX swing screener -> posts results to a Google Sheet via Apps Script Web App.

Run this AFTER `uv run idx daily` and `uv run idx corporate` have refreshed
data/parquet/stock_summary.parquet and data/parquet/corporate_actions.parquet.
Sector classification (used for the per-sector shortlist cap) comes from
data/parquet/financial_ratios.parquet, refreshed by `uv run idx financial` +
`uv run idx parquet` -- see the note above FIN_RATIOS_PATH below.

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
FIN_RATIOS_PATH = os.path.join(PARQUET_DIR, "financial_ratios.parquet")

# ---- Screening thresholds (same as the original brief, plus the ATRsBelowHigh gate) ----
MIN_ADTV = 5_000_000_000       # IDR
PRICE_MIN, PRICE_MAX = 50, 3000
MIN_ATR_PCT = 0.03
MAX_DIST_TO_HIGH = 0.05
MAX_ATRS_BELOW_HIGH = 1.0      # ATRs -- how far a breakout is allowed to have pulled back and still count as "holding"
MAX_PER_SECTOR = 2             # IDX-IC sector cap on the shortlist
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


def load_sector_map():
    """IDX-IC level-1 sector per ticker, sourced from financial_ratios.parquet (company
    profile / financial-statement snapshot -- NOT refreshed by `idx daily`, only by
    `idx financial` + `idx parquet`). Sector assignment is structural and changes rarely,
    so a stale fsDate is a much smaller problem here than it would be for the ratio
    columns (per/roe/etc, which we don't use). Returns (map, caveat_string)."""
    if not os.path.exists(FIN_RATIOS_PATH):
        return {}, ("financial_ratios.parquet not found -- sector unknown for all tickers, "
                     "shortlist NOT capped by sector this run. Run `uv run idx financial` "
                     "then `uv run idx parquet` once to populate it.")
    fr = pd.read_parquet(FIN_RATIOS_PATH, columns=["code", "sector", "fsDate"])
    fr = fr.dropna(subset=["code"]).drop_duplicates(subset=["code"], keep="first")
    sector_map = dict(zip(fr["code"], fr["sector"].fillna("Unclassified")))
    latest_fs = pd.to_datetime(fr["fsDate"], errors="coerce").max()
    caveat = (f"Sector = IDX-IC classification from financial_ratios.parquet "
              f"(latest financial-statement date in that snapshot: {latest_fs.date() if pd.notna(latest_fs) else 'unknown'}). "
              f"That file is a periodic snapshot, not refreshed by the daily job -- newly listed tickers "
              f"may show as 'Unclassified' until it's re-run.")
    return sector_map, caveat


def run_screen(as_of_date=None):
    """as_of_date (optional): truncate stock_summary to Date <= as_of_date before computing
    every rolling window, so the screen only sees what would have been available on that
    date -- used by backfill_screen_log.py to replay history. corporate_actions.parquet and
    financial_ratios.parquet (sector) are NOT truncated -- only today's snapshot of either
    exists, so a historical replay necessarily uses today's copy of both. That's real
    hindsight bias on F5 and Sector specifically (not on F1-F4, which only look backward
    from as_of_date); see the caveats this function returns."""
    ss_path = os.path.join(PARQUET_DIR, "stock_summary.parquet")
    if not os.path.exists(ss_path):
        print(f"ERROR: {ss_path} not found. Run `uv run idx daily` first.")
        sys.exit(1)

    sector_map, sector_caveat = load_sector_map()

    ss = pd.read_parquet(ss_path)
    if as_of_date is not None:
        ss = ss[ss["Date"] <= pd.Timestamp(as_of_date)]
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
        atr = win_atr["TrueRange"].mean() if len(win_atr) else np.nan          # ATR14, in IDR (price units)
        atr_pct = atr / last_price if (pd.notna(atr) and last_price) else np.nan

        dist_to_high = (high_n - last_price) / high_n if high_n else np.nan
        # ATRsBelowHigh: how many ATR14's the last price sits below the 20-day high --
        # a volatility-scaled version of DistToHigh20. 0 or negative = at/above the high;
        # 1.0 = one full average daily range below it.
        atrs_below_high = (high_n - last_price) / atr if (pd.notna(atr) and atr > 0 and pd.notna(high_n)) else np.nan

        recent3_high = g[g["rank_desc"] <= 2]["High"].max()
        prior_high_ex3 = g[(g["rank_desc"] >= 3) & (g["rank_desc"] <= WINDOW_ADTV_HIGH - 1)]["High"].max()
        breakout = (pd.notna(recent3_high) and pd.notna(prior_high_ex3) and recent3_high > prior_high_ex3)

        last3_vol = g[g["rank_desc"] <= 2]["Volume"].sum()
        last3_freq = g[g["rank_desc"] <= 2]["Frequency"].sum()
        zero_recent = (last3_vol == 0 and last3_freq == 0)

        rows.append(dict(
            Ticker=code, Name=last["StockName"], Sector=sector_map.get(code, "Unclassified"),
            LastPrice=float(last_price), LotCost=float(last_price) * 100,
            ADTV20=float(adtv) if pd.notna(adtv) else None,
            ATR14_pct=float(atr_pct) if pd.notna(atr_pct) else None,
            High20=float(high_n) if pd.notna(high_n) else None,
            DistToHigh20=float(dist_to_high) if pd.notna(dist_to_high) else None,
            ATRsBelowHigh=float(atrs_below_high) if pd.notna(atrs_below_high) else None,
            BreakoutLast3=bool(breakout), ZeroTradeLast3=bool(zero_recent),
        ))

    df = pd.DataFrame(rows)
    df["F1_Liquidity"] = df["ADTV20"] >= MIN_ADTV
    df["F2_PriceRange"] = df["LastPrice"].between(PRICE_MIN, PRICE_MAX)
    # F3, corrected: the two OR-branches used to both fire once BreakoutLast3 was TRUE,
    # because High20 already incorporates whatever high was just set in the last 3
    # sessions -- so DistToHigh20<=5% was true for almost any breakout anyway, and
    # contributed nothing. Now each branch is gated on a DIFFERENT setup:
    #   - no breakout yet: still consolidating within 5% of the old high (DistToHigh20)
    #   - breakout fired: require the close to still be holding near the NEW high,
    #     within MAX_ATRS_BELOW_HIGH average daily ranges, instead of having fully
    #     round-tripped back down (the "failed breakout" pattern).
    df["F3_VolMomentum"] = (df["ATR14_pct"] >= MIN_ATR_PCT) & (
        (df["BreakoutLast3"] & (df["ATRsBelowHigh"] <= MAX_ATRS_BELOW_HIGH)) |
        (~df["BreakoutLast3"] & (df["DistToHigh20"] <= MAX_DIST_TO_HIGH))
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

    df_sorted = df.sort_values("ADTV20", ascending=False, na_position="last")  # universe order; ADTV20 is only used to check F1, not to rank passers
    passers = df[df["PassesAll"]].copy()

    # Rank on the screen's own dimensions, not on ADTV20 (which just restates F1 and,
    # with an >8x spread across the passing set, swamped everything else in the old
    # ranking). Primary key: ATRsBelowHigh ascending (closest to/above its own
    # volatility-scaled high first = cleanest setup). Tiebreak: ATR14_pct descending
    # (stronger move wins between two equally "clean" setups). This is one defensible
    # choice, not a market fact -- ATR14_pct-first is an equally reasonable alternative
    # if you'd rather prioritize raw momentum over cleanliness.
    passers = passers.sort_values(["ATRsBelowHigh", "ATR14_pct"], ascending=[True, False],
                                   na_position="last").reset_index(drop=True)

    apply_cap = bool(passers["Sector"].ne("Unclassified").any()) if len(passers) else False
    if apply_cap:
        passers["SectorRank"] = passers.groupby("Sector").cumcount() + 1
        passers["IncludedInShortlist"] = passers["SectorRank"] <= MAX_PER_SECTOR
    else:
        passers["SectorRank"] = np.arange(1, len(passers) + 1)
        passers["IncludedInShortlist"] = True  # no usable sector data this run -- cap not applied, see caveat

    shortlist = passers[passers["IncludedInShortlist"]].copy()

    # Near-misses are a diagnostic ("who almost qualified"), not the ranked pass list --
    # ADTV20 is a defensible sort key here (biggest names worth a manual look), and it's
    # not restating a gate they passed since these tickers failed exactly one filter.
    # Always computed now (previously only when pass_count < 3), so a high-pass day still
    # surfaces the next tier down instead of leaving this tab silently empty.
    fail_count = (~df_sorted[["F1_Liquidity", "F2_PriceRange", "F3_VolMomentum",
                               "F4_NotZeroTradeFlag", "F5_NoPendingCA"]]).sum(axis=1)
    near_misses = df_sorted[(fail_count == 1)].head(10).copy()

    # Sector breakdown: universe / passed / shortlisted counts per IDX-IC sector, so a
    # 50-pass day is legible even without listing all 50 (Sector_Breakdown tab) --
    # and the full 50 are still available in full in the All_Passers tab.
    sector_breakdown = (
        df.groupby("Sector")
        .agg(Universe=("Ticker", "count"), Passed=("PassesAll", "sum"))
        .reset_index()
    )
    # NB: the empty-shortlist fallback needs BOTH a value name ("Shortlisted", so pandas
    # doesn't raise "Cannot merge a Series without a name") AND an index named "Sector" (so
    # merge(on="Sector") has something to join against -- otherwise it raises KeyError:
    # 'Sector' instead). The non-empty branch gets both for free from groupby("Sector").size();
    # this only surfaces on the first zero-shortlist day, which a single live run is unlikely
    # to hit but 300+ backfilled days will eventually land on.
    shortlist_counts = (shortlist.groupby("Sector").size().rename("Shortlisted") if len(shortlist)
                         else pd.Series(dtype=int, name="Shortlisted", index=pd.Index([], name="Sector")))
    sector_breakdown = sector_breakdown.merge(shortlist_counts, on="Sector", how="left")
    sector_breakdown["Shortlisted"] = sector_breakdown["Shortlisted"].fillna(0).astype(int)
    sector_breakdown = sector_breakdown.sort_values("Passed", ascending=False).reset_index(drop=True)

    caveats = [
        ca_caveat,
        "F4 (ARA/ARB/UMA) uses a same-day zero-volume proxy, not IDX's official suspension/UMA feed -- verify manually before acting.",
        sector_caveat,
    ]
    if len(passers) and not apply_cap:
        caveats.append(f"Sector cap NOT applied this run ({len(passers)} passers all 'Unclassified') -- see sector caveat above.")
    if as_of_date is not None:
        caveats.append(
            "BACKFILLED ROW: this replays the CURRENT screen logic against historical price data, but "
            "F5 and Sector were checked against TODAY's corporate_actions/financial_ratios snapshot, not "
            "what was known as of this date -- a model of what the logic would flag today, not a "
            "point-in-time fact of what it would have flagged then."
        )

    return dict(
        df=df, shortlist=shortlist, all_passers=passers, near_misses=near_misses,
        sector_breakdown=sector_breakdown, last_date=last_date, caveats=caveats,
    )


def to_shortlist_records(shortlist_df, source="Live"):
    out = []
    for _, r in shortlist_df.iterrows():
        out.append(dict(
            Ticker=r["Ticker"], Name=r.get("Name", ""), Sector=r.get("Sector", "Unclassified"),
            LastPrice=r["LastPrice"], LotCost=r["LotCost"], ADTV20=r["ADTV20"],
            ATR14_pct=round(r["ATR14_pct"], 4) if pd.notna(r["ATR14_pct"]) else None,
            High20=r["High20"] if pd.notna(r["High20"]) else None,  # the raw 20-day-high price DistToHigh20/ATRsBelowHigh are both computed from
            DistToHigh20=round(r["DistToHigh20"], 4) if pd.notna(r["DistToHigh20"]) else None,
            ATRsBelowHigh=round(r["ATRsBelowHigh"], 3) if pd.notna(r["ATRsBelowHigh"]) else None,
            BreakoutLast3=bool(r["BreakoutLast3"]),
            Source=source,  # "Live" = real daily run, "Backfill" = historical replay (see caveats)
        ))
    return out


def to_all_passers_records(passers_df):
    out = []
    for _, r in passers_df.iterrows():
        out.append(dict(
            Ticker=r["Ticker"], Name=r.get("Name", ""), Sector=r.get("Sector", "Unclassified"),
            LastPrice=r["LastPrice"], ADTV20=r["ADTV20"],
            ATR14_pct=round(r["ATR14_pct"], 4) if pd.notna(r["ATR14_pct"]) else None,
            High20=r["High20"] if pd.notna(r["High20"]) else None,
            DistToHigh20=round(r["DistToHigh20"], 4) if pd.notna(r["DistToHigh20"]) else None,
            ATRsBelowHigh=round(r["ATRsBelowHigh"], 3) if pd.notna(r["ATRsBelowHigh"]) else None,
            BreakoutLast3=bool(r["BreakoutLast3"]),
            IncludedInShortlist=bool(r["IncludedInShortlist"]),
        ))
    return out


def to_all_tickers_records(df, shortlist_tickers):
    """Every ticker in the day's universe (~950-980), pass or fail, with each of the 5
    filters' individual TRUE/FALSE outcome -- lets you see WHY a name isn't in
    All_Passers/Latest_Top8, not just that it isn't. Live-run only (see build_payload):
    backfill_screen_log.py already posts 30+ days per HTTP call, and a ~980-row universe
    per day would balloon that payload for no benefit -- nothing reads a historical
    per-ticker universe today, only the shortlist (Screen_Log)."""
    out = []
    for _, r in df.iterrows():
        out.append(dict(
            Ticker=r["Ticker"], Name=r.get("Name", ""), Sector=r.get("Sector", "Unclassified"),
            LastPrice=r["LastPrice"],
            ADTV20=r["ADTV20"] if pd.notna(r["ADTV20"]) else None,
            ATR14_pct=round(r["ATR14_pct"], 4) if pd.notna(r["ATR14_pct"]) else None,
            High20=r["High20"] if pd.notna(r["High20"]) else None,
            DistToHigh20=round(r["DistToHigh20"], 4) if pd.notna(r["DistToHigh20"]) else None,
            ATRsBelowHigh=round(r["ATRsBelowHigh"], 3) if pd.notna(r["ATRsBelowHigh"]) else None,
            BreakoutLast3=bool(r["BreakoutLast3"]),
            F1_Liquidity=bool(r["F1_Liquidity"]), F2_PriceRange=bool(r["F2_PriceRange"]),
            F3_VolMomentum=bool(r["F3_VolMomentum"]), F4_NotZeroTradeFlag=bool(r["F4_NotZeroTradeFlag"]),
            F5_NoPendingCA=bool(r["F5_NoPendingCA"]), PassesAll=bool(r["PassesAll"]),
            IncludedInShortlist=bool(r["Ticker"] in shortlist_tickers),
        ))
    return out


def failed_filters(row):
    names = {"F1_Liquidity": "Liquidity", "F2_PriceRange": "Price range", "F3_VolMomentum": "Vol+Momentum",
             "F4_NotZeroTradeFlag": "Not zero-trade(possible suspension)", "F5_NoPendingCA": "No pending CA"}
    return [label for key, label in names.items() if not bool(row[key])]


def build_payload(cfg, as_of_date=None, run_date=None, source="Live"):
    """Runs the screen and shapes it into the JSON body doPost expects. Shared by the live
    daily run (as_of_date=None -> uses all available data, run_date=today) and
    backfill_screen_log.py (as_of_date=<historical date>, run_date=<that same date>,
    source="Backfill")."""
    r = run_screen(as_of_date=as_of_date)
    df, shortlist, all_passers, near_misses = r["df"], r["shortlist"], r["all_passers"], r["near_misses"]
    run_date = run_date or datetime.date.today().isoformat()

    payload = {
        "secret": cfg.get("shared_secret", ""),
        "run_date": run_date,
        "last_trading_date": pd.Timestamp(r["last_date"]).date().isoformat(),
        "universe_count": int(len(df)),
        "pass_count": int(df["PassesAll"].sum()),
        "top": to_shortlist_records(shortlist, source=source),
        "all_passers": to_all_passers_records(all_passers),
        "sector_breakdown": [
            {"Sector": row["Sector"], "Universe": int(row["Universe"]), "Passed": int(row["Passed"]),
             "Shortlisted": int(row["Shortlisted"])}
            for _, row in r["sector_breakdown"].iterrows()
        ],
        "near_misses": [
            {"Ticker": row["Ticker"], "Name": row.get("Name", ""), "ADTV20": row["ADTV20"],
             "FailedFilters": failed_filters(row)}
            for _, row in near_misses.iterrows()
        ] if len(near_misses) else [],
        "caveats": r["caveats"],
    }
    # Full universe (pass + fail, with the reason) -- live-run only. See
    # to_all_tickers_records()'s docstring for why backfill_screen_log.py skips this field
    # entirely (it never sets source="Live") rather than sending an empty/huge version of it.
    if source == "Live":
        payload["all_tickers"] = to_all_tickers_records(df, set(shortlist["Ticker"]))
    return payload


def post_payload(cfg, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cfg["webapp_url"], data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


def main():
    cfg = load_config()
    payload = build_payload(cfg)
    print(json.dumps(payload, indent=2, default=str))
    try:
        status, body = post_payload(cfg, payload)
        print("Web app response:", status, body)
    except urllib.error.HTTPError as e:
        print("HTTP error posting to sheet:", e.code, e.read().decode("utf-8"))
        sys.exit(1)
    except Exception as e:
        print("Error posting to sheet:", repr(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
