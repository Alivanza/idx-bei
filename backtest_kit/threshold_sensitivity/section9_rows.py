"""Build the rows of Backtest_Results section 9 (threshold sensitivity + price-bucket check) as threshold_tab_rows.json.
Run from backtest_kit/threshold_sensitivity/ AFTER run_all.py (both variants) and fillability_audit.py:
    python run_all.py --out ts_base.csv
    python run_all.py --exclude-adjusted --out ts_xadj.csv
    python section9_rows.py [--base ts_base.csv] [--xadj ts_xadj.csv] [--fill ..\\fillability_rows.csv] [--out ..\\threshold_tab_rows.json]
backtest_analysis.py appends the file after section 11 when it sits in its working directory."""
import sys, bisect, json, argparse, numpy as np, pandas as pd
sys.path.insert(0, '.'); import rets as X, evaluate as E
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="ts_base.csv"); ap.add_argument("--xadj", default="ts_xadj.csv")
ap.add_argument("--fill", default="../fillability_rows.csv"); ap.add_argument("--cap", default="2026-11-18")
ap.add_argument("--prices", default="../idx_price_history.csv"); ap.add_argument("--log", default="../screen_log_export.csv")
ap.add_argument("--parquet", default="../../data/parquet"); ap.add_argument("--out", default="../threshold_tab_rows.json")
a = ap.parse_args()
rows = []
add = lambda k, *c: rows.append([k, list(c)])
b = pd.read_csv(a.base); x = pd.read_csv(a.xadj)
k = ["Param", "Value", "H"]
m = b.merge(x[k + ["dNetA", "dT"]].rename(columns={"dNetA": "dX", "dT": "dTX"}), on=k)
m["Value"] = m["Value"].astype(str)
add("S", "9. THRESHOLD SENSITIVITY (one threshold changed at a time; replay of the F1-F5 screen over the 353 Backfill dates; entry Open(D+1), net A; date-level; prices through 18/11/2026)")
add("X", "Method: backtest_kit/threshold_sensitivity/replay.py is a vectorised copy of run_screen (row-based windows, each ticker's last row on or before D, F5 from today's corporate_actions snapshot, sector from the Backfill log labels else today's financial_ratios, stable ranking ATRsBelowHigh ascending / ATR14_pct descending, sector cap 2). 13 of the 353 dates still differ from the logged shortlist. Statistic = date-level mean (unit = screen date, so each date counts once), Newey-West t with lag h-1; the difference column is the date-paired difference from the current screen (current = 0 by construction). 'Excl. adjustment-crossing' drops rows whose window crosses an exchange price adjustment (|Previous / prior Close - 1| > 2%). 100 further tickers were fetched on 22/11/2026 (idx_price_history.csv now holds 480 tickers) so that every looser variant's rows are priced: unpriced rows fall from up to 204 per variant to 0. Sections 0-11 were re-run on the new file and are unchanged (the 136,814 price rows through 16/10 are identical to the previous file).")
add("X", "Limits: 15 variants x 2 horizons = 30 comparisons, uncorrected t statistics; a Bonferroni correction at 5% two-sided needs |t| of about 3.14. The 7d and 14d windows overlap in time, so the two horizons are not independent tests. Provenance (stated by the user, 19/11/2026): MIN_ADTV 5bn, price 50-3,000, MIN_ATR_PCT 3% and MAX_DIST_TO_HIGH 5% were set before any backtest; MAX_ATRS_BELOW_HIGH 1.0 was added in the October 2026 audit and applied retroactively when Backfill was regenerated from April 2025, with the sample visible, so it is not an out-of-sample choice. PRICE_MIN is not varied. Costs are cost A throughout; a cost model cannot create a gross-return gap, so any gap is checked in gross first.")
add("Hd", "Parameter", "Value", "Current setting", "Horizon (d)", "Screen dates", "Rows", "Rows per date", "Mean gross (pp)", "Mean net A (pp)", "NW t (net A)", "Net A minus current (pp)", "NW t of difference", "Same, excl. adjustment-crossing rows (pp)", "NW t, excl.")
for r in m.itertuples():
    cur = bool(r.IsCurrent)
    add("R", r.Param, r.Value, "yes" if cur else "", int(r.H), int(r.Dates), int(r.Rows), round(r.RowsPerDate, 2), round(r.Gross, 3), round(r.NetA, 3), round(r.NWt, 2),
        "" if cur else round(r.dNetA, 3), "" if cur else round(r.dT, 2), "" if cur else round(r.dX, 3), "" if cur else round(r.dTX, 2))
add("B")

# ---- price-bucket check (<2,000 vs 2,000-3,000), date-paired
log = pd.read_csv(a.log, dtype=str); bf = log[log.Source == "Backfill"].drop_duplicates(["LastTradingDate", "Ticker"])
ret = X.Returns(None, a.prices, a.cap, "csv"); adj = X.Returns(a.parquet, a.prices, a.cap, "parquet")
fr = pd.read_csv(a.fill, low_memory=False, usecols=["Ticker", "Date", "N", "E1", "E3"]); fr["key"] = fr.Ticker + "|" + fr.Date + "|" + fr.N.astype(str)
E1 = dict(zip(fr.key, fr.E1.astype(bool))); E3 = dict(zip(fr.key, fr.E3.astype(bool)))
ss = pd.read_parquet(f"{a.parquet}/stock_summary.parquet", columns=["Date", "StockCode", "Close"]); ss["D"] = ss.Date.dt.strftime("%Y-%m-%d")
cl = ss.set_index(["D", "StockCode"]).Close
L = []
for r in bf.itertuples():
    p = float(cl.get((r.LastTradingDate, r.Ticker), np.nan))
    for n in (7, 14):
        e = ret.ex(r.Ticker, r.LastTradingDate, n)
        if e is None or np.isnan(p): continue
        t = float(X.tick(np.array([p]))[0]); ca = 0.40 + 100 * t / p
        D, O, C_, A = adj.S[r.Ticker]; i = bisect.bisect_left(D, r.LastTradingDate)
        art = bool((np.abs(A[i + 2:i + n + 1] - 1) > 0.02).any()); kk = f"{r.Ticker}|{r.LastTradingDate}|{n}"
        L.append(dict(Tk=r.Ticker, D=r.LastTradingDate, H=n, P=p, g=e[0], A=e[0] - ca, art=art, e1=E1.get(kk, False), e3=E3.get(kk, False)))
d = pd.DataFrame(L); d["lo"] = d.P < 2000
dl = lambda s, c: s.groupby("D")[c].mean()
add("X", "PRICE-BUCKET CHECK (the largest uncorrected difference above is PRICE_MAX 2,000, i.e. dropping the 2,000-3,000 band). Logged Backfill rows split by LastPrice on the screen date: below 2,000 against 2,000-3,000 (about 10% of rows). Gap = mean of (below 2,000 minus 2,000-3,000), paired by screen date, over the dates that have both buckets. The treatment decides the result: 4 PACK rows per horizon sit in the 2,000-3,000 bucket and carry the -92% artifact (12/01/2026 adjustment; they pull that bucket down), while RLCO's 3 rows are limit-locked entries (they pull it up). This is a descriptive split of one cut, chosen after the replay ranked it first, not a tested threshold.")
add("Hd", "Treatment (rows removed)", "Horizon (d)", "Rows < 2,000", "Rows 2,000-3,000", "Mean gross < 2,000 (pp)", "Mean gross 2,000-3,000 (pp)", "Gap, gross (pp)", "NW t", "Gap, net A (pp)", "NW t", "Screen dates with both buckets", "Mean cost A < 2,000 (pp)", "Mean cost A 2,000-3,000 (pp)")
tr = [("All rows", lambda s: s),
      ("Excl. rows whose window crosses an exchange price adjustment (PACK x4 in the upper bucket)", lambda s: s[~s.art]),
      ("Same, and excl. RLCO (ad hoc)", lambda s: s[~s.art & (s.Tk != "RLCO")]),
      ("Same as row 2, and excl. E1 (open at ARA or single-price lock at +9%; section 10)", lambda s: s[~s.art & ~s.e1]),
      ("Same as row 2, and excl. E3 (E1 or no opening trade; section 10)", lambda s: s[~s.art & ~s.e3])]
for n in (7, 14):
    for lab, f in tr:
        s = f(d[d.H == n]); lo = s[s.lo]; hi = s[~s.lo]; out = []
        for c in ("g", "A"):
            u, w = dl(lo, c), dl(hi, c); com = u.index.intersection(w.index); df = u[com] - w[com]
            out += [round(df.mean(), 2), round(E.nw_t(df.values, n - 1), 2)]
        add("R", lab, n, len(lo), len(hi), round(lo.g.mean(), 2), round(hi.g.mean(), 2), out[0], out[1], out[2], out[3], len(com),
            round((lo.g - lo.A).mean(), 2), round((hi.g - hi.A).mean(), 2))
add("X", "Reading: the gross gap is positive in all ten rows above and cost A is smaller, not larger, in the 2,000-3,000 bucket (about 0.8 against about 1.0pp), so the net gap is slightly smaller than the gross gap and costs do not create it. Its size runs from +1.4 to +4.1pp (date-paired, gross) and its t from 1.4 to 3.3 depending on which handful of rows is removed; no treatment is a clean definition, and none of the t statistics is corrected for the 30 comparisons or for choosing this cut after seeing it.")
json.dump(rows, open(a.out, "w"))
print(len(rows), "rows ->", a.out)
