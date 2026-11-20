"""Vectorised replay of daily_screen.run_screen over every historical date, for threshold sensitivity.

Mirrors run_screen(as_of_date=D) exactly:
  * per-ticker ROW-based windows (a ticker's last 20 / 14 / 3 rows), truncated at D
  * a ticker with no row on D keeps its last row <= D (run_screen's universe is every ticker with ANY row <= D)
  * F5 uses today's corporate_actions snapshot; Sector uses today's financial_ratios snapshot (same hindsight as Backfill rows)
  * ranking: ATRsBelowHigh asc, ATR14_pct desc, NaN last, stable on alphabetical ticker; sector cap 2, applied only
    if at least one passer has a classified sector that day.
"""
import numpy as np
import pandas as pd

BASE = dict(MIN_ADTV=5_000_000_000, PRICE_MIN=50, PRICE_MAX=3000, MIN_ATR_PCT=0.03, MAX_DIST=0.05, MAX_ATRS=1.0)
MAX_PER_SECTOR = 2


def load_panel(parq_dir):
    ss = pd.read_parquet(f"{parq_dir}/stock_summary.parquet",
                         columns=["Date", "StockCode", "High", "Low", "Close", "Volume", "Value", "Frequency"])
    ss = ss.sort_values(["StockCode", "Date"]).reset_index(drop=True)
    g = ss.groupby("StockCode", sort=True)
    ss["PrevClose"] = g["Close"].shift(1)
    ss["TR"] = np.maximum.reduce([(ss.High - ss.Low).values, (ss.High - ss.PrevClose).abs().values,
                                  (ss.Low - ss.PrevClose).abs().values])
    g = ss.groupby("StockCode", sort=True)
    rl = lambda col, w, f: getattr(g[col].rolling(w, min_periods=1), f)().reset_index(level=0, drop=True)
    ss["ADTV20"] = rl("Value", 20, "mean")
    ss["High20"] = rl("High", 20, "max")
    ss["ATR"] = rl("TR", 14, "mean")                                  # NaN TR ignored, like win_atr's notna filter
    ss["R3High"] = rl("High", 3, "max")
    ss["PriorHigh"] = g["High"].shift(3).groupby(ss["StockCode"]).rolling(17, min_periods=1).max().reset_index(level=0, drop=True)
    ss["V3"] = rl("Volume", 3, "sum"); ss["F3"] = rl("Frequency", 3, "sum")
    ss["Valid"] = ss.Close.notna() & (ss.Close != 0)
    P = lambda col: ss.pivot(index="Date", columns="StockCode", values=col)
    dates = sorted(ss.Date.unique())
    out = {}
    for c in ["Close", "ADTV20", "High20", "ATR", "R3High", "PriorHigh", "V3", "F3"]:
        out[c] = P(c).reindex(dates).ffill()
    out["Valid"] = P("Valid").reindex(dates).ffill().fillna(False).astype(bool)
    # a ticker's state on date D is its last row <= D; leading NaN (not yet listed) stays NaN / invalid
    return out, dates


def load_static(parq_dir):
    fr = pd.read_parquet(f"{parq_dir}/financial_ratios.parquet", columns=["code", "sector", "fsDate"])
    fr = fr.dropna(subset=["code"]).drop_duplicates(subset=["code"], keep="first")
    sector = dict(zip(fr["code"], fr["sector"].fillna("Unclassified")))
    ca = pd.read_parquet(f"{parq_dir}/corporate_actions.parquet")
    return sector, ca


def pending_ca(ca, dates, tickers):
    """bool matrix dates x tickers: True when a CA listing falls in [D, D + 10 BDay]."""
    out = pd.DataFrame(False, index=dates, columns=tickers)
    cc = ca[["KodeEmiten", "TanggalPencatatan"]].dropna()
    for d in dates:
        end = pd.Timestamp(d) + pd.tseries.offsets.BDay(10)
        hit = set(cc[(cc.TanggalPencatatan >= d) & (cc.TanggalPencatatan <= end)].KodeEmiten)
        hit &= set(tickers)
        if hit:
            out.loc[d, list(hit)] = True
    return out


def prepare(parq_dir, dates_wanted=None):
    pan, dates = load_panel(parq_dir)
    sector, ca = load_static(parq_dir)
    tickers = list(pan["Close"].columns)
    if dates_wanted is not None:
        dts = [d for d in dates if str(pd.Timestamp(d).date()) in dates_wanted]
    else:
        dts = dates
    pend = pending_ca(ca, dts, tickers)
    ctx = dict(pan=pan, dates=dts, tickers=tickers, sector=sector, pend=pend)
    # derived (config-independent) matrices on the wanted dates
    P = pan["Close"].loc[dts]; H20 = pan["High20"].loc[dts]; ATR = pan["ATR"].loc[dts]
    ctx["P"] = P
    ctx["ADTV"] = pan["ADTV20"].loc[dts]
    ctx["ATRpct"] = ATR / P
    ctx["Dist"] = (H20 - P) / H20
    ctx["ATRsBH"] = ((H20 - P) / ATR).where(ATR > 0)
    r3, ph = pan["R3High"].loc[dts], pan["PriorHigh"].loc[dts]
    ctx["Breakout"] = (r3.notna() & ph.notna() & (r3 > ph))
    ctx["Zero"] = (pan["V3"].loc[dts] == 0) & (pan["F3"].loc[dts] == 0)
    ctx["Valid"] = pan["Valid"].loc[dts]
    return ctx


def shortlists(ctx, cfg):
    """{date: [tickers in rank order]} for the shortlist under cfg."""
    P, ADTV, ATRp, Dist, ABH = ctx["P"], ctx["ADTV"], ctx["ATRpct"], ctx["Dist"], ctx["ATRsBH"]
    brk, zero, valid, pend = ctx["Breakout"], ctx["Zero"], ctx["Valid"], ctx["pend"]
    f1 = ADTV >= cfg["MIN_ADTV"]
    f2 = P.ge(cfg["PRICE_MIN"]) & P.le(cfg["PRICE_MAX"])
    f3 = (ATRp >= cfg["MIN_ATR_PCT"]) & ((brk & (ABH <= cfg["MAX_ATRS"])) | (~brk & (Dist <= cfg["MAX_DIST"])))
    f4 = ~zero
    f5 = ~pend
    ok = f1 & f2 & f3 & f4 & f5 & valid
    sector = ctx["sector"]
    tickers = ctx["tickers"]
    sec = np.array([sector.get(t, "Unclassified") for t in tickers])
    out = {}
    okv = ok.to_numpy(); abh = ABH.to_numpy(); atp = ATRp.to_numpy()
    for k, d in enumerate(ctx["dates"]):
        idx = np.flatnonzero(okv[k])
        if len(idx) == 0:
            out[d] = []; continue
        tmp = pd.DataFrame({"i": idx, "abh": abh[k, idx], "atp": atp[k, idx]})
        tmp = tmp.sort_values(["abh", "atp"], ascending=[True, False], na_position="last", kind="stable")
        ii = tmp["i"].to_numpy()
        if (sec[ii] != "Unclassified").any():
            s = pd.Series(sec[ii]); keep = (s.groupby(s).cumcount() + 1 <= MAX_PER_SECTOR).to_numpy()
            ii = ii[keep]
        out[d] = [tickers[i] for i in ii]
    return out
