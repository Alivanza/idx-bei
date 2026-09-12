# Daily IDX swing-screener job.
# Windows Task Scheduler runs THIS file on weekday afternoons after market close.
# It fetches today's data, refreshes the corporate-actions list, runs the screen,
# and posts the result to your Google Sheet.

$ErrorActionPreference = "Stop"

# Always run from this script's own folder, regardless of what folder Task Scheduler starts in.
Set-Location -Path $PSScriptRoot

$logFile = Join-Path $PSScriptRoot "logs\run_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "logs") | Out-Null

Start-Transcript -Path $logFile

try {
    Write-Host "=== $(Get-Date) : starting daily IDX ingestion ==="
    uv run idx daily
    uv run idx corporate
    uv run python daily_screen.py
    Write-Host "=== $(Get-Date) : done ==="
}
catch {
    Write-Host "ERROR: $_"
}
finally {
    Stop-Transcript
}