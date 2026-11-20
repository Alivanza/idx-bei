import bisect, numpy as np, pandas as pd

def tick(p):
    return np.where(p < 200, 1, np.where(p < 500, 2, np.where(p < 2000, 5, np.where(p < 5000, 10, 25))))

class Returns:
    """Excess return of buying Open(D+1) and selling Close(D+N) vs ^JKSE Open(D+1)->Close(D+N), in pp, for ANY ticker.
    src='parquet': IDX stock_summary OHLC (all ~980 tickers). Splits/bonus/rights are neutralised with the exchange's own
    Previous column: adj_t = Previous_t / Close_{t-1} (1.0 on a normal day), exit value is restated into entry units.
    src='csv': Yahoo .JK file used by backtest_analysis.py (only the tickers that ever reached a shortlist)."""
    def __init__(self, parq_dir, csv_path, cap, src="parquet"):
        px = pd.read_csv(csv_path); px = px[px.Date <= cap]
        j = px[px.Ticker == "^JKSE"].sort_values("Date")
        self.bd = j.Date.tolist(); self.bc = j.Close.to_numpy(); self.bo = j.Open.to_numpy()
        self.src = src; self.S = {}
        if src == "csv":
            for t, g in px.groupby("Ticker"):
                g = g.sort_values("Date"); self.S[t] = (g.Date.tolist(), g.Open.to_numpy(), g.Close.to_numpy(), np.ones(len(g)))
        else:
            ss = pd.read_parquet(f"{parq_dir}/stock_summary.parquet", columns=["Date", "StockCode", "OpenPrice", "Close", "Previous"])
            ss["D"] = ss.Date.dt.strftime("%Y-%m-%d"); ss = ss[ss.D <= cap].sort_values(["StockCode", "D"])
            for t, g in ss.groupby("StockCode"):
                c = g.Close.to_numpy(float); pv = g.Previous.to_numpy(float)
                adj = np.ones(len(g)); adj[1:] = np.where((c[:-1] > 0) & (pv[1:] > 0), pv[1:] / c[:-1], 1.0)
                self.S[t] = (g.D.tolist(), g.OpenPrice.to_numpy(float), c, adj)

    def ex(self, t, d, n):
        """(gross excess pp, SheetPx) or None if the window is incomplete / unpriced."""
        if t not in self.S: return None
        D, O, C, A = self.S[t]
        i = bisect.bisect_left(D, d)
        if i >= len(D) or D[i] != d: return None
        j = bisect.bisect_left(self.bd, d)
        if j >= len(self.bd) or self.bd[j] != d: return None
        if i + n >= len(D) or j + n >= len(self.bd) or i + 1 >= len(D) or j + 1 >= len(self.bd): return None
        o1 = O[i + 1]
        if not (o1 > 0) or not (self.bo[j + 1] > 0): return None
        scale = np.prod(A[i + 2:i + n + 1])           # adjustments after the entry session (units change from the entry day onward)
        x = C[i + n] / scale
        bm = (self.bc[j + n] - self.bo[j + 1]) / self.bo[j + 1] * 100
        return (x - o1) / o1 * 100 - bm, C[i]
