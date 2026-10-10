/*
 * ADD THIS to your existing AppsScript_Code.gs -- do not replace the whole file with this
 * snippet. Paste the three pieces below into the right spots in your current Code.gs
 * (wherever that stands right now -- this doesn't assume any particular version of the
 * rest of the file), then redeploy the same way you always do: Deploy > Manage deployments
 * > pencil icon > Version: New version > Deploy.
 *
 * WHERE EACH PIECE GOES:
 *
 * 1. In doPost(e), right after the existing `if (body.mode === "backfill") { ... }` block
 *    (before the unconditional writeLatestTop8(ss, body) line), add:
 *
 *        if (body.mode === "prices_init") {
 *          var priceRows = body.rows || [];
 *          appendDailyPricesInit(ss, priceRows);
 *          return ContentService.createTextOutput(
 *            "OK prices_init: " + priceRows.length + " row(s) appended to " + DAILY_PRICES_TAB_NAME
 *          ).setMimeType(ContentService.MimeType.TEXT);
 *        }
 *
 *        if (body.mode === "prices_daily") {
 *          var dailyRows = body.rows || [];
 *          appendDailyPrices(ss, dailyRows, body.date);
 *          return ContentService.createTextOutput(
 *            "OK prices_daily (" + body.date + "): " + dailyRows.length + " row(s) written to " + DAILY_PRICES_TAB_NAME
 *          ).setMimeType(ContentService.MimeType.TEXT);
 *        }
 *
 * 2. Anywhere at the top level of the file (e.g. right after the existing
 *    LIVE_WATCH_ALLTICKERS_MAX_ROWS var), add everything between the ===== markers below.
 *
 * This reuses getOrCreateSheet / styleHeaderRow, which already exist in your file -- nothing
 * else needs to change.
 */

// ===== BEGIN ADDITION =====

// ---- Daily All Tickers (full-universe OHLCV) --------------------------------------------
// Replaces the tab's previous content -- a Screen_Log self-join formula that only ever
// showed a ticker on days it happened to get shortlisted (~15/day out of ~950-980), so it
// was blank for the vast majority of ticker/date combinations by construction, not by
// failure. This version is posted data (Open/High/Low/Close/Volume per ticker per day),
// sourced from stock_summary.parquet via backfill_daily_prices.py (one-time historical
// load) and daily_screen.py (ongoing daily append) -- see those scripts' docstrings.
//
// Long format (one row per ticker per day), not a wide ticker-by-date grid like the old
// tab: a single grid cell can only hold one value, and OHLCV is 5 values per ticker/day.
// Use FILTER/QUERY formulas on top of this tab to pivot into a wide view for a specific
// field if you want one (e.g. Close only) -- that's a spreadsheet-formula job, not
// something this script needs to pre-build.

var DAILY_PRICES_TAB_NAME = "Daily All Tickers";
var DAILY_PRICES_HEADERS = ["Ticker", "Name", "Date", "Open", "High", "Low", "Close", "Volume"];

// Resets the tab (clears whatever was there -- the old self-join formula, or a stale
// schema from an earlier version of this script) only if its header doesn't already match
// DAILY_PRICES_HEADERS. Once the tab is on this schema, later calls (in either mode) never
// clear it again -- they only append/replace specific date-rows. This is what makes it
// safe to call appendDailyPricesInit() across many chunked POSTs without each one wiping
// the previous chunk's work.
function getOrResetDailyPricesSheet(ss) {
  var sh = getOrCreateSheet(ss, DAILY_PRICES_TAB_NAME);
  var lastRow = sh.getLastRow();
  var needsReset = true;
  if (lastRow > 0) {
    var width = Math.max(sh.getLastColumn(), 1);
    var currentHeader = sh.getRange(1, 1, 1, width).getValues()[0];
    needsReset = currentHeader.join("|") !== DAILY_PRICES_HEADERS.join("|");
  }
  if (needsReset) {
    sh.clear();
    sh.getRange(1, 1, 1, DAILY_PRICES_HEADERS.length).setValues([DAILY_PRICES_HEADERS]);
    styleHeaderRow(sh, DAILY_PRICES_HEADERS.length);
    sh.setColumnWidths(1, DAILY_PRICES_HEADERS.length, 100);
    sh.setColumnWidth(2, 220); // Name
  }
  return sh;
}

function formatDailyPricesRows(sh, startRow, numRows) {
  if (numRows <= 0) return;
  sh.getRange(startRow, 3, numRows, 1).setNumberFormat("yyyy-mm-dd"); // Date
  sh.getRange(startRow, 4, numRows, 1).setNumberFormat("#,##0");      // Open
  sh.getRange(startRow, 5, numRows, 1).setNumberFormat("#,##0");      // High
  sh.getRange(startRow, 6, numRows, 1).setNumberFormat("#,##0");      // Low
  sh.getRange(startRow, 7, numRows, 1).setNumberFormat("#,##0");      // Close
  sh.getRange(startRow, 8, numRows, 1).setNumberFormat("#,##0");      // Volume
}

// mode "prices_init" -- ONE-TIME historical load, called once per chunk by
// backfill_daily_prices.py. Deliberately does NOT scan for/delete pre-existing rows before
// appending: the caller guarantees each chunk covers dates that have never been posted
// before (a fresh tab, loaded date-range by date-range in order), so there is nothing to
// dedupe against. Skipping that scan matters at this data's scale -- the full historical
// load is ~339,000 rows, and reading/comparing a column that large on every one of dozens
// of chunk-POSTs would add real time for no benefit when nothing will ever collide. If you
// re-run backfill_daily_prices.py after an interrupted run, it resumes from the last
// checkpoint (see that script) rather than re-POSTing already-landed chunks, so this
// assumption holds even on a resume.
function appendDailyPricesInit(ss, rows) {
  var sh = getOrResetDailyPricesSheet(ss);
  if (!rows.length) return;
  var startRow = sh.getLastRow() + 1;
  sh.getRange(startRow, 1, rows.length, DAILY_PRICES_HEADERS.length).setValues(rows);
  formatDailyPricesRows(sh, startRow, rows.length);
}

// mode "prices_daily" -- ONGOING update, one call per trading day from daily_screen.py.
// Mirrors appendToLogBulk's dedupe pattern elsewhere in this file: one bulk read of the
// Date column, delete any rows already present for TODAY's date (so a retried/re-run daily
// post replaces that day's block instead of duplicating it), then bulk-insert. This is a
// single-date scan even once the tab has hundreds of thousands of historical rows in it --
// still one bulk getValues() call, not a per-row operation, same as the rest of this file's
// established pattern.
function appendDailyPrices(ss, rows, date) {
  var sh = getOrResetDailyPricesSheet(ss);
  var lastRow = sh.getLastRow();
  if (lastRow >= 2) {
    var tz = Session.getScriptTimeZone();
    var values = sh.getRange(2, 3, lastRow - 1, 1).getValues(); // column C = Date
    var rowsToDelete = [];
    for (var i = 0; i < values.length; i++) {
      var cell = values[i][0];
      var cellStr = (cell instanceof Date) ? Utilities.formatDate(cell, tz, "yyyy-MM-dd") : String(cell);
      if (cellStr === date) rowsToDelete.push(2 + i);
    }
    for (var j = rowsToDelete.length - 1; j >= 0; j--) {
      sh.deleteRow(rowsToDelete[j]); // descending order so earlier indices stay valid
    }
  }
  if (!rows.length) return;
  var startRow = sh.getLastRow() + 1;
  sh.getRange(startRow, 1, rows.length, DAILY_PRICES_HEADERS.length).setValues(rows);
  formatDailyPricesRows(sh, startRow, rows.length);
}

// ===== END ADDITION =====
