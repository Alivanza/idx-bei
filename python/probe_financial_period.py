"""
READ-ONLY probe: does IDX serve a later financial-ratio reporting period than FY2024 Q4, and would it
fill the 'Unclassified' gap (tickers listed after the FY2024 statements)?

Why: `uv run idx financial` is fixed to year=2024, quarter=4 (fetch_financial_ratios defaults). It re-pulls
the same 947 statements with current sector LABELS, so the newest fsDate stays 2024-10-31 and tickers listed
since stay 'Unclassified' (33 of 980 on 05/11/2026). This probe tries other periods WITHOUT touching
anything the screener reads.

What it does NOT touch: data/ (no JSON, no parquet), the Sheet, git-tracked files. It writes raw responses
to python/probe_out/ (new folder) and prints a comparison against the current
data/parquet/financial_ratios.parquet and the current universe.

Usage (from python\\):
    uv run python probe_financial_period.py                 # tries 2025 Q4, 2026 Q1, 2026 Q2
    uv run python probe_financial_period.py 2025 4 2026 2   # explicit YEAR QUARTER pairs
"""
import json
import os
import sys
import time

import pandas as pd

from idx.core.client import IDXClient
from idx.scrapers.financial import QUERY_PARAMS

HERE = os.path.dirname(os.path.abspath(__file__))
PARQUET_DIR = os.path.abspath(os.path.join(HERE, "..", "data", "parquet"))
OUT_DIR = os.path.join(HERE, "probe_out")
ENDPOINT = "/DigitalStatistic/GetApiDataPaginated"
MAX_PAGES = 30  # safety stop; the FY2024 file is 10 pages of 100


def fetch_period(client, year, quarter):
    records, page = [], 1
    while page <= MAX_PAGES:
        params = {**QUERY_PARAMS, "periodQuarter": quarter, "periodYear": year, "pageSize": 100, "pageNumber": page}
        data = client.get_json(ENDPOINT, params=params)
        if data and data.get("data"):
            records.extend(data["data"])
            page += 1
            time.sleep(1.0)
        else:
            break
    return records


def main():
    args = sys.argv[1:]
    if args:
        if len(args) % 2:
            print("Give YEAR QUARTER pairs, e.g. 2025 4 2026 2")
            sys.exit(2)
        periods = [(int(args[i]), int(args[i + 1])) for i in range(0, len(args), 2)]
    else:
        periods = [(2025, 4), (2026, 1), (2026, 2)]

    cur = pd.read_parquet(os.path.join(PARQUET_DIR, "financial_ratios.parquet"), columns=["code", "sector", "fsDate"])
    cur = cur.dropna(subset=["code"]).drop_duplicates("code")
    cur_sector = dict(zip(cur["code"], cur["sector"]))
    ss = pd.read_parquet(os.path.join(PARQUET_DIR, "stock_summary.parquet"), columns=["Date", "StockCode"])
    universe = set(ss["StockCode"])
    unclassified = sorted(universe - set(cur["code"]))
    print(f"Current snapshot: {len(cur)} codes, newest fsDate {pd.to_datetime(cur['fsDate'], errors='coerce').max().date()}; "
          f"universe {len(universe)}; Unclassified now {len(unclassified)}.")

    os.makedirs(OUT_DIR, exist_ok=True)
    client = IDXClient()
    for year, quarter in periods:
        print(f"\n=== {year} Q{quarter} ===")
        try:
            recs = fetch_period(client, year, quarter)
        except Exception as e:  # keep going to the next period
            print(f"  FAILED: {e!r}")
            continue
        path = os.path.join(OUT_DIR, f"financial_ratio_{year}Q{quarter}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"totalRecords": len(recs), "data": recs}, f)
        print(f"  records: {len(recs)}  (raw saved to python/probe_out/{os.path.basename(path)})")
        if not recs:
            print("  nothing served for this period.")
            continue
        print(f"  first record keys: {sorted(recs[0].keys())}")
        df = pd.DataFrame(recs)
        if "code" not in df.columns:
            print("  no 'code' column -- cannot compare. Paste the keys line above.")
            continue
        df = df.dropna(subset=["code"]).drop_duplicates("code")
        fs = pd.to_datetime(df.get("fsDate"), errors="coerce")
        print(f"  codes: {len(df)}; fsDate min/max: {fs.min().date() if fs.notna().any() else None} / "
              f"{fs.max().date() if fs.notna().any() else None}")
        print(f"  fsDate top values: {fs.dt.date.value_counts().head(3).to_dict() if fs.notna().any() else {}}")
        has_sector = df[df["sector"].notna()] if "sector" in df.columns else df.iloc[0:0]
        sec = dict(zip(has_sector["code"], has_sector["sector"]))
        got = [c for c in unclassified if c in sec]
        print(f"  Unclassified tickers covered with a sector: {len(got)} of {len(unclassified)}")
        if got:
            print("   ", {c: sec[c] for c in got[:40]})
        both = [c for c in sec if c in cur_sector and cur_sector[c] != sec[c]]
        print(f"  sector label differs from current snapshot for {len(both)} of {len(set(sec) & set(cur_sector))} shared codes")
        for c in both[:15]:
            print(f"    {c}: {cur_sector[c]} -> {sec[c]}")
        missing = sorted(set(cur_sector) - set(sec))
        print(f"  codes in current snapshot but absent here: {len(missing)}" + (f" e.g. {missing[:10]}" if missing else ""))
    print("\nDone. Nothing under data/ was modified.")


if __name__ == "__main__":
    main()
