import sys, json, bisect, numpy as np, pandas as pd
sys.path.insert(0,'.'); import replay as R, configs as C, rets as X

def nw_t(x, L):
    x = np.asarray(x, float); T = len(x); e = x - x.mean(); lrv = (e @ e) / T
    for l in range(1, L + 1):
        lrv += 2 * (1 - l / (L + 1)) * (e[l:] @ e[:-l]) / T
    se = np.sqrt(lrv / T); return x.mean() / se if se > 0 else np.nan

def evaluate(sl_by_cfg, ret, ctx, horizons=(7, 14), excl=None):
    """sl_by_cfg: {name: {date: [tickers]}}. Returns tidy rows + per-date net series."""
    P = ctx["P"]; cache = {}
    def row(t, d, n):
        k = (t, d, n)
        if k not in cache:
            ds = str(pd.Timestamp(d).date()); r = ret.ex(t, ds, n)
            if r is None or (excl is not None and excl(t, ds, n)): cache[k] = None
            else:
                px = float(P.loc[d, t]); cost = 0.40 + 100 * float(X.tick(np.array([px]))[0]) / px
                cache[k] = (r[0], r[0] - cost, cost)
        return cache[k]
    out = {}
    for name, sl in sl_by_cfg.items():
        for n in horizons:
            g, nt, cs, nrows, nmiss = {}, {}, {}, 0, 0
            for d, ts in sl.items():
                v = [row(t, d, n) for t in ts]; nrows += len(v)
                ok = [x for x in v if x is not None]; nmiss += len(v) - len(ok)
                if ok:
                    g[d] = np.mean([x[0] for x in ok]); nt[d] = np.mean([x[1] for x in ok]); cs[d] = np.mean([x[2] for x in ok])
            out[(name, n)] = dict(gross=pd.Series(g).sort_index(), net=pd.Series(nt).sort_index(), cost=pd.Series(cs).sort_index(), rows=nrows, miss=nmiss)
    return out
