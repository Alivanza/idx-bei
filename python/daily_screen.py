"""
Daily IDX swing screener -> posts results to a Google Sheet via Apps Script Web App.

Run this AFTER `uv run idx daily` and `uv run idx corporate` have refreshed
data/parquet/stock_summary.parquet and data/parquet/corporate_actions.parquet.
Sector classification (used for the per-sector shortlist cap) comes from
data/parquet/financial_ratios.parquet, refreshed by `uv run idx financial` +
`uv run idx parquet` -- see the note above FIN_RATIOS_PATH below. NOTE: `idx financial` is fixed to
the FY2024 Q4 statements (fetch_financial_ratios defaults year=2024, quarter=4), so it refreshes sector
LABELS only; it does not advance the newest fsDate and adds no tickers listed since.

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

# ---- Measurement-only additions (03/11/2026) -------------------------------------------
# NOTHING below feeds F1-F5, PassesAll, the sector cap or the Shortlist ranking. These are
# context columns to read, not gates. (Near_Misses ordering is the one display-order change;
# it is a diagnostic tab, not the shortlist.)
ATR_SPIKE_CAP_MULT = 2.0        # cap each session's True Range at this multiple of the 14-session median TR
ATR_LOW_CONFIDENCE_TICKS = 10   # ATR14 smaller than this many price ticks is dominated by tick rounding
SECTOR_STALE_DAYS = 180         # warn in Run_Info when the sector snapshot is older than this
RECENT_ARA_WINDOW = 5           # sessions counted by ARA_days_last5

# IDX auto-rejection ATAS (ARA) regimes, keyed by the date the regime takes effect. Each band is
# (upper bound of the PREVIOUS close, inclusive; kind; value). Source: BEI Directors' Decision
# Kep-00136/BEI/09-2026 (21/09/2026), as reported by Katadata and Detik: from 28/09/2026 the
# minimum price falls from Rp50 to Rp1 and prices Rp1-Rp10 move by an absolute Rp1; the percentage
# bands for Rp11+ are unchanged (35% up to Rp200 inclusive, 25% up to Rp5,000 inclusive, 20% above).
# ARB is deliberately NOT modelled here: it changes on several dates (28/09, 19/10, 1/1/2027) and
# one of those (19-20/10) I could not confirm from a readable source.
ARA_REGIMES = [
    (pd.Timestamp("1900-01-01"), [(200, "pct", 0.35), (5000, "pct", 0.25), (np.inf, "pct", 0.20)]),
    (pd.Timestamp("2026-09-28"), [(10, "abs", 1.0), (200, "pct", 0.35), (5000, "pct", 0.25), (np.inf, "pct", 0.20)]),
]
# Not modelled (not in stock_summary.parquet): Special Monitoring Board / full-call-auction names and
# IPO first-day limits. They use different limits, so ClosedAtARA can under-count on those names.


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
              f"That file is a periodic snapshot, not refreshed by the daily job -- tickers listed after that "
              f"statement period show as 'Unclassified'; re-running `idx financial` does not add them (it is "
              f"fixed to FY2024 Q4).")
    return sector_map, caveat


def sector_staleness_caveat(reference_date, n_unclassified=None, n_universe=None,
                            n_unclassified_passers=None, n_passers=None):
    """Returns a WARNING string when the sector snapshot (financial_ratios.parquet) is older than
    SECTOR_STALE_DAYS relative to `reference_date` (age = reference_date minus the newest fsDate in
    the file), else None. `uv run idx financial` + `uv run idx parquet` refreshes sector LABELS for
    the same FY2024 Q4 statements (the command is fixed to that period): it can change sector
    assignments and therefore the 2-per-sector shortlist cap, but it does not move the newest fsDate
    and adds no new listings. The optional counts say how many tickers are 'Unclassified' (all of
    them share one cap group of MAX_PER_SECTOR)."""
    if not os.path.exists(FIN_RATIOS_PATH):
        return None
    fr = pd.read_parquet(FIN_RATIOS_PATH, columns=["fsDate"])
    latest_fs = pd.to_datetime(fr["fsDate"], errors="coerce").max()
    if pd.isna(latest_fs):
        return None
    ref = pd.Timestamp(reference_date)
    fs_age = (ref - latest_fs).days
    if fs_age <= SECTOR_STALE_DAYS:
        return None
    # The file's modification time is deliberately NOT used: `uv run idx parquet` rewrites every
    # parquet on each run, so mtime says "recently written", not "recently sourced".
    uncl = ""
    if n_unclassified is not None and n_universe is not None:
        uncl = f": {n_unclassified} of {n_universe} universe tickers"
        if n_unclassified_passers is not None and n_passers is not None:
            uncl += f", {n_unclassified_passers} of today's {n_passers} passers"
        uncl += f", all sharing one cap group of {MAX_PER_SECTOR}"
    return (f"WARNING - STALE SECTOR SNAPSHOT: newest financial-statement date in financial_ratios.parquet is "
            f"{latest_fs.date()} ({fs_age} days before {ref.date()}, limit {SECTOR_STALE_DAYS}). The 2-per-sector "
            f"cap uses this classification, so a name that changed sector, or a newly listed one ('Unclassified'{uncl}), "
            f"is capped on stale data. `uv run idx financial` + `uv run idx parquet` refreshes sector LABELS only: "
            f"the command is fixed to the FY2024 Q4 statements, so it does not change this date and adds no new "
            f"listings (clearing the warning needs a later reporting period). If you re-source, diff the shortlist "
            f"before and after (a changed sector changes who survives the cap).")


def tick_size(price):
    """IDX price fraction (fraksi harga) for equities: Rp1 below Rp200, Rp2 from Rp200 to below Rp500,
    Rp5 from Rp500 to below Rp2,000, Rp10 from Rp2,000 to below Rp5,000, Rp25 from Rp5,000. Lower
    bounds are INCLUSIVE (a price of exactly Rp200 has a Rp2 tick). Standard IDX table; I could not
    re-confirm it against a readable source for the 28/09/2026 Rp1-minimum change."""
    p = np.asarray(price, dtype=float)
    t = np.select([p < 200, p < 500, p < 2000, p < 5000], [1.0, 2.0, 5.0, 10.0], default=25.0)
    return np.where(np.isnan(p), np.nan, t)


def ara_price(prev_close, dates):
    """Upper price limit (ARA) for each row from the PREVIOUS close and the trading date, using the
    regime in force on that date (ARA_REGIMES). Percentage limits are rounded DOWN to the tick of the
    resulting price. Checked against 329,631 stock-days in stock_summary.parquet: no High exceeds
    this value, and 2,055 closes land exactly on it."""
    prev = np.asarray(prev_close, dtype=float)
    dts = pd.to_datetime(np.asarray(dates)).values
    out = np.full(prev.shape, np.nan)
    for i, (eff, bands) in enumerate(ARA_REGIMES):
        nxt = ARA_REGIMES[i + 1][0] if i + 1 < len(ARA_REGIMES) else None
        in_regime = dts >= np.datetime64(eff)
        if nxt is not None:
            in_regime &= dts < np.datetime64(nxt)
        lo = -np.inf
        for hi, kind, val in bands:
            m = in_regime & (prev > lo) & (prev <= hi)
            if kind == "abs":
                out = np.where(m, prev + val, out)
            else:
                raw = prev * (1.0 + val)
                t = tick_size(raw)
                out = np.where(m, np.floor(raw / t + 1e-9) * t, out)
            lo = hi
    return out


_BENCH_CACHE = {}


def load_benchmark_returns():
    """{Timestamp: daily return of ^JKSE}. Primary source: IDX COMPOSITE in index_summary.parquet
    (Close/Previous - 1), which equals Yahoo's ^JKSE to about 1e-8 over the 20 overlapping days.
    index_summary.parquet only holds 14/09/2026 onward and is missing 12-16/10/2026, so those days,
    and everything before 14/09/2026, fall back to ^JKSE close-to-close from
    backtest_kit/idx_price_history.csv when that file exists. Days in neither source are absent
    (EntryDayExcess is then blank, not guessed)."""
    if "r" in _BENCH_CACHE:
        return _BENCH_CACHE["r"]
    rets = {}
    csv_path = os.path.abspath(os.path.join(HERE, "..", "backtest_kit", "idx_price_history.csv"))
    if os.path.exists(csv_path):
        try:
            ph = pd.read_csv(csv_path, usecols=["Ticker", "Date", "Close"], parse_dates=["Date"])
            j = ph[ph["Ticker"] == "^JKSE"].sort_values("Date")
            r = j["Close"].pct_change()
            rets.update({pd.Timestamp(d): float(v) for d, v in zip(j["Date"], r) if pd.notna(v)})
        except Exception as e:  # optional input: never let it break the screen
            print(f"NOTE: could not read ^JKSE fallback from {csv_path}: {e!r}")
    ix_path = os.path.join(PARQUET_DIR, "index_summary.parquet")
    if os.path.exists(ix_path):
        ix = pd.read_parquet(ix_path, columns=["Date", "IndexCode", "Close", "Previous"])
        c = ix[ix["IndexCode"] == "COMPOSITE"]
        for d, cl, pv in zip(c["Date"], c["Close"], c["Previous"]):
            if pd.notna(cl) and pd.notna(pv) and pv:
                rets[pd.Timestamp(d)] = float(cl / pv - 1.0)  # IDX value wins where both exist
    _BENCH_CACHE["r"] = rets
    return rets


def miss_distance(r):
    """How far a ticker that failed EXACTLY ONE filter is from passing it: (distance, detail).
    Distance is the relative shortfall against the threshold, so 0.10 means 10% short of it:
      F1  (MIN_ADTV - ADTV20) / MIN_ADTV
      F2  distance outside [PRICE_MIN, PRICE_MAX], relative to the bound crossed
      F3  the larger of (ATR shortfall vs MIN_ATR_PCT) and (the branch-specific miss: ATRsBelowHigh
          vs MAX_ATRS_BELOW_HIGH if a breakout fired, else DistToHigh20 vs MAX_DIST_TO_HIGH)
      F4/F5  binary flags with no continuum -> distance None (ranked after the continuous misses).
    One defensible normalisation, not a market fact: relative shortfalls of different filters are
    not strictly comparable."""
    if not bool(r["F1_Liquidity"]):
        a = r["ADTV20"]
        if pd.isna(a):
            return None, "ADTV20 missing"
        return (MIN_ADTV - a) / MIN_ADTV, f"ADTV20 {a/1e9:.2f}bn vs {MIN_ADTV/1e9:.2f}bn"
    if not bool(r["F2_PriceRange"]):
        p = r["LastPrice"]
        if p < PRICE_MIN:
            return (PRICE_MIN - p) / PRICE_MIN, f"price {p:,.0f} below {PRICE_MIN}"
        return (p - PRICE_MAX) / PRICE_MAX, f"price {p:,.0f} above {PRICE_MAX:,}"
    if not bool(r["F3_VolMomentum"]):
        parts, notes = [], []
        atr = r["ATR14_pct"]
        if pd.isna(atr):
            return None, "ATR14 missing"
        if atr < MIN_ATR_PCT:
            parts.append((MIN_ATR_PCT - atr) / MIN_ATR_PCT)
            notes.append(f"ATR14 {atr:.2%} vs {MIN_ATR_PCT:.0%}")
        if bool(r["BreakoutLast3"]):
            a = r["ATRsBelowHigh"]
            if pd.notna(a) and a > MAX_ATRS_BELOW_HIGH:
                parts.append((a - MAX_ATRS_BELOW_HIGH) / MAX_ATRS_BELOW_HIGH)
                notes.append(f"breakout but {a:.2f} ATRs below high vs {MAX_ATRS_BELOW_HIGH:g}")
        else:
            d = r["DistToHigh20"]
            if pd.notna(d) and d > MAX_DIST_TO_HIGH:
                parts.append((d - MAX_DIST_TO_HIGH) / MAX_DIST_TO_HIGH)
                notes.append(f"{d:.1%} below 20d high vs {MAX_DIST_TO_HIGH:.0%}")
        if not parts:
            return None, "F3 failed on missing inputs"
        return max(parts), "; ".join(notes)
    if not bool(r["F4_NotZeroTradeFlag"]):
        return None, "binary: no volume or frequency in last 3 sessions"
    return None, "binary: corporate action listing within the lookahead window"


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

    # ---- measurement-only per-row fields (see the block of constants near the top) ----
    # ARA ceiling for each session from that session's previous close, and whether the session
    # closed on it. "Closed at ARA" is a PRICE fact (Close equals the ceiling and is above the
    # previous close); "book locked" additionally needs the end-of-day order book to show no
    # offers and a bid sitting at the ceiling. HATM on 20/10/2026 closed at ARA (665) but was NOT
    # locked: 381,300 shares were still offered at 665 with the best bid back at 660.
    ss["ARA_px"] = ara_price(ss["Previous"].values, ss["Date"].values)
    ss["ClosedAtARA_row"] = ((ss["Close"] == ss["ARA_px"]) & (ss["Close"] > ss["Previous"])).fillna(False)
    ss["LockedAtARA_row"] = (ss["ClosedAtARA_row"] & (ss["Offer"].fillna(0) == 0) &
                             (ss["OfferVolume"].fillna(0) == 0) & (ss["Bid"] == ss["ARA_px"]) &
                             (ss["BidVolume"].fillna(0) > 0))
    bench = load_benchmark_returns()
    bench_ret = bench.get(pd.Timestamp(last_date))
    # The universe below is every ticker with ANY row on file (~980), but only ~963 have a row on
    # last_date; the rest are stale (delisted or long-suspended: their "last" row is an old date).
    # Same-day measurements (EntryDayExcess, ClosedAtARA, ARA_price...) are blank/False for those,
    # and "last 5 sessions" means the last 5 market dates, not the ticker's last 5 rows.
    last_sessions = sorted(ss["Date"].unique())[-RECENT_ARA_WINDOW:]

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

        # EntryDayExcess: the stock's return on the screen date minus ^JKSE's return that day
        # (Close[D]/Close[D-1] - 1 for the stock). A fraction, shown as a %: 0.0375 = +3.75pp.
        # Context for reading a setup, NOT a filter (gating on it did not improve mean excess
        # return: 20d 5.48 -> 5.76pp with an ede<10pp gate, inside the CI).
        on_last_date = pd.Timestamp(last["Date"]) == pd.Timestamp(last_date)
        prev_close = last["PrevClose"]
        ede = ((last_price / prev_close - 1.0) - bench_ret) if (on_last_date and pd.notna(prev_close) and prev_close and bench_ret is not None) else np.nan

        # ATR spike decomposition: how much of ATR14 is one or two outsized sessions. Each session's
        # True Range is capped at ATR_SPIKE_CAP_MULT x the median TR of the same 14 sessions.
        tr_vals = win_atr["TrueRange"].values
        atr_capped_pct = spike_share = spike_days = np.nan
        if len(tr_vals) >= WINDOW_ATR and pd.notna(atr) and atr > 0:
            med_tr = float(np.median(tr_vals))
            if med_tr > 0:  # median of 0 means more than half the sessions had no range: cap is meaningless
                cap = ATR_SPIKE_CAP_MULT * med_tr
                capped_mean = float(np.minimum(tr_vals, cap).mean())
                atr_capped_pct = capped_mean / last_price
                spike_share = 1.0 - capped_mean / atr
                spike_days = int((tr_vals > cap).sum())

        # ATR in price ticks: below ATR_LOW_CONFIDENCE_TICKS the ATR is mostly tick rounding.
        atr_ticks = (atr / float(tick_size(last_price))) if (pd.notna(atr) and atr > 0) else np.nan

        recent_ara = g[g["Date"].isin(last_sessions)]
        rows.append(dict(
            Ticker=code, Name=last["StockName"], Sector=sector_map.get(code, "Unclassified"),
            LastPrice=float(last_price), LotCost=float(last_price) * 100,
            ADTV20=float(adtv) if pd.notna(adtv) else None,
            ATR14_pct=float(atr_pct) if pd.notna(atr_pct) else None,
            High20=float(high_n) if pd.notna(high_n) else None,
            DistToHigh20=float(dist_to_high) if pd.notna(dist_to_high) else None,
            ATRsBelowHigh=float(atrs_below_high) if pd.notna(atrs_below_high) else None,
            BreakoutLast3=bool(breakout), ZeroTradeLast3=bool(zero_recent),
            EntryDayExcess=float(ede) if pd.notna(ede) else None,
            ATR14_capped_pct=float(atr_capped_pct) if pd.notna(atr_capped_pct) else None,
            ATR_SpikeShare=float(spike_share) if pd.notna(spike_share) else None,
            ATR_SpikeDays=int(spike_days) if pd.notna(spike_days) else None,
            ATR_ticks=float(atr_ticks) if pd.notna(atr_ticks) else None,
            ATR_LowConfidence=bool(atr_ticks < ATR_LOW_CONFIDENCE_TICKS) if pd.notna(atr_ticks) else None,
            ARA_price=float(last["ARA_px"]) if (on_last_date and pd.notna(last["ARA_px"])) else None,
            ClosedAtARA=bool(last["ClosedAtARA_row"]) if on_last_date else False,
            BookLockedAtARA=bool(last["LockedAtARA_row"]) if on_last_date else False,
            ARA_days_last5=int(recent_ara["ClosedAtARA_row"].sum()),
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
    # Ranked by MissDistance (see miss_distance()): the tickers that are closest to passing their one
    # failed filter come first, instead of simply the largest by ADTV20. Binary misses (F4/F5) have
    # no distance and follow the continuous ones, ADTV20 descending, as before. This is a display
    # order for a diagnostic tab; it does not touch the shortlist.
    near_misses = df_sorted[(fail_count == 1)].copy()
    if len(near_misses):
        md = near_misses.apply(miss_distance, axis=1, result_type="expand")
        near_misses["MissDistance"] = md[0].astype(float)
        near_misses["MissDetail"] = md[1]
        near_misses["_binary"] = near_misses["MissDistance"].isna()
        near_misses = (near_misses.sort_values(["_binary", "MissDistance", "ADTV20"],
                                               ascending=[True, True, False], kind="stable")
                       .head(10).drop(columns="_binary"))
    else:
        near_misses["MissDistance"] = pd.Series(dtype=float)
        near_misses["MissDetail"] = pd.Series(dtype=object)

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
    if as_of_date is None:  # a historical replay uses today's snapshot by design (hindsight caveat below)
        stale = sector_staleness_caveat(
            last_date,
            n_unclassified=int((df["Sector"] == "Unclassified").sum()), n_universe=int(len(df)),
            n_unclassified_passers=int((passers["Sector"] == "Unclassified").sum()), n_passers=int(len(passers)),
        )
        if stale:
            caveats.insert(0, stale)
    # Standing caveat (item 2d of the 03/11/2026 audit): the regime split is not testable.
    caveats.append(
        "REGIME SPLIT NOT TESTABLE: short selling resumed 15/09/2026 (per BCA Sekuritas), but Backtest_Results only "
        "contains screen dates through 2026-09-10 (later days lack complete forward windows), so a before/after "
        "2026-10-01 comparison has no post-period sample. Read Backtest_Results as pre-resumption evidence only."
    )
    if bench_ret is None:
        caveats.append(f"EntryDayExcess blank this run: no ^JKSE return found for {pd.Timestamp(last_date).date()} "
                       f"in index_summary.parquet or backtest_kit/idx_price_history.csv.")
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


def _num(v, nd):
    return round(float(v), nd) if (v is not None and pd.notna(v)) else None


def measurement_fields(r):
    """The measurement-only columns, shaped for JSON (None for blank). Appended at the END of every
    tab so no existing column moves. Rounded like the neighbouring columns (fractions to 4 dp)."""
    return dict(
        EntryDayExcess=_num(r.get("EntryDayExcess"), 4),
        ATR14_capped_pct=_num(r.get("ATR14_capped_pct"), 4),
        ATR_SpikeShare=_num(r.get("ATR_SpikeShare"), 3),
        ATR_SpikeDays=int(r["ATR_SpikeDays"]) if pd.notna(r.get("ATR_SpikeDays")) else None,
        ATR_ticks=_num(r.get("ATR_ticks"), 2),
        ATR_LowConfidence=bool(r["ATR_LowConfidence"]) if pd.notna(r.get("ATR_LowConfidence")) else None,
        ARA_price=_num(r.get("ARA_price"), 2),
        ClosedAtARA=bool(r["ClosedAtARA"]),
        BookLockedAtARA=bool(r["BookLockedAtARA"]),
        ARA_days_last5=int(r["ARA_days_last5"]),
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
            **measurement_fields(r),
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
            **measurement_fields(r),
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
            **measurement_fields(r),
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
             "FailedFilters": failed_filters(row),
             "MissDistance": _num(row.get("MissDistance"), 4), "MissDetail": row.get("MissDetail", "")}
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
    # 120 s (was 30): the Apps Script post timed out client-side on 17/10/2026 while the sheet
    # stayed consistent, so the script exited 1 despite the data landing.
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status, resp.read().decode("utf-8")


def to_daily_prices_rows(df, date):
    """Builds [Ticker, Name, Date, Open, High, Low, Close, Volume] rows for one date --
    the same row shape DAILY_PRICES_HEADERS (AppsScript_Code.gs) and backfill_daily_prices.py
    already use for "Daily All Tickers". `df` is the raw stock_summary.parquet frame (not
    run_screen()'s enriched version), `date` a 'YYYY-MM-DD' string."""
    sub = df[df["Date"] == pd.Timestamp(date)].sort_values("StockCode")
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


def post_daily_prices(cfg, date):
    """POSTs one trading day's full-universe OHLCV to "Daily All Tickers" (mode=
    "prices_daily"). AppsScript_Code.gs's appendDailyPrices() has supported this since it
    was written -- comment there says "one call per trading day from daily_screen.py" -- but
    nothing in this pipeline actually called it until now, which is why that tab only ever
    had the one-time backfill_daily_prices.py load and then fell behind again immediately.
    Separate POST from the main screen payload; Apps Script dedupes by date server-side
    (deletes that date's existing rows before re-inserting), so safe to retry or re-run."""
    ss_path = os.path.join(PARQUET_DIR, "stock_summary.parquet")
    df = pd.read_parquet(ss_path)
    rows = to_daily_prices_rows(df, date)
    if not rows:
        print(f"post_daily_prices: no rows for {date} in stock_summary.parquet, skipping.")
        return
    body = {"secret": cfg.get("shared_secret", ""), "mode": "prices_daily", "rows": rows, "date": date}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(cfg["webapp_url"], data=data, headers={"Content-Type": "application/json"})
    # 330 s (was 180, originally 60): the Apps Script delete+append on the ~364k-row sheet took longer
    # than 180 s on 20/10/2026, so the client gave up ("read operation timed out") while the sheet
    # stayed correct. 330 s sits just under Apps Script's 6-minute execution cap.
    with urllib.request.urlopen(req, timeout=330) as resp:
        status, text = resp.status, resp.read().decode("utf-8")
    print(f"post_daily_prices ({date}): HTTP {status} {text.strip()}")


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

    # Keep "Daily All Tickers" current going forward. Non-fatal on its own: the main screen
    # payload above already posted successfully by this point, so a problem here (e.g. a
    # transient Apps Script timeout) shouldn't turn an otherwise-successful run red.
    try:
        post_daily_prices(cfg, payload["last_trading_date"])
    except Exception as e:
        print("Error posting daily prices to Daily All Tickers (non-fatal):", repr(e))


if __name__ == "__main__":
    main()
