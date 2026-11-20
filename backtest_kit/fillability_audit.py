"""
fillability_audit.py -- (1) were the Open(D+1) entries fillable?  (2) systematic extreme-row audit.

Run from backtest_kit/:  python fillability_audit.py [--parquet ../data/parquet] [--daily-screen ../python/daily_screen.py]
Needs idx_price_history.csv, screen_log_export.csv, backtest_analysis.py (same folder), pandas, numpy, pyarrow.
Writes fillability_rows.csv (one row per Backfill screen row x horizon with flags) and prints every table.

ENTRY-DAY FLAGS (entry session = the ticker's own row D+1; ARA from the exchange's Previous column and the
regime table in daily_screen.ara_price; Open = Yahoo Open(D+1), the price the headline assumes you paid):
  OpenAtARA    Open(D+1) >= ARA - one tick                        "opened at / within one tick of the upper limit"
  OpenAtARA_x  Open(D+1) >= ARA                                    strict version
  DayAtARA     Low(D+1)  >= ARA                                    the whole session traded at the limit (no print below it)
  ClosedAtARA  Close(D+1) == ARA and Close > Previous              (price fact, as daily_screen)
  LockedAtARA  ClosedAtARA and no offer / offer volume and a bid at the limit at the close   (book fact, as daily_screen)
What this can and cannot say: an Open at the limit means the first print was at the limit; whether a retail order would have
filled depends on queue position, which stock_summary does not contain.  Not modelled: Special Monitoring Board / IPO limits.
"""
import sys, bisect, argparse, importlib.util
import numpy as np, pandas as pd
sys.path.insert(0, ".")
import backtest_analysis as BA

H = BA.H; SEED = BA.SEED
ap = argparse.ArgumentParser()
ap.add_argument("--parquet", default="../data/parquet"); ap.add_argument("--daily-screen", default="../python/daily_screen.py")
ap.add_argument("--cap", default=BA.PX_CAP_MAIN); ap.add_argument("--out", default="fillability_rows.csv")
args, _ = ap.parse_known_args()

spec = importlib.util.spec_from_file_location("ds", args.daily_screen); DS = importlib.util.module_from_spec(spec); spec.loader.exec_module(DS)


def stats(d, n, col="net", seed_off=2):
    """date-level mean, NW t, MBB CI of one horizon's rows (d has Date, Ex_O1m, CostA)."""
    v = (d.Ex_O1m - d.CostA) if col == "net" else d.Ex_O1m
    x = v.groupby(d.Date).mean().sort_index().to_numpy()
    if len(x) < 5: return dict(N=len(d), T=len(x), mean=np.nan, t=np.nan, lo=np.nan, hi=np.nan)
    _, t = BA.nw_t(x, n - 1); lo, hi = BA.mbb_ci(x, n, np.random.default_rng(SEED + seed_off))
    return dict(N=len(d), T=len(x), mean=x.mean(), t=t, lo=lo, hi=hi)


def entry_flags(df):
    """one row per (Ticker, Date): flags for the entry session D+1."""
    px = pd.read_csv("idx_price_history.csv"); px = px[px.Date <= args.cap]
    Y = {t: g.sort_values("Date").reset_index(drop=True) for t, g in px.groupby("Ticker")}
    ss = pd.read_parquet(f"{args.parquet}/stock_summary.parquet",
                         columns=["Date", "StockCode", "OpenPrice", "High", "Low", "Close", "Previous", "Volume", "Bid", "BidVolume", "Offer", "OfferVolume"])
    ss["D"] = ss.Date.dt.strftime("%Y-%m-%d")
    ss["ARA"] = DS.ara_price(ss.Previous.values, ss.Date.values)
    ss = ss.set_index(["D", "StockCode"])
    keys = df[["Ticker", "Date"]].drop_duplicates()
    out = []
    for t, d in zip(keys.Ticker, keys.Date):
        g = Y[t]; i = bisect.bisect_left(g.Date.tolist(), d)
        if i + 1 >= len(g): continue
        e = g.Date.iloc[i + 1]; yo = g.Open.iloc[i + 1]
        r = ss.loc[(e, t)] if (e, t) in ss.index else None
        if r is None: out.append(dict(Ticker=t, Date=d, Entry=e, YOpen=yo)); continue
        ara = r.ARA; tk = float(DS.tick_size(ara)) if pd.notna(ara) else np.nan
        closed = bool(r.Close == ara and r.Close > r.Previous)
        locked = bool(closed and (pd.isna(r.Offer) or r.Offer == 0) and (pd.isna(r.OfferVolume) or r.OfferVolume == 0) and r.Bid == ara and (r.BidVolume or 0) > 0)
        out.append(dict(Ticker=t, Date=d, Entry=e, YOpen=yo, PqOpen=r.OpenPrice, EPrev=r.Previous, ARA=ara, EntryClose=r.Close, EHigh=r.High, ELow=r.Low, EVol=r.Volume,
                        OpenAtARA=bool(yo >= ara - tk), OpenAtARA_x=bool(yo >= ara), DayAtARA=bool(r.Low >= ara), ClosedAtARA=closed, LockedAtARA=locked))
    return pd.DataFrame(out)




# ----------------------------------------------------------------------------------------------------------------------
# Additional flags.  The standard ARA table (35% / 25% / 20%) misses Special-Monitoring-Board names, whose limit is 10%
# (RLCO: +9.9% a day for 12 sessions), so the lock test is also run without an ARA table:
#   F_lock    entry session traded at ONE price (High == Low) and closed >= +9% above the previous close
#   F_zerovol entry session had zero volume (nothing traded; no price exists)
#   F_noopen  IDX shows no opening trade (OpenPrice == 0): Yahoo's Open is then a placeholder, not a print
#   E1 = OpenAtARA or F_lock                 (limit-up entries: the first price you could have had was the limit, if any)
#   E2 = E1 or F_zerovol
#   E3 = E1 or F_noopen                      (the widest bound: every entry whose Open is not a real opening trade)
#   E4 = E1 or ClosedAtARA                   (also drops entries that opened lower and then ran to the limit)
# ----------------------------------------------------------------------------------------------------------------------
def add_flags(d):
    d = d.copy()
    d["F_open"] = d.OpenAtARA.astype(bool)
    d["F_lock"] = (d.EHigh == d.ELow) & (d.EntryClose / d.EPrev - 1 >= 0.09)
    d["F_zerovol"] = d.EVol == 0
    d["F_noopen"] = d.PqOpen == 0
    d["ClosedAtARA"] = d.ClosedAtARA.astype(bool); d["LockedAtARA"] = d.LockedAtARA.astype(bool)
    d["E1"] = d.F_open | d.F_lock; d["E2"] = d.E1 | d.F_zerovol; d["E3"] = d.E1 | d.F_noopen; d["E4"] = d.E1 | d.ClosedAtARA
    return d


def top_tickers(x, k=4):
    return ", ".join(f"{t} ({c})" for t, c in x.Ticker.value_counts().head(k).items())


def test1(d, add, f1, f2):
    u = d.drop_duplicates(["Ticker", "Date"]); NU = len(u)
    add("S", "10. FILLABILITY OF THE Open(D+1) ENTRY (Backfill rows; entry-session flags from stock_summary.parquet)")
    z = u[u.F_noopen]
    add("X", f"Question: could a buyer actually have paid the Open(D+1) price the headline assumes? Entry session = the ticker's own row D+1. Open(D+1) is Yahoo's Open. FLAGS: OpenAtARA = Open >= ARA - 1 tick (ARA from the exchange's Previous column and daily_screen.ara_price: 35% to Rp200, 25% to Rp5,000, 20% above). The ARA table does not know Special Monitoring Board names (limit 10%), so a second lock test is run without it: F_lock = the session traded at a single price (High = Low) and closed >= +9% above the previous close. F_zerovol = no volume that session. F_noopen = IDX shows NO opening trade (OpenPrice = 0): Yahoo's Open is then a placeholder, not a print; in those {len(z)} rows it equals the previous close in {100 * (z.YOpen == z.EPrev).mean():.0f}% and the entry-day close in {100 * (z.YOpen == z.EntryClose).mean():.0f}%, and {int(z.F_zerovol.sum())} of them ({100 * z.F_zerovol.mean():.0f}%) had no volume at all that session (no price existed). Every Yahoo Open on a session with volume lies inside the IDX Low-High range, so 'outside the range' adds no flag. EXCLUSION SETS: E1 = OpenAtARA or F_lock; E2 = E1 or F_zerovol; E3 = E1 or F_noopen (widest); E4 = E1 or closed at ARA. What this cannot say: whether a retail order at the limit would have been filled (queue position is not in the data); E1 is a bound, not a fill model.")
    add("Hd", "Flag", "Screen rows", "Tickers", "% of rows", "Most frequent tickers (rows)")
    for lab, c in (("OpenAtARA (Yahoo Open within 1 tick of ARA)", "F_open"), ("F_lock (single-price session, close >= +9%)", "F_lock"), ("E1 = OpenAtARA or F_lock", "E1"),
                   ("F_zerovol (no volume in the entry session)", "F_zerovol"), ("E2 = E1 or F_zerovol", "E2"), ("F_noopen (IDX OpenPrice = 0)", "F_noopen"),
                   ("E3 = E1 or F_noopen", "E3"), ("ClosedAtARA (entry session closed at the limit)", "ClosedAtARA"), ("LockedAtARA (limit-up, no offers, bid at the limit at the close)", "LockedAtARA"), ("E4 = E1 or ClosedAtARA", "E4")):
        x = u[u[c]]; add("R", lab, len(x), x.Ticker.nunique(), f1(100 * len(x) / NU), top_tickers(x))
    r = u[u.Ticker == "RLCO"]
    add("X", f"RLCO: {len(r)} screen rows; {int(r.F_lock.sum())} are single-price limit-up entries, {int(r.F_open.sum())} OpenAtARA, {int(r.F_zerovol.sum())} zero-volume, {int(r.F_noopen.sum())} had no opening trade. The standard ARA flag catches only {int(r.F_open.sum())} of them because RLCO was on a 10% limit (1,330 -> 1,460 -> 1,605 -> 1,765 -> 1,940 -> 2,130 -> 2,340 -> 2,570 -> 3,010 / 3,210).")
    add("B")
    add("Hd", "Contribution of E1 (limit-locked) entries to the date-level means | horizon", "Rows excluded", "Gross mean, all", "Gross mean, excl. E1", "Net A mean, all", "Net A mean, excl. E1", "Contribution (pp, = all minus excl.)", "Mean net A of the excluded rows (pp, row-level)", "Largest contributors (rows, mean net A)")
    for n in H:
        v = d[d.N == n].dropna(subset=["Ex_O1m"]); keep = ~v.E1
        g = lambda x, m: x[m].groupby(v.Date[m]).mean().mean()
        net = v.Ex_O1m - v.CostA; e = v[v.E1]; ne = net[v.E1]
        top = ne.groupby(e.Ticker).agg(["count", "sum", "mean"]).sort_values("sum", ascending=False).head(4)
        add("R", n, len(e), f2(g(v.Ex_O1m, v.E1 | keep)), f2(g(v.Ex_O1m, keep)), f2(g(net, v.E1 | keep)), f2(g(net, keep)), f2(g(net, v.E1 | keep) - g(net, keep)), f1(ne.mean()),
            ", ".join(f"{t} ({int(r_['count'])}, {r_['mean']:.0f})" for t, r_ in top.iterrows()))
    add("B")
    add("Hd", "Open(D+1) headline net of cost A with unfillable entries EXCLUDED | variant", "Horizon", "N rows", "Screen dates", "Date-level mean net A", "Newey-West t", "Block-bootstrap 95% CI lower", "Block-bootstrap 95% CI upper")
    variants = [("All rows (section 0)", lambda v: pd.Series(True, index=v.index)), ("Excl. RLCO", lambda v: v.Ticker != "RLCO"), ("Excl. E1 (limit-locked entries)", lambda v: ~v.E1),
                ("Excl. E2 (E1 + zero-volume sessions)", lambda v: ~v.E2), ("Excl. E3 (E1 + no opening trade)", lambda v: ~v.E3), ("Excl. E4 (E1 + closed at ARA)", lambda v: ~v.E4)]
    res = {}
    for lab, m in variants:
        for n in H:
            v = d[d.N == n].dropna(subset=["Ex_O1m"]); s_ = stats(v[m(v)], n); res[(lab, n)] = s_
            add("R", lab, n, s_["N"], s_["T"], f2(s_["mean"]), f2(s_["t"]), f2(s_["lo"]), f2(s_["hi"]))
    add("B")
    add("Hd", "Top 1% of rows by Open(D+1) gross excess | horizon", "Rows in top 1%", "Cut-off (excess >=, %)", "Contribution to row-level mean (pp)", "Row-level mean, all rows", "In E1", "In E1 (%)", "In E3 (no real opening trade)", "In E3 (%)", "Most frequent tickers")
    for n in H:
        v = d[d.N == n].dropna(subset=["Ex_O1m"]); k = int(np.ceil(0.01 * len(v))); t_ = v.nlargest(k, "Ex_O1m")
        add("R", n, k, f1(t_.Ex_O1m.min()), f2(t_.Ex_O1m.sum() / len(v)), f2(v.Ex_O1m.mean()), int(t_.E1.sum()), f1(100 * t_.E1.mean()), int(t_.E3.sum()), f1(100 * t_.E3.mean()), top_tickers(t_, 5))
    add("B")
    return res


def verify_rows(parquet, log_rows):
    """602-row style audit: rows whose raw Open(D+1)->Close(D+N) return exceeds +-50% at any horizon."""
    px = pd.read_csv("idx_price_history.csv"); px = px[px.Date <= args.cap]
    Y = {t: g.sort_values("Date").reset_index(drop=True) for t, g in px.groupby("Ticker")}
    ss = pd.read_parquet(f"{parquet}/stock_summary.parquet", columns=["Date", "StockCode", "OpenPrice", "Close", "Previous"])
    ss["D"] = ss.Date.dt.strftime("%Y-%m-%d"); ss["ARA"] = DS.ara_price(ss.Previous.values, ss.Date.values)
    P = {t: g.sort_values("D").reset_index(drop=True) for t, g in ss.groupby("StockCode")}
    ca = pd.read_parquet(f"{parquet}/corporate_actions.parquet")
    R = []
    for t, dte in log_rows:
        g = Y[t]; i = bisect.bisect_left(g.Date.tolist(), dte)
        if i + 1 >= len(g): continue
        o1 = g.Open.iloc[i + 1]; rec = dict(Ticker=t, Date=dte)
        for n in H: rec[f"r{n}"] = (g.Close.iloc[i + n] / o1 - 1) * 100 if i + n < len(g) else np.nan
        R.append(rec)
    R = pd.DataFrame(R); X = R[R[[f"r{n}" for n in H]].abs().max(axis=1) > 50].copy()
    out = []
    for _, r in X.iterrows():
        t, dte = r.Ticker, r.Date; g = Y[t]; i = bisect.bisect_left(g.Date.tolist(), dte); nmax = max(n for n in H if abs(r[f"r{n}"]) > 50)
        p = P[t]; j = bisect.bisect_left(p.D.tolist(), dte); raw = r[f"r{nmax}"]
        adj = (p.Previous / p.Close.shift(1)).iloc[j + 2:j + nmax + 1]; hit = ((adj - 1).abs() > 0.02)
        scale = float(np.prod(np.where(hit, adj, 1.0))); o1 = g.Open.iloc[i + 1]
        scale_e = p.Close.iloc[j + 1] / g.Close.iloc[i + 1]
        idx_ret = (p.Close.iloc[j + nmax] / scale / (o1 * scale_e) - 1) * 100 if o1 > 0 else np.nan
        moves = (p.Close / p.Close.shift(1) - 1).iloc[j + 2:j + nmax + 1]; lim = (p.ARA / p.Previous - 1).iloc[j + 2:j + nmax + 1]
        over = int(((moves.abs() > lim + 0.005) & ~hit).sum()); open_real = bool(p.OpenPrice.iloc[j + 1] > 0)
        agree = bool(abs(idx_ret - raw) < 5) if pd.notna(idx_ret) else False
        c = ca[(ca.KodeEmiten == t) & (ca.TanggalPencatatan >= p.D.iloc[j + 1]) & (ca.TanggalPencatatan <= p.D.iloc[j + nmax]) & (ca.caType != "ipo")]
        cas = ",".join(sorted(set(c.caType.astype(str))))
        a = bool(hit.any())
        if a and (abs(idx_ret) < 50 or np.sign(idx_ret) != np.sign(raw)):
            status, why = "verified-artifact", f"exchange price adjustment inside the window; restated return {idx_ret:+.0f}% vs raw {raw:+.0f}%"
        else:
            probs = [x for x in [("exchange price adjustment inside window (ex-rights type); raw return understates, extremeness not caused by it" if a else ""),
                                 (f"{over} daily close moves beyond the exchange limit" if over else ""),
                                 (f"Yahoo-based and IDX-based returns differ by {abs(idx_ret - raw):.0f}pp" if not agree else ""),
                                 (f"corporate action recorded in window: {cas}" if cas else "")] if x]
            status, why = ("unchecked", "; ".join(probs)) if probs else ("verified-real", "")
        out.append(dict(status=status, reason=why, trips_previous_test=a, restated_return=idx_ret, max_horizon_tripping=nmax, daily_moves_over_limit=over, entry_has_opening_trade=open_real, corp_action=cas))
    X = pd.concat([X.reset_index(drop=True), pd.DataFrame(out)], axis=1)
    X["max_abs_return"] = X[[f"r{n}" for n in H]].abs().max(axis=1)
    return R, X


def winsor_trim(d, add, f2):
    add("Hd", "Open(D+1) net A, row-level winsorized / trimmed at the 1st and 99th percentile, then date-level | horizon", "P1 of row net A (pp)", "P99 of row net A (pp)", "Raw date-level mean", "Raw NW t", "Winsorized mean", "Winsorized NW t", "Winsorized CI lower", "Winsorized CI upper", "Trimmed mean", "Trimmed NW t", "Trimmed CI lower", "Trimmed CI upper", "Row-level median")
    out = {}
    for n in H:
        v = d[d.N == n].dropna(subset=["Ex_O1m"]).copy(); v["net"] = v.Ex_O1m - v.CostA; lo, hi = np.percentile(v.net, [1, 99])
        def S(df):
            x = df.net.groupby(df.Date).mean().sort_index().to_numpy(); ci = BA.mbb_ci(x, n, np.random.default_rng(SEED + 2)); return x.mean(), BA.nw_t(x, n - 1)[1], ci[0], ci[1]
        w = v.copy(); w["net"] = w.net.clip(lo, hi); rs, ws, ts = S(v), S(w), S(v[(v.net >= lo) & (v.net <= hi)]); out[n] = (rs, ws, ts)
        add("R", n, f2(lo), f2(hi), f2(rs[0]), f2(rs[1]), f2(ws[0]), f2(ws[1]), f2(ws[2]), f2(ws[3]), f2(ts[0]), f2(ts[1]), f2(ts[2]), f2(ts[3]), f2(v.net.median()))
    add("B")
    return out


def reconcile(d, add, f2):
    px_ss = pd.read_parquet(f"{args.parquet}/stock_summary.parquet", columns=["Date", "StockCode", "Close", "Previous"]); px_ss["D"] = px_ss.Date.dt.strftime("%Y-%m-%d")
    P = {t: g.sort_values("D").reset_index(drop=True) for t, g in px_ss.groupby("StockCode")}
    def crosses(t, dte, n):
        p = P[t]; j = bisect.bisect_left(p.D.tolist(), dte)
        return bool((((p.Previous / p.Close.shift(1)).iloc[j + 2:j + n + 1]) - 1).abs().gt(0.02).any())
    d = d.copy(); d["ADJ"] = [crosses(t, dt, n) if pd.notna(e) else False for t, dt, n, e in zip(d.Ticker, d.Date, d.N, d.Ex_O1m)]
    add("Hd", "Rows whose window crosses an exchange price adjustment (|Previous / prior Close - 1| > 2%) | horizon", "Rows crossing", "of which PACK", "Other rows", "Net A mean, all", "Net A, excl. PACK", "Net A, excl. all crossing", "PACK effect (pp)", "Other rows' effect (pp)", "Total effect (pp)")
    for n in H:
        v = d[d.N == n].dropna(subset=["Ex_O1m"]); net = v.Ex_O1m - v.CostA; g = lambda m: net[m].groupby(v.Date[m]).mean().mean()
        pk = (v.Ticker == "PACK") & v.ADJ; a, p_, x = g(pd.Series(True, index=v.index)), g(~pk), g(~v.ADJ)
        add("R", n, int(v.ADJ.sum()), int(pk.sum()), int((v.ADJ & ~pk).sum()), f2(a), f2(p_), f2(x), round(p_ - a, 3), round(x - p_, 3), round(x - a, 3))
    add("B")


def test2(d, add, f1, f2):
    log = pd.read_csv("screen_log_export.csv", dtype=str); bf = log[log.Source == "Backfill"].drop_duplicates(["LastTradingDate", "Ticker"])
    R, X = verify_rows(args.parquet, list(zip(bf.Ticker, bf.LastTradingDate)))
    add("S", "11. EXTREME-ROW AUDIT (Backfill rows; raw gross return Open(D+1) to Close(D+N), before the benchmark)")
    add("X", f"{len(X)} of {len(R):,} screen rows ({X.Ticker.nunique()} tickers) have |gross return| > 50% at one or more horizons; the full list (ticker, date, returns, status, reason) is the tab Extreme_Row_Audit. Status is MECHANICAL, not news-checked. verified-real = (1) no exchange price adjustment (IDX Previous differs from the prior Close by > 2%) in the window, (2) no daily close-to-close move beyond the exchange limit, (3) an independent return built from IDX closes (restated through any adjustment) within 5pp of the Yahoo-based return, (4) no non-IPO corporate action recorded in the window. verified-artifact = an exchange price adjustment inside the window that, once restated, removes the extreme return or flips its sign. unchecked = anything else, with the reason. 'Entry has opening trade' is a separate column: it says whether Open(D+1) was a real opening print.")
    add("Hd", "Horizon", "Rows with |gross return| > 50% at this horizon", "of N rows with a window", "verified-real", "verified-artifact", "unchecked")
    for n in H:
        z = X[X[f"r{n}"].abs() > 50]; add("R", n, len(z), int(R[f"r{n}"].notna().sum()), int((z.status == "verified-real").sum()), int((z.status == "verified-artifact").sum()), int((z.status == "unchecked").sum()))
    add("R", "Any horizon (rows)", len(X), len(R), int((X.status == "verified-real").sum()), int((X.status == "verified-artifact").sum()), int((X.status == "unchecked").sum()))
    add("X", f"Of the {int((X.status == 'verified-real').sum())} verified-real rows, {int(((X.status == 'verified-real') & ~X.entry_has_opening_trade).sum())} had NO opening trade on the entry day (the path is real, the entry price is a placeholder). Rows that trip the Previous test: {int(X.trips_previous_test.sum())} ({', '.join(sorted(X[X.trips_previous_test].Ticker.unique()))}).")
    add("B")
    add("Hd", "Rows NOT verified-real | Ticker", "Screen date", "Status", "Max |return| (%)", "Horizon (d)", "Entry has opening trade", "Reason")
    for _, r in X[X.status != "verified-real"].sort_values(["status", "Ticker", "Date"]).iterrows():
        add("R", r.Ticker, r.Date, r.status, f1(r.max_abs_return), int(r.max_horizon_tripping), "yes" if r.entry_has_opening_trade else "no", r.reason)
    add("B")
    wt = winsor_trim(d, add, f2)
    reconcile(d, add, f2)
    return X, wt


def main():
    df = BA.build(); fl = entry_flags(df); d = add_flags(df.merge(fl, on=["Ticker", "Date"], how="left"))
    d.to_csv(args.out, index=False)
    f1 = lambda x: round(float(x), 1); f2 = lambda x: round(float(x), 2)
    rows = []; add = lambda k, *c: rows.append((k, list(c)))
    res = test1(d, add, f1, f2)
    X, wt = test2(d, add, f1, f2)
    import json
    json.dump(rows, open("fillability_tab_rows.json", "w"))
    X.drop(columns=[]).round(2).to_csv("extreme_rows_audit.csv", index=False)
    print("rows for the tab:", len(rows), "| extreme rows:", len(X), X.status.value_counts().to_dict())
    print("20d net A: all", f2(res[("All rows (section 0)", 20)]["mean"]), "t", f2(res[("All rows (section 0)", 20)]["t"]), "| excl E1", f2(res[("Excl. E1 (limit-locked entries)", 20)]["mean"]), "t", f2(res[("Excl. E1 (limit-locked entries)", 20)]["t"]))


if __name__ == "__main__":
    main()
