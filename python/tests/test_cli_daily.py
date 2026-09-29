"""Tests for `idx daily`'s exit code (idx.cli's "daily" branch).

This is the other half of the ingestion-freeze fix: idx.pipelines.daily.ingest_daily()
correctly reports a real fetch failure as results[dataset]["status"] == "error" (see
test_pipelines_daily.py), but until now nothing at the CLI layer looked at that --
`idx daily` always exited 0 regardless. Since daily-screen.yml's GitHub Actions step
runs `uv run idx daily` directly (not `python -m idx.pipelines.daily`), the CLI branch
is the actual entry point that has to fail loudly for the workflow to turn red.
"""

import pytest

from idx import cli


def test_daily_exits_nonzero_when_a_dataset_errors(monkeypatch):
    monkeypatch.setattr(
        cli,
        "ingest_daily",
        lambda date=None: {
            "stock_summary": {"status": "error", "message": "HTTP 403 for .../GetStockSummary"},
            "broker_summary": {"status": "ok", "records": 900},
            "index_summary": {"status": "no_data"},
        },
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["daily"])
    assert exc_info.value.code == 1


def test_daily_exits_zero_when_nothing_errors(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "ingest_daily",
        lambda date=None: {
            "stock_summary": {"status": "ok", "records": 950},
            "broker_summary": {"status": "ok", "records": 900},
            "index_summary": {"status": "no_data"},  # legitimate (e.g. a holiday) -- not an error
        },
    )
    # Should return normally (no SystemExit) -- "no_data" alone must not fail the run.
    cli.main(["daily"])


def test_daily_reports_which_datasets_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "ingest_daily",
        lambda date=None: {
            "stock_summary": {"status": "error", "message": "boom"},
            "broker_summary": {"status": "error", "message": "boom too"},
            "index_summary": {"status": "ok", "records": 1},
        },
    )
    with pytest.raises(SystemExit):
        cli.main(["daily"])
    err = capsys.readouterr().err
    assert "broker_summary" in err
    assert "stock_summary" in err
