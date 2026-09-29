"""Tests for idx.pipelines.daily – daily ingestion pipeline (mocked client)."""

import os

import pytest

from idx.core import timeseries as ts
from idx.pipelines import daily as daily_mod


class FakeClient:
    """IDXClient stand-in returning canned responses per endpoint."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_json(self, endpoint, params=None, **kwargs):
        self.calls.append((endpoint, params, kwargs))
        result = self.responses.get(endpoint)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def ts_dir(tmp_path, monkeypatch):
    """Redirect the timeseries store to a temp dir."""
    path = str(tmp_path / "timeseries")
    os.makedirs(path, exist_ok=True)
    monkeypatch.setattr(ts, "TIMESERIES_DIR", path)
    return path


class TestIngestDataset:
    def test_ingests_new_date(self, ts_dir):
        client = FakeClient(
            {
                "/TradingSummary/GetStockSummary": {
                    "data": [{"Date": "2026-01-05", "StockCode": "BBCA"}]
                }
            }
        )
        result = daily_mod._ingest_dataset(
            client, "stock_summary", "/TradingSummary/GetStockSummary", "20260105", "2026-01-05"
        )
        assert result["status"] == "ok"
        assert result["records"] == 1
        assert set(ts.existing_dates("stock_summary")) == {"2026-01-05"}
        # raise_on_error=True is the whole fix (see _ingest_dataset's docstring) --
        # assert it's actually passed, not just that this happy path still works.
        assert len(client.calls) == 1
        _endpoint, _params, call_kwargs = client.calls[0]
        assert call_kwargs.get("raise_on_error") is True

    def test_skips_existing_date(self, ts_dir):
        ts.write_partition(
            "stock_summary", "2026-01-05", [{"Date": "2026-01-05", "StockCode": "BBCA"}]
        )
        client = FakeClient({})
        result = daily_mod._ingest_dataset(
            client, "stock_summary", "/TradingSummary/GetStockSummary", "20260105", "2026-01-05"
        )
        assert result["status"] == "skipped"
        assert client.calls == []  # no HTTP call for cached dates

    def test_no_data_on_non_trading_day(self, ts_dir):
        client = FakeClient({"/TradingSummary/GetIndexSummary": {"data": []}})
        result = daily_mod._ingest_dataset(
            client, "index_summary", "/TradingSummary/GetIndexSummary", "20260103", "2026-01-03"
        )
        assert result["status"] == "no_data"

    def test_defensive_fallback_if_get_json_somehow_still_returns_none(self, ts_dir):
        """With raise_on_error=True, the real client should never return bare None
        on failure (it raises IDXRequestError instead -- see the next test). This
        only exercises _ingest_dataset's own defensive `isinstance(data, dict)`
        guard for a None value, in case some future caller's get_json stand-in
        returns it directly; it is not simulating a real fetch failure anymore."""
        client = FakeClient({"/TradingSummary/GetBrokerSummary": None})
        result = daily_mod._ingest_dataset(
            client, "broker_summary", "/TradingSummary/GetBrokerSummary", "20260105", "2026-01-05"
        )
        assert result["status"] == "no_data"

    def test_real_fetch_failure_propagates_instead_of_being_treated_as_no_data(self, ts_dir):
        """The actual bug: a real fetch failure (bad HTTP status, exhausted
        retries, a WAF block against the shared GitHub Actions runner IP pool --
        whatever the cause) must surface as a raised error, not silently collapse
        into the same 'no_data (non-trading day?)' status a genuinely empty
        response gets. This is what let stock_summary.parquet freeze at
        2026-09-11 while daily.yml kept reporting a green run."""
        from idx.core.client import IDXRequestError

        client = FakeClient(
            {
                "/TradingSummary/GetStockSummary": IDXRequestError(
                    "HTTP 403 for /TradingSummary/GetStockSummary", status_code=403
                )
            }
        )
        with pytest.raises(IDXRequestError):
            daily_mod._ingest_dataset(
                client,
                "stock_summary",
                "/TradingSummary/GetStockSummary",
                "20260105",
                "2026-01-05",
            )
        # And critically: nothing got written for this date, so a later retry
        # (once the block clears) will correctly see it as still missing --
        # existing_dates() must NOT show 2026-01-05 as already handled.
        assert "2026-01-05" not in ts.existing_dates("stock_summary")


class TestIngestDaily:
    def test_full_run_writes_all_datasets(self, ts_dir, monkeypatch):
        responses = {
            name: {"data": [{"Date": "2026-01-05", f"Key{i}": 1}]}
            for i, name in enumerate(
                [
                    "/TradingSummary/GetStockSummary",
                    "/TradingSummary/GetBrokerSummary",
                    "/TradingSummary/GetIndexSummary",
                ]
            )
        }
        monkeypatch.setattr(daily_mod, "export_parquet_default", False, raising=False)
        results = daily_mod.ingest_daily(
            date="20260105", client=FakeClient(responses), export_parquet=False
        )
        assert all(
            results[ds]["status"] == "ok"
            for ds in ("stock_summary", "broker_summary", "index_summary")
        )
        # Idempotent: re-running the same date skips everything
        results2 = daily_mod.ingest_daily(
            date="20260105", client=FakeClient(responses), export_parquet=False
        )
        assert all(
            results2[ds]["status"] == "skipped"
            for ds in ("stock_summary", "broker_summary", "index_summary")
        )

    def test_migrates_legacy_json(self, ts_dir, tmp_path):
        import json
        import os

        legacy = os.path.join(ts_dir, "index_summary.json")
        with open(legacy, "w") as f:
            json.dump([{"Date": "2025-12-31T00:00:00", "IndexCode": "IHSG"}], f)
        client = FakeClient({})  # no new data anywhere
        daily_mod.ingest_daily(date="20260105", client=client, export_parquet=False)
        assert set(ts.existing_dates("index_summary")) == {"2025-12-31"}
        assert not os.path.exists(legacy)  # renamed to .migrated

    def test_ingest_daily_with_parquet_export_and_errors(self, ts_dir, monkeypatch):
        responses = {
            "/TradingSummary/GetStockSummary": RuntimeError("Connection timed out"),
            "/TradingSummary/GetBrokerSummary": {"data": []},
            "/TradingSummary/GetIndexSummary": {"data": []},
        }
        monkeypatch.setattr("idx.pipelines.parquet.export_all", lambda: {"status": "ok"})
        results = daily_mod.ingest_daily(
            date="20260105", client=FakeClient(responses), export_parquet=True
        )
        assert results["stock_summary"]["status"] == "error"
        assert "parquet_export" in results
