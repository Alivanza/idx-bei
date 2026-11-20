"""Price-bucket split of the logged Backfill rows (<2,000 vs 2,000-3,000): gross, net A, net B, N rows at 7d and 14d, with and
without rows whose window crosses an exchange price adjustment, and without RLCO. Run from this folder:
    python price_bucket_test.py [--prices ../idx_price_history.csv] [--log ../screen_log_export.csv] [--parquet ../../data/parquet]"""
import sys, bisect, argparse, numpy as np, pandas as pd
sys.path.insert(0, '.'); import rets as X, evaluate as E
ap = argparse.ArgumentParser(); ap.add_argument("--cap", default="2026-11-18"); ap.add_argument("--prices", default="../idx_price_history.csv")
ap.add_argument("--log", default="../screen_log_export.csv"); ap.add_argument("--parquet", default="../../data/parquet"); a = ap.parse_args()
log = pd.read_csv(a.log, dtype=str); bf = log[log.Source == "Backfill"].drop_duplicates(["LastTradingDate", "Ticker"])
ret = X.Returns(None, a.prices, a.cap, "csv"); adj = X.Returns(a.parquet, a.prices, a.cap, "parquet")
ss = pd.read_parquet(f"{a.parquet}/stock_summary.parquet", columns=["Date", "StockCode", "Close"]); ss["D"] = ss.Date.dt.strftime("%Y-%m-%d")
cl = ss.set_index(["D", "StockCode"]).Close
rows = []
for r in bf.itertuples():
    p = float(cl.get((r.LastTradingDate, r.Ticker), np.nan))
    for n in (7, 14):
        x = ret.ex(r.Ticker, r.LastTradingDate, n)
        if x is None or np.isnan(p): continue
        t = float(X.tick(np.array([p]))[0]); ca = 0.40 + 100 * t / p; cb = 0.40 + 200 * t / p
        D, O, C_, A = adj.S[r.Ticker]; i = bisect.bisect_left(D, r.LastTradingDate)
        art = bool((np.abs(A[i + 2:i + n + 1] - 1) > 0.02).any())
        rows.append(dict(Tk=r.Ticker, D=r.LastTradingDate, H=n, P=p, g=x[0], A=x[0] - ca, B=x[0] - cb, cA=ca, cB=cb, art=art))
d = pd.DataFrame(rows); d["bucket"] = np.where(d.P < 2000, "<2,000", "2,000-3,000")
dl = lambda x, c: x.groupby("D")[c].mean()
for n in (7, 14):
    for label, f in (("RAW", lambda s: s), ("EXCL. adjustment-crossing rows", lambda s: s[~s.art]), ("EXCL. adjustment-crossing rows and RLCO", lambda s: s[~s.art & (s.Tk != "RLCO")])):
        s = f(d[d.H == n]); print(f"\n{n}d  {label}")
        print(s.groupby("bucket").agg(N=("g", "size"), gross=("g", "mean"), netA=("A", "mean"), netB=("B", "mean"), costA=("cA", "mean"), medG=("g", "median")).round(2).to_string())
        lo = s[s.bucket == "<2,000"]; hi = s[s.bucket != "<2,000"]
        for c in ("g", "A", "B"):
            x, y = dl(lo, c), dl(hi, c); com = x.index.intersection(y.index); df = x[com] - y[com]
            print(f"  gap {c}: row-level {lo[c].mean() - hi[c].mean():+.2f} | date-paired {df.mean():+.2f}  NW t {E.nw_t(df.values, n - 1):+.2f}  ({len(com)} dates)")
