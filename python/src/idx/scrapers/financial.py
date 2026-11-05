"""
Financial ratios and statistics scraper module.
"""

import datetime
import os
import time

from idx.core.client import IDXClient
from idx.core.utils import (
    DATA_DIR,
    check_count_anomaly,
    check_schema_drift,
    get_logger,
    save_json,
    validate_schema,
)

log = get_logger("idx.scrapers.financial")

BASE_URL = "https://www.idx.co.id/primary/DigitalStatistic/GetApiDataPaginated"

QUERY_PARAMS = {
    "urlName": "LINK_FINANCIAL_DATA_RATIO",
    "periodQuarter": 4,
    "periodYear": 2024,
    "type": "yearly",
    "isPrint": "false",
    "cumulative": "false",
    "pageSize": 100,
    "orderBy": "",
    "search": "",
}

OUTPUT_FILE = os.path.join(DATA_DIR, "financial_ratio.json")


def build_url(page_number, year=2024, quarter=4, page_size=100):
    """Builds full URL with query parameters for financial ratio API."""
    from urllib.parse import urlencode

    params = {
        **QUERY_PARAMS,
        "periodQuarter": quarter,
        "periodYear": year,
        "pageSize": page_size,
        "pageNumber": page_number,
    }
    return f"{BASE_URL}?{urlencode(params)}"


def _fetch_period_records(client, year, quarter, page_size):
    """Pages through one reporting period and returns the raw records (possibly empty)."""
    endpoint = "/DigitalStatistic/GetApiDataPaginated"
    base_params = {
        **QUERY_PARAMS,
        "periodQuarter": quarter,
        "periodYear": year,
        "pageSize": page_size,
    }

    all_records = []
    page_number = 1
    has_more = True

    log.info("Fetching financial ratios for year %d quarter %d...", year, quarter)

    while has_more:
        params = {**base_params, "pageNumber": page_number}
        data = client.get_json(endpoint, params=params)

        if data and data.get("data") and len(data["data"]) > 0:
            records = data["data"]
            all_records.extend(records)
            log.info(
                "Retrieved %d records from page %d. Total: %d",
                len(records),
                page_number,
                len(all_records),
            )

            if page_number == 1:
                validate_schema(data, "financial_ratio_page")
                check_schema_drift("financial_ratio_page", data)

            page_number += 1
            time.sleep(1.0)
        else:
            has_more = False
    return all_records


def fetch_financial_ratios(year=None, quarter=4, client=None, page_size=100):
    """Fetches paginated financial data and ratios across all companies.

    year=None (the default, used by `uv run idx financial`) means "the freshest period IDX serves":
    the current calendar year is tried first, then the previous one. Probed 05/11/2026: asking for
    2026 (Q1 and Q2 gave byte-identical responses) returns the latest filings (fsDate up to 2026-09-30,
    963 tickers, every currently listed ticker has a sector); asking for 2025 Q4 returns the FY2025
    file (958 tickers, 26 of the 33 newest listings); 2024 Q4 returns the old 947-ticker file. The
    default used to be a hard-coded 2024 Q4, which is why the sector snapshot went stale. Pass an
    explicit year to pin a period (fetch_historical_financial_ratios does).
    """
    if client is None:
        client = IDXClient()

    if year is None:
        this_year = datetime.date.today().year
        candidates = [this_year, this_year - 1]
    else:
        candidates = [year]

    all_records = []
    used_year = None
    for y in candidates:
        all_records = _fetch_period_records(client, y, quarter, page_size)
        if all_records:
            used_year = y
            break
        log.warning("No financial ratio records for year %d quarter %d.", y, quarter)

    if all_records:
        result = {"totalRecords": len(all_records), "data": all_records}
        save_json(OUTPUT_FILE, result)
        check_count_anomaly("financial_ratio", len(all_records))
        fs_dates = [r.get("fsDate") for r in all_records if r.get("fsDate")]
        log.info(
            "Financial ratios collection finished: %d records (requested year %s, newest fsDate %s).",
            len(all_records), used_year, max(fs_dates) if fs_dates else "unknown",
        )
        return result

    log.warning("No financial ratio records collected.")
    return None


def fetch_historical_financial_ratios(
    years=(2022, 2023, 2024),
    quarters=(4,),
    client=None,
    delay=1.0,
):
    """Fetches financial ratios across multiple historical periods and consolidates them.

    Args:
        years: sequence of years to scrape (e.g. [2022, 2023, 2024]).
        quarters: sequence of quarters (e.g. [1, 2, 3, 4] or [4] for annuals).
        client: optional IDXClient instance.
        delay: delay in seconds between period fetches.

    Returns:
        dict with totalRecords and list of combined records.
    """
    if client is None:
        client = IDXClient()

    all_historical = []
    for y in years:
        for q in quarters:
            log.info("--- Scraping Financial Ratios for %d-Q%d ---", y, q)
            res = fetch_financial_ratios(year=y, quarter=q, client=client)
            if res and res.get("data"):
                all_historical.extend(res["data"])
            time.sleep(delay)

    if all_historical:
        combined_result = {"totalRecords": len(all_historical), "data": all_historical}
        save_json(OUTPUT_FILE, combined_result)
        log.info("Historical financial ratios consolidated: %d total records.", len(all_historical))
        return combined_result

    return None
