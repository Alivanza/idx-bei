"""
backtest_analysis.py  --  rebuilds every table in the Google Sheet tab `Backtest_Results`.

Run from backtest_kit/ (needs pandas, numpy, scipy; no statsmodels):
    python backtest_analysis.py            # writes backtest_results_tab.json (+ prints a validation block)

Inputs (same files compute_backtest.py uses):
    idx_price_history.csv  -- Ticker, Date, Close, Open, High, Low, Volume (Yahoo .JK + ^JKSE)
    screen_log_export.csv  -- Screen_Log export (RunDate, LastTradingDate, Ticker, Name, Sector, LastPrice, Source)

Definitions (identical to compute_backtest.py; verified to reproduce backtest_row_detail.csv to <0.001pp):
    entry   = Close(T, D);  exit = Close(T, N-th trading day after D in T's own series)
    excess  = (exit-entry)/entry*100 - ^JKSE return over the same trading-day window     [pp]
Horizons: 3, 5, 7, 10, 14, 20 trading days.

Section 1b (overlap-corrected inference) -- unit of observation = one screen date:
    x_d   = mean excess of that date's shortlist rows                                     [date-level series]
    t     = mean(x) / (sd(x)/sqrt(T))                                                     [naive across dates]
    NW    = Newey-West, Bartlett kernel, lag = horizon-1, no small-sample correction
            gamma_l = sum_{t>=l} e_t e_{t-l} / T ;  LRV = gamma_0 + 2*sum_{l=1..L}(1-l/(L+1))*gamma_l ;  SE = sqrt(LRV/T)
    MBB   = moving-block bootstrap on x, block length = horizon (in date observations, overlapping blocks,
            blocks drawn from start positions 0..T-L), ceil(T/L) blocks concatenated and cut to T,
            10,000 resamples, 2.5th/97.5th percentiles, numpy default_rng(SEED) shared across horizons in order.

Section 0 (HEADLINE, executable): entry Open(D+1), exit Close(D+N), benchmark ^JKSE Open(D+1) -> Close(D+N) (same window).
    Same date-level inference as 1b (Newey-West lag h-1, moving-block bootstrap), gross and net of cost A / cost B.
    Sections 1, 1b, 1c, 2, 3, 5 use Close(D): reference only, not executable when the screen runs after the close.
Section 8 (regime split, only when idx_price_history.csv covers screens after 01/10/2026): pre/post short-selling resumption.

Section 1c (costs), row level:
    cost_A (base case) = 0.15% buy + 0.25% sell + ONE full tick = 0.40 + 100*tick/LastPrice = RoundTripPct = SpreadPct + 0.40%
    cost_B (stress)    = 0.15% buy + 0.25% sell + TWO ticks     = 0.40 + 200*tick/LastPrice = RoundTripPct_wide = 2*SpreadPct + 0.40%
    (a buyer lifts the offer and a seller hits the bid: a one-tick-wide book costs ONE tick per round trip; two ticks is the
     thin-name stress case.)  main() asserts cost_A / cost_B equal round_trip_pct() below, the function item 6 puts in daily_screen.py.
    tick   = IDX price fraction on the row's Screen_Log LastPrice: <200 ->1, 200..<500 ->2, 500..<2000 ->5,
             2000..<5000 ->10, >=5000 ->25 (lower bounds inclusive; 27 of 5,252 rows sit exactly on 200/500/2000 and
             moving them to the lower band changes the mean cost by 0.003pp).
    net    = excess - cost.   No slippage beyond the tick, no liquidity limit.
"""
import bisect, json
import numpy as np, pandas as pd

BASE = ""                       # run from backtest_kit/
H = [3, 5, 7, 10, 14, 20]
SEED = 20261119
PX_CAP_MAIN = "2026-10-16"      # sections 0-7: price vintage as published (windows end on or before 16/10/2026)
PX_CAP_FULL = "2026-11-18"      # section 8: last complete session (rows dated 19/11 are intraday and excluded)
HSC_STAGES = ("2026-11-02", "2026-11-13")   # staged HSC exclusion from the IHSG
L1_END = "2026-11-01"           # Layer 1: forward window must END on or before this date
B_MBB = 10000
LEGACY_BOOT = {5: (1.26, 2.76), 10: (1.95, 5.03), 20: (3.46, 9.48)}   # ticker-cluster draws published 16/10/2026


def tick(p):
    return np.where(p < 200, 1, np.where(p < 500, 2, np.where(p < 2000, 5, np.where(p < 5000, 10, 25))))


def round_trip_pct(p, wide=False):
    """Item 6: RoundTripPct = SpreadPct + 0.40% (base) ; RoundTripPct_wide = 2*SpreadPct + 0.40%.  Fractions, not pp."""
    p = np.asarray(p, dtype=float)
    return (2 if wide else 1) * tick(p) / p + 0.004


def sheet_px(s):
    return float(str(s).replace(".", "").replace(",", "."))


def trim_mean(x, frac):
    x = np.sort(np.asarray(x)); k = int(frac * len(x))
    return x[k:len(x) - k].mean()


LAST_PX = ""


def build(sources=("Backfill",), cap=None):
    cap = cap or PX_CAP_MAIN
    px = pd.read_csv(BASE + "idx_price_history.csv"); px = px[px.Date <= cap]
    sl = pd.read_csv(BASE + "screen_log_export.csv", dtype=str)
    sl = sl[sl.Source.isin(sources)].drop_duplicates(["LastTradingDate", "Ticker"])
    global LAST_PX; LAST_PX = pd.read_csv(BASE + "idx_price_history.csv", usecols=["Date"]).Date.max()
    ser = {t: g.sort_values("Date").reset_index(drop=True) for t, g in px.groupby("Ticker")}
    D = {t: g.Date.tolist() for t, g in ser.items()}
    O = {t: g.Open.to_numpy() for t, g in ser.items()}; Hh = {t: g.High.to_numpy() for t, g in ser.items()}
    L = {t: g.Low.to_numpy() for t, g in ser.items()}; C = {t: g.Close.to_numpy() for t, g in ser.items()}
    bd, bc, bo = D["^JKSE"], C["^JKSE"], O["^JKSE"]
    out = []
    for r in sl.itertuples(index=False):
        t, d = r.Ticker, r.LastTradingDate
        if t not in D: continue
        i = bisect.bisect_left(D[t], d)
        if i >= len(D[t]) or D[t][i] != d: continue
        j = bisect.bisect_left(bd, d)
        if j >= len(bd) or bd[j] != d: continue
        P = sheet_px(r.LastPrice)
        hi20 = Hh[t][i - 19:i + 1].max() if i >= 19 else np.nan      # 20 sessions ending D
        cl20 = C[t][i - 19:i + 1].max() if i >= 19 else np.nan
        c0, h0, l0 = C[t][i], Hh[t][i], L[t][i]
        prev = C[t][i - 1] if i >= 1 else np.nan
        for n in H:
            rec = dict(Ticker=t, Date=d, SheetPx=P, N=n, Close=c0, High=h0, Low=l0, Hi20=hi20, Cl20=cl20, Prev=prev)
            if i + n < len(D[t]) and j + n < len(bd):
                rec["ExitDate"] = D[t][i + n]; rec["EntryDate1"] = D[t][i + 1]
                rec["Misaligned"] = D[t][i + n] != bd[j + n]          # ticker's N-th own row must be the index calendar's N-th day
            if i + n < len(D[t]) and j + n < len(bd):
                x = C[t][i + n]; b = (bc[j + n] - bc[j]) / bc[j] * 100
                # Close(D) rows: same 3-decimal rounding as compute_backtest.py (ForwardReturnPct, Benchmark, Excess), so the
                # tab reproduces backtest_row_detail.csv exactly; other entry variants use unrounded values.
                ret_r, b_r = round((x - c0) / c0 * 100, 3), round(b, 3)
                rec.update(Bench=b_r, Ret=ret_r, Ex_C0=round(ret_r - b_r, 3), Ex_H0=(x - h0) / h0 * 100 - b)
                if i + 1 < len(D[t]):
                    o1 = O[t][i + 1]; rec["Ex_O1"] = (x - o1) / o1 * 100 - b          # section 4: benchmark Close(D)->Close(D+N)
                    if j + 1 < len(bd) and bo[j + 1] > 0:                              # section 0: matched benchmark Open(D+1)->Close(D+N)
                        bm = (bc[j + n] - bo[j + 1]) / bo[j + 1] * 100
                        rec["Ex_O1m"] = (x - o1) / o1 * 100 - bm
            if i + 1 + n < len(D[t]) and j + 1 + n < len(bd):
                c1 = C[t][i + 1]; x1 = C[t][i + 1 + n]; b1 = (bc[j + 1 + n] - bc[j + 1]) / bc[j + 1] * 100
                rec["Ex_C1"] = (x1 - c1) / c1 * 100 - b1
            out.append(rec)
    df = pd.DataFrame(out)
    df["Tick"] = tick(df.SheetPx.to_numpy()); df["SpreadPct"] = df.Tick / df.SheetPx * 100
    df["CostA"] = 0.40 + df.SpreadPct; df["CostB"] = 0.40 + 2 * df.SpreadPct
    return df


def nw_t(x, L):
    T = len(x); e = x - x.mean()
    lrv = (e @ e) / T
    for l in range(1, L + 1):
        lrv += 2 * (1 - l / (L + 1)) * (e[l:] @ e[:-l]) / T
    se = np.sqrt(lrv / T)
    return se, x.mean() / se


def mbb_ci(x, L, rng):
    n = len(x); k = int(np.ceil(n / L))
    idx = rng.integers(0, n - L + 1, size=(B_MBB, k))
    ar = (idx[:, :, None] + np.arange(L)[None, None, :]).reshape(B_MBB, -1)[:, :n]
    return np.percentile(x[ar].mean(axis=1), [2.5, 97.5])


def ticker_boot(d, rng, B=1000):
    g = d.groupby("Ticker").Ex_C0.agg(["sum", "count"]); s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    idx = rng.integers(0, len(g), size=(B, len(g)))
    m = s[idx].sum(axis=1) / c[idx].sum(axis=1)
    return np.percentile(m, [2.5, 97.5])




def section0(add, v, f1, f2):
    """Headline: executable entry Open(D+1). Date-level means, NW t, moving-block bootstrap CI; gross, net A, net B."""
    rng0 = np.random.default_rng(SEED + 2)
    res = {}
    for n in H:
        d = v(n, "Ex_O1m"); res[n] = d
    add("S", "0. HEADLINE - EXECUTABLE ENTRY: buy at Open(D+1), exit Close(D+N), benchmark ^JKSE Open(D+1) to Close(D+N) (Backfill rows)")
    add("X", "The screen is computed from data as of D, so the first price you can trade is the next open. Unit = screen date (mean over that date's shortlist rows); Newey-West lag = horizon - 1; moving-block bootstrap (blocks of length = horizon, 10,000 resamples, 95% interval). Net = excess minus round-trip cost per row: A (base) = 0.40% + one tick of the screen LastPrice = RoundTripPct; B (stress) = 0.40% + two ticks = RoundTripPct_wide. Row-level median and hit rate are over rows. Sections 1-5 enter at Close(D) and are reference only.")
    add("Hd", "Gross, before costs | horizon (trading days)", "Screen dates", "Date-level mean excess", "SD across dates", "Newey-West t", "Block-bootstrap 95% CI lower", "Block-bootstrap 95% CI upper", "Row-level median excess", "Row-level hit rate, excess > 0 (%)")
    ser = {}
    for n in H:
        d = res[n]; x = d.groupby("Date").Ex_O1m.mean().sort_index().to_numpy(); ser[n] = x
        _, t = nw_t(x, n - 1); lo, hi = mbb_ci(x, n, rng0)
        add("R", n, len(x), f2(x.mean()), f2(x.std(ddof=1)), f2(t), f2(lo), f2(hi), f2(d.Ex_O1m.median()), f1((d.Ex_O1m > 0).mean() * 100))
    for key, col, lab in (("A", "CostA", "Net of cost A (base): 0.40% + 1 tick"), ("B", "CostB", "Net of cost B (stress): 0.40% + 2 ticks")):
        add("Hd", lab + " | horizon (trading days)", "N rows", "Mean cost (pp)", "Date-level mean net excess", "Newey-West t", "Block-bootstrap 95% CI lower", "Block-bootstrap 95% CI upper", "Row-level median net excess", "Row-level hit rate, net excess > 0 (%)")
        for n in H:
            d = res[n]; net = d.Ex_O1m - d[col]
            g = net.groupby(d.Date).mean().sort_index().to_numpy(); _, t = nw_t(g, n - 1); lo, hi = mbb_ci(g, n, rng0)
            add("R", n, len(d), f2(d[col].mean()), f2(g.mean()), f2(t), f2(lo), f2(hi), f2(net.median()), f1((net > 0).mean() * 100))
    add("Hd", "With / without RLCO (11 Backfill rows; limit-up run 352 -> 8,700 from 10/12/2025) | horizon", "RLCO rows in sample", "Gross date-level mean, with", "Gross, excl. RLCO", "Net A date-level mean, with", "Net A, excl. RLCO", "Net A Newey-West t, excl. RLCO", "Net B date-level mean, with", "Net B, excl. RLCO")
    for n in H:
        d = res[n]; e = d[d.Ticker != "RLCO"]
        def dl(x, col, sub=None):
            z = (x.Ex_O1m - (x[col] if col else 0)).groupby(x.Date).mean().sort_index().to_numpy(); return z
        gw, ge = dl(d, None), dl(e, None); aw, ae = dl(d, "CostA"), dl(e, "CostA"); bw, be = dl(d, "CostB"), dl(e, "CostB")
        add("R", n, int((d.Ticker == "RLCO").sum()), f2(gw.mean()), f2(ge.mean()), f2(aw.mean()), f2(ae.mean()), f2(nw_t(ae, n - 1)[1]), f2(bw.mean()), f2(be.mean()))
    d5 = res[5]
    old = v(5, "Ex_O1")["Ex_O1"]
    add("X", f"Reconciliation: 5d row-level mean gross excess = {d5.Ex_O1m.mean():.2f} here (matched benchmark) vs {old.mean():.2f} in section 4 (benchmark Close(D) to Close(D+N), which subtracts the index's average +0.19% overnight gap that an Open(D+1) buyer never owned); the date-level gross mean at 5d is {ser[5].mean():.2f}. Hold period is the first session from the open plus N-1 closes (exit Close(D+N), the same exit as section 1).")
    add("B")


def regime_section(add, df, f1, f2):
    """Section 8: before / after short-selling resumption, executable entry Open(D+1), all sources, prices through PX_CAP_FULL.
    Layer 1 = forward window ends on or before L1_END (no HSC stage inside the window).  Layer 2 = every complete window; rows whose
    window crosses an HSC stage (entry date < stage <= exit date) are counted and flagged.  Estimates are withheld where
    dates / horizon < 3 (fewer than three non-overlapping windows)."""
    dm = lambda d: f"{d[8:10]}/{d[5:7]}/{d[:4]}"
    df = df.dropna(subset=["Ex_O1m", "ExitDate"]).copy()
    s1, s2 = HSC_STAGES
    df["Cross"] = ((df.EntryDate1 < s1) & (df.ExitDate >= s1)) | ((df.EntryDate1 < s2) & (df.ExitDate >= s2))
    df["L1"] = df.ExitDate <= L1_END
    last = df.Date.max()
    splits = ("2026-09-15", "2026-10-01")
    add("S", "8. REGIME SPLIT: before / after short-selling resumption (executable entry Open(D+1); date-level; all Backfill + Live rows; prices through 18/11/2026)")
    add("X", "READ AS A SINGLE-REGIME TEST, NOT A GENERAL OUT-OF-SAMPLE TEST: the post sample sits inside one episode, the October crash and rebound (IHSG 6,071 on 30/09, 4,996 on 19/10, 5,439 on 23/10; -11.6% on 16/10), so it cannot separate a short-selling effect from that episode. Short selling was reactivated in phases from 15/09/2026 and the eligible-stock list took effect in October; the split is shown at 15/09 and at 01/10. BENCHMARK MOVES INSIDE SOME WINDOWS: the HSC exclusion from the IHSG came in stages (about 80 stocks on 02/11, 383 by 13/11). Layer 1 = only screens whose entire forward window ends on or before 01/11/2026 (no contamination). Layer 2 = every complete window; rows whose window crosses 02/11 or 13/11 are measured against a benchmark that changed composition mid-hold and are counted below. Price rows dated 19/11 are intraday and excluded; the last complete session is 18/11/2026. Sample sizes come first; estimates are withheld where screen dates / horizon < 3 (fewer than three non-overlapping windows).")
    for split in splits:
        add("Hd", f"SAMPLE SIZES, split {dm(split)} | horizon (trading days)", "Last screen date with a complete window (all)", "Last screen date, Layer 1 (window ends <= 01/11)", "Pre dates", "Post dates, Layer 1", "Post dates, Layer 2", "Post rows, Layer 1", "Post rows, Layer 2", "Layer 2 rows that cross an HSC stage")
        for n in H:
            d = df[df.N == n]; post = d[d.Date >= split]; pre = d[d.Date < split]; p1 = post[post.L1]
            add("R", n, dm(d.Date.max()), dm(d[d.L1].Date.max()), pre.Date.nunique(), p1.Date.nunique(), post.Date.nunique(), len(p1), len(post), int(post.Cross.sum()))
    add("Hd", "ESTIMATES | split / regime / layer | horizon", "Screen dates", "Independent windows (dates / h)", "Gross date-level mean excess", "SD across dates", "Newey-West t (gross)", "Net A date-level mean", "Newey-West t (net A)", "Status")
    for split in splits:
        for n in H:
            d = df[df.N == n]
            for lab, sub in ((f"{dm(split)} | pre (all windows clean) | {n}d", d[d.Date < split]),
                             (f"{dm(split)} | post, Layer 1 (clean) | {n}d", d[(d.Date >= split) & d.L1]),
                             (f"{dm(split)} | post, Layer 2 (full; contaminated rows flagged) | {n}d", d[d.Date >= split])):
                T = sub.Date.nunique(); ind = round(T / n, 1)
                cn = int(sub.Cross.sum()) if "Layer 2" in lab else 0
                st = (f"{cn} of {len(sub)} rows cross an HSC stage" if "Layer 2" in lab else f"{len(sub)} rows")
                if T == 0 or T / n < 3:
                    add("R", lab, T, ind, "withheld", "withheld", "withheld", "withheld", "withheld", st + "; withheld: fewer than 3 independent windows"); continue
                g = sub.groupby("Date").Ex_O1m.mean().sort_index().to_numpy(); gn = (sub.Ex_O1m - sub.CostA).groupby(sub.Date).mean().sort_index().to_numpy()
                L = n - 1; ok = T > L + 2
                add("R", lab, T, ind, f2(g.mean()), f2(g.std(ddof=1)), f2(nw_t(g, L)[1]) if ok else "n/a", f2(gn.mean()), f2(nw_t(gn, L)[1]) if ok else "n/a", st)
    add("B")


def main():
    df = build()
    # cost model must equal the item-6 formulas exactly (this is what the user asked to be asserted)
    assert np.allclose(df.CostA / 100, round_trip_pct(df.SheetPx)), "cost A != RoundTripPct"
    assert np.allclose(df.CostB / 100, round_trip_pct(df.SheetPx, wide=True)), "cost B != RoundTripPct_wide"
    # worked checks: SMGR 1,640 (tick 5): A = 0.705% (0.71 to 2 dp as quoted), B = 1.010%;  DEWI 250 (tick 2): A = 1.20%, B = 2.00%
    for p, a_, b_ in ((1640, 0.7049, 1.0098), (250, 1.20, 2.00)):
        assert abs(float(round_trip_pct(p)) * 100 - a_) < 0.0005 and abs(float(round_trip_pct(p, True)) * 100 - b_) < 0.0005
    v = lambda n, col="Ex_C0": df[(df.N == n)].dropna(subset=[col])
    f2 = lambda x: round(float(x), 2)
    f1 = lambda x: round(float(x), 1)
    rows = []                                  # (kind, [cells])  kinds: T title, X text, S section, Hd header, R data, B blank
    add = lambda k, *c: rows.append((k, list(c)))
    add("T", "Shortlist forward-return backtest (Backfill rows)")
    add("X", "Generated 16/10/2026 from backtest_kit/idx_price_history.csv (OHLC, Yahoo .JK, unadjusted; data through 16/10/2026); horizons 3/7/14d, section 0 (executable Open(D+1) entry), section 1b (overlap-corrected inference) and section 1c (costs) added 19/11/2026 from the same inputs by backtest_kit/backtest_analysis.py. 5/10/20d figures reproduce the 16/10 generation. Benchmark ^JKSE. Returns in % points. Backfill only (screens 2025-04-16 to 2026-10-09); Live is 15 rows on 1 date and is not reported here. Sections 10 (fillability of the entry) and 11 (extreme-row audit, winsorized/trimmed headline, PACK vs other adjustment rows) and the Extreme_Row_Audit tab added 19/11/2026 by backtest_kit/fillability_audit.py. Section 9 (threshold sensitivity and price-bucket check) added 22/11/2026 by backtest_kit/threshold_sensitivity/ (run_all.py, section9_rows.py) after the price file was extended to 480 symbols. READ SECTION 0 FIRST: it is the only section whose entry price can be traded. Every section that enters at Close(D) is a reference, not an achievable return.")
    add("B")
    section0(add, v, f1, f2)
    add("S", "1. REFERENCE ONLY - entry Close on screen date D (UNACHIEVABLE IN PRACTICE: the screen runs on data as of D, so Close(D) cannot be bought)")
    add("Hd", "Horizon (trading days)", "N rows", "Mean excess vs ^JKSE", "Median excess", "Hit rate, raw return > 0 (%)", "Hit rate, excess > 0 (%)", "Mean raw return", "Mean ^JKSE return")
    sd = []
    for n in H:
        d = v(n); sd.append(f1(d.Ex_C0.std(ddof=1)))
        add("R", n, len(d), f2(d.Ex_C0.mean()), f2(d.Ex_C0.median()), f1((d.Ret > 0).mean() * 100), f1((d.Ex_C0 > 0).mean() * 100), f2(d.Ret.mean()), f2(d.Bench.mean()))
    add("X", "Std dev of excess (" + "/".join(f"{n}" for n in H) + "d): " + " / ".join(str(s) for s in sd) + ". Mean > 0 with median < 0 and hit rate < 50% = positive skew: a minority of large winners carries the average.")
    add("B")

    # ---- 1b
    rng = np.random.default_rng(SEED)
    inf = {}
    for n in H:
        x = v(n).groupby("Date").Ex_C0.mean().sort_index().to_numpy(); T = len(x)
        se, t_nw = nw_t(x, n - 1); lo, hi = mbb_ci(x, n, rng)
        inf[n] = dict(T=T, mean=x.mean(), sd=x.std(ddof=1), t=x.mean() / (x.std(ddof=1) / np.sqrt(T)), se=se, tnw=t_nw, lo=lo, hi=hi)
    add("S", "1b. CORRECTED INFERENCE, entry Close(D) - REFERENCE ONLY, not executable (unit = screen date; accounts for overlapping windows and same-date correlation)")
    add("X", f"Observation = one screen date: the mean excess of that date's shortlist rows (~14.9 rows per date). Naive t = mean / (SD / sqrt(dates)). Newey-West: Bartlett kernel, lag = horizon - 1, no small-sample correction. Block bootstrap: moving blocks of length = horizon (date observations; dates with no shortlist rows are absent, so a block spans slightly more than h trading days), {B_MBB:,} resamples, seed {SEED}, 2.5th / 97.5th percentiles. Rows in the sections below are kept as they were; the equal-weight-per-date row and the ticker-cluster bootstrap in section 2 are valid but are not overlap-corrected.")
    add("Hd", "Horizon (trading days)", "Screen dates", "Date-level mean excess", "SD across dates", "Naive t (dates)", "Newey-West SE", "Newey-West t", "Block-bootstrap 95% CI lower", "Block-bootstrap 95% CI upper")
    for n in H:
        r = inf[n]; add("R", n, r["T"], f2(r["mean"]), f2(r["sd"]), f2(r["t"]), round(r["se"], 3), f2(r["tnw"]), f2(r["lo"]), f2(r["hi"]))
    lo_t = min(i["tnw"] for i in inf.values()); hi_t = max(i["tnw"] for i in inf.values())
    add("X", f"Newey-West t ranges {lo_t:.2f} to {hi_t:.2f} across horizons and every block-bootstrap interval excludes zero; the naive date-level t overstates the evidence by a factor of {min(i['t']/i['tnw'] for i in inf.values()):.1f} to {max(i['t']/i['tnw'] for i in inf.values()):.1f}.")
    add("B")

    # ---- 1c
    add("S", "1c. COST-ADJUSTED EXCESS, entry Close(D) - REFERENCE ONLY, not executable (net = excess - round-trip cost, per row)")
    add("X", "Cost A (base case) = 0.15% buy + 0.25% sell + ONE full tick of the row's LastPrice (= 0.40 + 100 x tick / LastPrice, in pp) = RoundTripPct = SpreadPct + 0.40%: you buy at the offer and sell at the bid, one tick per round trip on a one-tick-wide book (asserted equal to the formula daily_screen.py will use). Cost B (stress case) = 0.40% + TWO ticks = RoundTripPct_wide = 2 x SpreadPct + 0.40%, for thin names whose book is wider than one tick. Tick (IDX price fraction): < Rp200 = 1; Rp200 to < 500 = 2; 500 to < 2,000 = 5; 2,000 to < 5,000 = 10; >= 5,000 = 25, taken from the row's Screen_Log LastPrice (lower bounds inclusive; the IDX page's table uses '<' on both sides, so the boundary convention is mine; 27 of 5,252 rows sit on a boundary, mean cost effect 0.003pp). No slippage beyond the tick and no liquidity limit. Date-level columns average net excess within each screen date first (1b series).")
    cons = {"A": [], "B": []}
    for key, col, lab in (("A", "CostA", "Cost A (base): 0.40% + 1 tick (RoundTripPct)"), ("B", "CostB", "Cost B (stress): 0.40% + 2 ticks (RoundTripPct_wide)")):
        add("Hd", lab + " | horizon (trading days)", "N rows", "Mean cost (pp)", "Mean net excess", "Median net excess", "Hit rate, net excess > 0 (%)", "Gross date-level mean", "Net date-level mean", "Newey-West t (net date-level)")
        for n in H:
            d = v(n); net = d.Ex_C0 - d[col]
            g = net.groupby(d.Date).mean().sort_index().to_numpy(); gg = inf[n]["mean"]
            _, tn = nw_t(g, n - 1); cons[key].append(f"{n}d {100 * (1 - g.mean() / gg):.0f}%")
            add("R", n, len(d), f2(d[col].mean()), f2(net.mean()), f2(net.median()), f1((net > 0).mean() * 100), f2(gg), f2(g.mean()), f2(tn))
    add("X", "Share of the gross date-level mean consumed by cost. A: " + ", ".join(cons["A"]) + ". B: " + ", ".join(cons["B"]) + ".")
    add("B")

    # ---- 2
    add("S", "2. SENSITIVITY OF MEAN EXCESS (entry Close(D) - reference only)")
    add("Hd", "Treatment", *[f"{n}d" for n in H], "How computed")
    rngb = np.random.default_rng(SEED + 1)
    cols = {}
    for n in H:
        d = v(n); x = d.Ex_C0.to_numpy(); lo, hi = np.percentile(x, [1, 99], method="nearest")
        k = int(len(x) * 0.01); top = np.sort(x)[-k:]; rl = d[d.Ticker == "RLCO"].Ex_C0
        bl, bh = ticker_boot(d, rngb)
        if n in LEGACY_BOOT: bl, bh = LEGACY_BOOT[n]
        cols[n] = [f2(x.mean()), f2(np.clip(x, lo, hi).mean()), f2(trim_mean(x, 0.01)), f2(d.groupby("Date").Ex_C0.mean().mean()),
                   f2(bl), f2(bh), f1(top.mean()), f2(top.sum() / len(x)), f2(rl.sum() / len(x))]
    labels = [("Raw mean", "AVERAGE of ExcessReturnPct"),
              ("Winsorized 1% / 99%", "clip to 1st/99th percentile (nearest-rank), then average"),
              ("Trimmed 1% each tail", "drop lowest and highest 1% of rows, average the rest"),
              ("Equal weight per screen date", "average within each LastTradingDate, then average the date means"),
              ("Bootstrap 95% CI, lower", "resample tickers with replacement, 1,000 draws, 2.5th percentile (5/10/20d: draws published 16/10/2026; 3/7/14d: new draws, seed %d). Handles ticker clustering, not same-date correlation or window overlap - see 1b" % (SEED + 1)),
              ("Bootstrap 95% CI, upper", "97.5th percentile of the same draws"),
              ("Top 1% of rows: mean excess", "average of the highest 1% of rows by excess (floor(1% x N) rows)"),
              ("Top 1% of rows: contribution to overall mean", "(sum of top-1% excess) / (all rows)"),
              ("RLCO (11 rows): contribution to overall mean", "RLCO close 352 -> 8,700 between 10/12/2025 and 20/01/2026: consecutive limit-up days, not a split")]
    for i, (lab, how) in enumerate(labels):
        add("R", lab, *[cols[n][i] for n in H], how)
    add("B")

    # ---- 3
    add("S", "3. PERIOD STABILITY (mean excess, entry Close(D) - reference only)")
    add("Hd", "Half (by screen date)", *[f"{n}d" for n in H], "Split date (median screen date)")
    fh, sh, sp = [], [], []
    for n in H:
        d = v(n); dates = np.sort(d.Date.unique()); m = dates[len(dates) // 2]
        fh.append(f2(d[d.Date < m].Ex_C0.mean())); sh.append(f2(d[d.Date >= m].Ex_C0.mean())); sp.append(f"{m[8:10]}/{m[5:7]}/{m[:4]} ({n}d)")
    add("R", "First half", *fh, "before " + " / ".join(sp))
    add("R", "Second half", *sh, "on/after those dates")
    add("B")

    # ---- 4
    add("S", "4. ENTRY PRICE DEFINITION (excess vs ^JKSE, pp; exit = Close on D+N)")
    add("Hd", "Entry rule", "Horizon", "N rows", "Mean", "Trimmed 1% mean", "Median", "Hit rate, excess > 0 (%)")
    rules = [("Close(D)", "Ex_C0"), ("High(D) [hypothetical: unknowable until close]", "Ex_H0"), ("Open(D+1) [benchmark Close(D)->Close(D+N), as published 16/10; section 0 uses the matched benchmark]", "Ex_O1"), ("Close(D+1), exit Close(D+1+N)", "Ex_C1")]
    for n in H:
        for lab, c in rules:
            x = v(n, c)[c]; add("R", lab, n, len(x), f2(x.mean()), f2(trim_mean(x, 0.01)), f2(x.median()), f1((x > 0).mean() * 100))
    d5 = v(5)
    add("X", f"Gap and day-range facts ({len(d5):,} rows): mean Open(D+1)/Close(D) - 1 = +0.97% (median 0.00%). Mean Close(D)/High(D) - 1 = -1.59% (median -0.99%). Benchmark for High(D) and Open(D+1) rows is ^JKSE Close(D) to Close(D+N), so those two rows carry a small window mismatch.")
    add("B")

    # ---- 5
    add("S", "5. WHERE THE SIGNAL DAY CLOSED (entry Close(D) - reference only; excess pp)")
    add("Hd", "Subset", "Horizon", "N rows", "Mean", "Trimmed 1% mean", "Median", "Hit rate, excess > 0 (%)")
    hl = lambda d: d.High > d.Low
    groups = [("Close = day's high", lambda d: hl(d) & (d.Close >= d.High), "Close below day's high", lambda d: hl(d) & (d.Close < d.High)),
              ("Made 20-day INTRADAY high that day", lambda d: d.Hi20.notna() & (d.High >= d.Hi20), "Did not", lambda d: d.Hi20.notna() & (d.High < d.Hi20)),
              ("Close = highest close of last 20 days", lambda d: d.Cl20.notna() & (d.Close >= d.Cl20), "Not", lambda d: d.Cl20.notna() & (d.Close < d.Cl20)),
              ("Same-day return >= +5%", lambda d: (d.Close / d.Prev - 1) >= 0.05, "Same-day return < +5%", lambda d: (d.Close / d.Prev - 1) < 0.05)]
    def sub_row(lab, n, d):
        x = d.Ex_C0; add("R", lab, n, len(x), f2(x.mean()), f2(trim_mean(x, 0.01)), f2(x.median()), f1((x > 0).mean() * 100))
    for la, fa, lb, fb in groups:
        for n in H:
            d = v(n); sub_row(la, n, d[fa(d)]); sub_row(lb, n, d[fb(d)])
    for n in H:
        d = v(n); sub_row("Same-day return >= +10%", n, d[(d.Close / d.Prev - 1) >= 0.10])
    d = v(5); N5 = len(d); Nhl = int(hl(d).sum()); N20 = int(d.Hi20.notna().sum())
    add("X", f"Shares of screened rows (5d sample): close = day's high {100 * (hl(d) & (d.Close >= d.High)).sum() / Nhl:.1f}% (of {Nhl:,} rows; excludes {N5 - Nhl} rows with High = Low); 20-day closing high {100 * (d.Cl20.notna() & (d.Close >= d.Cl20)).sum() / N20:.1f}% and 20-day intraday high {100 * (d.Hi20.notna() & (d.High >= d.Hi20)).sum() / N20:.1f}% (of {N20:,} rows with 20 prior observations); same-day return >= +5% {100 * ((d.Close / d.Prev - 1) >= 0.05).sum() / N5:.1f}% (of {N5:,}). Subsets overlap.")
    add("B")

    # ---- 8 (only once the price file reaches into November, i.e. after the PC-side Yahoo fetch)
    if LAST_PX >= "2026-11-10":
        regime_section(add, build(("Backfill", "Live", "Sheet"), PX_CAP_FULL), f1, f2)

    # ---- 6, 7
    add("S", "6. FORMULAS (rebuild any number from idx_price_history.csv and screen_log_export.csv; backtest_analysis.py does all of it)")
    for a, b in [("EntryPrice", "Close(T, D), D = Screen_Log LastTradingDate. Variants: High(T, D); Open(T, D+1); Close(T, D+1)"),
                 ("ExitPrice", "Close(T, N-th trading day after D in T's own series); for the Close(D+1) variant, N-th day after D+1"),
                 ("ForwardReturn_N", "(ExitPrice - EntryPrice) / EntryPrice x 100"),
                 ("Excess_N", "ForwardReturn_N minus the ^JKSE return over the same trading-day window"),
                 ("Mean excess in Sheets", "AVERAGEIFS(ExcessCol, SourceCol, \"Backfill\", HorizonCol, 5, ReasonCol, \"OK\") after importing backtest_row_detail.csv as a tab"),
                 ("Median / trimmed", "MEDIAN(FILTER(ExcessCol, SourceCol=\"Backfill\", HorizonCol=5, ReasonCol=\"OK\")) ; TRIMMEAN(FILTER(...), 0.02)"),
                 ("Date-level mean (1b)", "x_d = AVERAGEIFS(ExcessCol, LastTradingDateCol, d, HorizonCol, N, ...) per screen date d; mean = AVERAGE(x_d); SD = STDEV.S(x_d); naive t = mean / (SD / SQRT(COUNT(x_d)))"),
                 ("Newey-West, block bootstrap (1b)", "Not practical as sheet formulas; formulas are in the docstring of backtest_analysis.py (Bartlett lag = horizon - 1; moving blocks of length = horizon, 10,000 resamples)"),
                 ("Cost per row (1c, section 0)", "CostA = RoundTripPct = 0.40 + 100 x Tick / LastPrice; CostB = RoundTripPct_wide = 0.40 + 200 x Tick / LastPrice; Tick = IFS(P<200,1, P<500,2, P<2000,5, P<5000,10, TRUE,25); net excess = Excess_N - Cost"),
                 ("Executable excess (section 0)", "(Close(T, D+N) - Open(T, D+1)) / Open(T, D+1) x 100 minus (^JKSE Close(D+N) - ^JKSE Open(D+1)) / ^JKSE Open(D+1) x 100. Section 4's Open(D+1) rows subtract the index return from Close(D) instead; the index gaps up +0.19% on average overnight, so section 4 is lower by about that much"),
                 ("Close position in day range", "(Close - Low) / (High - Low); 'close = day's high' means Close >= High with High > Low")]:
        add("R", a, b)
    add("B")
    add("S", "7. ASSUMPTIONS AND LIMITS")
    for a, b in [("Hindsight", "Not verified whether ADTV20 / High20 / ATR14_pct in Backfill were computed point-in-time. If not, selection uses future information. Threshold provenance (stated by the user, 19/11/2026): MIN_ADTV 5bn, price 50-3,000, MIN_ATR_PCT 3% and MAX_DIST_TO_HIGH 5% were set before any backtest; MAX_ATRS_BELOW_HIGH 1.0 was added in the October 2026 audit and applied retroactively when Backfill was regenerated from April 2025 (a structural argument, chosen with the sample visible), so that one threshold is not an out-of-sample choice. Section 9 shows how the net A result moves when each threshold is changed on its own."),
                 ("Execution", "Section 0 is the headline: entry at Open(D+1), the first price available after a screen run on data as of D, net of 0.40% fees plus one tick (A) or two ticks (B) at the screen LastPrice. It assumes a fill AT the open price with no slippage beyond the tick and no liquidity limit. Section 10 tests that: 118 of 5,252 screen rows (2.2%, 46 tickers) were entered on a session that traded at a single price at least 9% above the previous close, or opened within one tick of ARA (limit-locked); excluding them moves the 20d net A from +3.79 (Newey-West t 2.08, CI lower bound +0.20) to +3.23 (t 1.95, lower bound -0.06) and the 7d from +0.56 to -0.06. A further 574 rows (674 in all, 12.8%, 130 tickers) had no opening trade on IDX (OpenPrice = 0), so Yahoo's Open is a placeholder, not a print; all 286 zero-volume entry sessions are among those 674. Whether a retail order at the limit would have been filled depends on queue position, which the data cannot show. Sections 1-5 enter at Close(D), which cannot be bought, and are reference only. High(D) is unknowable until the close; it is a bound, not a strategy."),
                 ("Corporate actions", "No split / rights adjustment. 85 of 5,267 rows (1.6%) have Yahoo entry price differing >1% from sheet LastPrice (CYBR 27, RMKE 15, RAJA 10, MEJA 10, PBSA 6). Flagged, not removed. PACK closed 3,280 on 09/01/2026 and 272 on 12/01/2026 (-91.7%; the exchange's Previous column confirms an adjustment, an unadjusted corporate-action artifact): 4 PACK rows have windows crossing it. Removing them raises the date-level net A means by 0.025 to 0.053pp (3d to 20d) - the earlier note of 0.02 to 0.06pp was right for PACK alone. Removing EVERY row whose window crosses an exchange price adjustment (|Previous / prior Close - 1| > 2%: 6 to 33 rows by horizon) raises them by 0.03 to 0.12pp; the other rows' share is 0.007 to 0.077pp (7d: PACK +0.053, others +0.022; 14d: +0.048 and +0.045). Those other rows are mostly ex-rights adjustments, where the raw return understates the holder's return, and have not been verified one by one; see sections 10-11. COCO (-68% on 30/09/2025) sits inside no window."),
                 ("Independence", "Rows from the same ticker on consecutive dates have overlapping windows, and rows on the same screen date are correlated (~14.9 per date). The ticker-cluster bootstrap in section 2 handles dependence within a ticker but not overlap between adjacent dates or same-date correlation; section 1b (date-level series, Newey-West, block bootstrap) addresses those. Section 4-5 subset differences were not tested for significance."),
                 ("Coverage", "All 374 screened symbols returned data (incl. TRUE = Triniti Dinamik, 14 rows, now priced). From 22/11/2026 idx_price_history.csv holds 480 symbols (479 stocks and ^JKSE): the screened symbols plus those that enter the looser threshold variants of section 9; the rows of the screened symbols through 16/10 are identical to the previous file. Rows lacking a full forward window at a horizon are excluded from that horizon only (N rows differ by horizon)."),
                 ("Index methodology (HSC exclusion is STAGED, not a single break)", "Press reports: about 80 stocks removed from the IHSG calculation from 02/11/2026 (22/10 reports), the list growing to 383 stocks by 13/11/2026 (Antara, dated 13/11, gives 13/11 as the start of that stage); the test is now ownership-based (10 holders or fewer holding 92.5% or more). HATM and RLCO are on the 26/10 list; ARKO and MPXL are not. ^JKSE therefore drifts in methodology in steps, not at one date. Sections 0-6 use screens up to 09/10/2026 with windows ending on or before 16/10/2026, before any stage. Section 8 (regime split) uses windows that cross 02/11 and 13/11, so its excess figures mix two ^JKSE definitions; no historical HSC list vintages exist to rebuild a consistent benchmark."),
                 ("Cost model", "RoundTripPct (A) = SpreadPct + 0.40%, one tick per round trip; RoundTripPct_wide (B) = 2 x SpreadPct + 0.40%. Tick bands (IDX price fraction) are unchanged through 2026: <200 = 1, 200 to <500 = 2, 500 to <2,000 = 5, 2,000 to <5,000 = 10, >=5,000 = 25. The IDX table uses '<' on both sides of each band, so the boundary prices (200, 500, 2,000, 5,000) are unassigned there; this tab assigns them to the higher band (lower bound inclusive). 27 of 5,252 rows sit exactly on a boundary; moving them changes the mean cost by 0.003pp."),
                 ("Status", "Values pasted from Python analysis (not live formulas). Source: backtest_kit/ in the idx-bei repo folder (backtest_analysis.py).")]:
        add("R", a, b)

    import os
    if os.path.exists("fillability_tab_rows.json"):          # sections 10-11, produced by fillability_audit.py (run it first)
        rows.append(("B", []))
        rows += [tuple(x) for x in json.load(open("fillability_tab_rows.json"))]
    if os.path.exists("threshold_tab_rows.json"):             # section 9, produced by threshold_sensitivity/section9_rows.py
        rows.append(("B", []))
        rows += [tuple(x) for x in json.load(open("threshold_tab_rows.json"))]
    with open("backtest_results_tab.json", "w") as fh_:
        json.dump(rows, fh_)
    # validation block
    r0 = v(5)
    print("windows where ticker N-th own row != index N-th day:", int(df.Misaligned.sum()) if "Misaligned" in df else "n/a", "of", int(df.ExitDate.notna().sum()))
    print("rows:", len(rows), "| 5d N:", len(r0), "| dates per horizon:", {n: inf[n]["T"] for n in H})
    print("date-level mean:", {n: round(inf[n]["mean"], 2) for n in H})
    print("NW t:", {n: round(inf[n]["tnw"], 2) for n in H})
    print("20d MBB CI:", round(inf[20]["lo"], 2), round(inf[20]["hi"], 2))


if __name__ == "__main__":
    main()
