/*
 * IDX Daily Screener receiver.
 *
 * SETUP:
 * 1. In your Google Sheet: Extensions > Apps Script.
 * 2. Delete whatever is in Code.gs and paste this whole file in.
 * 3. Set your shared secret as a SCRIPT PROPERTY, not in this source file: Project Settings
 *    (gear icon, left sidebar) > Script Properties > Add script property > name it
 *    SHARED_SECRET, value = the SAME string you put in config.json's "shared_secret" on
 *    your PC. This keeps the actual secret out of this file (which is tracked in your git
 *    repo) -- only the code that reads it lives here.
 * 4. Click Deploy > New deployment > select type "Web app".
 *    - Execute as: Me
 *    - Who has access: Anyone
 *    - Click Deploy, authorize when prompted, then copy the "Web app URL" it gives you.
 * 5. Paste that URL into config.json's "webapp_url" field on your PC.
 * 6. Re-run run_daily.ps1 once by hand to test -- check the Sheet updates.
 *
 * UPDATING THIS SCRIPT LATER: editing and Saving Code.gs does NOT push new code to an
 * already-deployed Web App -- Apps Script freezes whatever code was live at the last
 * "Deploy". After pasting an update in, go to Deploy > Manage deployments > pencil icon
 * on the existing deployment > set Version to "New version" > Deploy. That keeps the same
 * URL (no secret to update) but actually serves the new code to doPost callers.
 *
 * ONE-TIME EXTRA STEP (styling / guide tab):
 * The formatting below (colors, number formats, frozen headers) and the Guide tab are
 * applied by code, not by hand -- because every automated run calls .clear() on the data
 * tabs, any formatting you added manually in the Sheet UI would be wiped on the next run.
 * To see the Guide tab and the new formatting immediately (without waiting for tomorrow's
 * run), open Extensions > Apps Script, pick "buildGuideTab" from the function dropdown
 * (top toolbar, next to the bug icon), and click Run once. Everything else (the data
 * tabs) will pick up the new formatting automatically the next time doPost fires.
 *
 * LIVE_WATCH TAB: pulls a live(ish) price for each ticker currently in Shortlist via
 * the Sheets-native GOOGLEFINANCE() function -- NOT part of the Python/parquet pipeline,
 * purely spreadsheet formulas that recalculate on their own while the Sheet is open.
 * ~20 min delayed and not all smaller IDX tickers have data (GOOGLEFINANCE limitation,
 * not this script's) -- see the note written into the tab itself. Rebuilt every run
 * (buildLiveWatchTab), same as Guide; run it manually from the function dropdown too if
 * you want to see it without waiting for the next scheduled run.
 */

// The secret itself lives in this script's Script Properties (see SETUP step 3 above), not
// here in source -- this file is tracked in a git repo, and a hardcoded secret would end up
// in that repo's history the moment it's committed. getSharedSecret() returns null if the
// property was never set, which doPost() below treats as "reject everything" rather than
// silently comparing against undefined.
function getSharedSecret() {
  return PropertiesService.getScriptProperties().getProperty("SHARED_SECRET");
}

// Renamed from "Latest_Top8": row count hasn't been a fixed 8 since the per-sector cap was
// added (see Guide tab section 3) -- "Shortlist" describes what the tab actually holds.
// LEGACY_SHORTLIST_TAB_NAME exists only so getShortlistSheet() (below) can rename the
// existing tab in place on the first run after this update, instead of abandoning it and
// creating an empty new one.
var SHORTLIST_TAB_NAME = "Shortlist";
var LEGACY_SHORTLIST_TAB_NAME = "Latest_Top8";

// High20 sits between ATR14_pct and DistToHigh20 in every header list below: it's the raw
// 20-day-high price (IDR) that DistToHigh20 and ATRsBelowHigh are both computed FROM
// ((High20 - LastPrice)/High20 and (High20 - LastPrice)/ATR14 respectively) -- having the
// input next to its two derived/normalized outputs is what you'd want to rebuild either by
// hand. Inserting it shifts every column index after it by one in TOP_HEADERS,
// ALLPASSERS_HEADERS and ALLTICKERS_HEADERS -- see the updated setNumberFormat column
// indices in formatTop8Rows/writeAllPassers/writeAllTickers below, all changed to match.
var TOP_HEADERS = ["RunDate", "LastTradingDate", "Ticker", "Name", "Sector", "LastPrice", "LotCost",
                    "ADTV20", "ATR14_pct", "High20", "DistToHigh20", "ATRsBelowHigh", "BreakoutLast3", "Source"];

// Screen_Log uses a DIFFERENT column order from TOP_HEADERS above: High20 is appended as the
// LAST column here instead of inserted between ATR14_pct and DistToHigh20. Reason: Shortlist
// (writeLatestTop8) calls sh.clear() and rewrites every cell on every run, so inserting
// High20 wherever reads best costs nothing there. Screen_Log is append-only history that
// already had thousands of rows written under the OLD 13-column schema before High20
// existed -- inserting a 14th field in the MIDDLE of that would silently shift every later
// field (DistToHigh20, ATRsBelowHigh, BreakoutLast3, Source) one column out from under its
// header for every pre-High20 row. Appending at the end instead means old rows just read
// blank for High20 (an honest "not computed for this row"), and nothing that was already
// correct moves. See getOrCreateScreenLogSheet() below for the one-time header migration
// this required on an existing Screen_Log tab.
var SCREENLOG_HEADERS = ["RunDate", "LastTradingDate", "Ticker", "Name", "Sector", "LastPrice", "LotCost",
                          "ADTV20", "ATR14_pct", "DistToHigh20", "ATRsBelowHigh", "BreakoutLast3", "Source",
                          "High20"];
var ALLPASSERS_HEADERS = ["RunDate", "LastTradingDate", "Ticker", "Name", "Sector", "LastPrice",
                           "ADTV20", "ATR14_pct", "High20", "DistToHigh20", "ATRsBelowHigh", "BreakoutLast3",
                           "IncludedInShortlist"];
var ALLTICKERS_HEADERS = ["RunDate", "LastTradingDate", "Ticker", "Name", "Sector", "LastPrice",
                           "ADTV20", "ATR14_pct", "High20", "DistToHigh20", "ATRsBelowHigh", "BreakoutLast3",
                           "F1_Liquidity", "F2_PriceRange", "F3_VolMomentum", "F4_NotZeroTradeFlag",
                           "F5_NoPendingCA", "PassesAll", "IncludedInShortlist"];
var SECTOR_HEADERS = ["RunDate", "Sector", "Universe", "Passed", "Shortlisted", "PassRate"];
var NEARMISS_HEADERS = ["RunDate", "Ticker", "Name", "ADTV20", "FailedFilters"];

// Column colors used consistently across tabs.
var HEADER_BG = "#1f3864";
var HEADER_FONT = "#ffffff";
var SECTION_BG = "#d9e2f3";
var BORDER_COLOR = "#cccccc";

function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return ContentService.createTextOutput("Bad JSON: " + err).setMimeType(ContentService.MimeType.TEXT);
  }

  var expectedSecret = getSharedSecret();
  if (!expectedSecret || body.secret !== expectedSecret) {
    return ContentService.createTextOutput("Forbidden: bad secret").setMimeType(ContentService.MimeType.TEXT);
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // One-time historical pull (backfill_screen_log.py): a batch of past-day results, each
  // shaped exactly like a normal daily body. Only Screen_Log is historical -- Shortlist /
  // All_Passers / Sector_Breakdown / Run_Info describe "right now" and are intentionally
  // left untouched here; the next real (Source="Live") doPost will populate those normally.
  if (body.mode === "backfill") {
    var runs = body.runs || [];
    appendToLogBulk(ss, runs);
    return ContentService.createTextOutput("OK backfill: " + runs.length + " day(s) written to Screen_Log")
      .setMimeType(ContentService.MimeType.TEXT);
  }

  // One-time historical pull for Daily All Tickers (backfill_daily_prices.py): a chunk of
  // full-universe OHLCV rows for one or more trading days. Mirrors the "backfill" branch
  // above -- deliberately does not touch Shortlist/Screen_Log/All_Passers/etc.
  if (body.mode === "prices_init") {
    var priceRows = body.rows || [];
    appendDailyPricesInit(ss, priceRows);
    return ContentService.createTextOutput(
      "OK prices_init: " + priceRows.length + " row(s) appended to " + DAILY_PRICES_TAB_NAME
    ).setMimeType(ContentService.MimeType.TEXT);
  }

  // Ongoing daily update for Daily All Tickers (daily_screen.py's post_daily_prices()): one
  // trading day's full-universe OHLCV, posted alongside (not instead of) the normal screen
  // payload below.
  if (body.mode === "prices_daily") {
    var dailyRows = body.rows || [];
    appendDailyPrices(ss, dailyRows, body.date);
    return ContentService.createTextOutput(
      "OK prices_daily (" + body.date + "): " + dailyRows.length + " row(s) written to " + DAILY_PRICES_TAB_NAME
    ).setMimeType(ContentService.MimeType.TEXT);
  }

  writeLatestTop8(ss, body);
  appendToLog(ss, body);
  writeAllPassers(ss, body);
  writeAllTickers(ss, body); // full universe, pass+fail -- see its own comment; live-run only
  writeSectorBreakdown(ss, body);
  writeNearMisses(ss, body);
  writeMeta(ss, body);
  buildGuideTab(ss); // static content, cheap to rebuild -- keeps it in sync with this script
  buildLiveWatchTab(ss); // formulas only, re-pointed at today's Shortlist rows each run
  buildLiveWatchPassersTab(ss); // same idea, sourced from All_Passers instead
  buildLiveWatchAllTickersTab(ss); // same idea again, capped to the top-100-by-ADTV20 of All_Tickers

  return ContentService.createTextOutput("OK").setMimeType(ContentService.MimeType.TEXT);
}

function getOrCreateSheet(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  return sh;
}

// One-time migration for the Latest_Top8 -> Shortlist rename: if "Shortlist" doesn't exist
// yet but the pre-rename "Latest_Top8" tab does, rename that tab in place rather than
// creating a fresh "Shortlist" tab and leaving "Latest_Top8" behind as an orphaned, stale
// duplicate -- this preserves the tab's position among the others and anything (a pinned
// color, a link, a filter view) pointed at that same sheet object. After the first run
// following this update, LEGACY_SHORTLIST_TAB_NAME no longer exists, so this is a no-op on
// every later call.
function getShortlistSheet(ss) {
  var sh = ss.getSheetByName(SHORTLIST_TAB_NAME);
  if (sh) return sh;
  var legacy = ss.getSheetByName(LEGACY_SHORTLIST_TAB_NAME);
  if (legacy) {
    legacy.setName(SHORTLIST_TAB_NAME);
    return legacy;
  }
  return ss.insertSheet(SHORTLIST_TAB_NAME);
}

// ---- Shared formatting helpers -------------------------------------------------

function styleHeaderRow(sh, numCols) {
  sh.getRange(1, 1, 1, numCols)
    .setFontWeight("bold")
    .setBackground(HEADER_BG)
    .setFontColor(HEADER_FONT)
    .setVerticalAlignment("middle");
  sh.setFrozenRows(1);
}

function borderRange(range) {
  range.setBorder(true, true, true, true, true, true, BORDER_COLOR, SpreadsheetApp.BorderStyle.SOLID);
}

// Column formats for the Top-8-style header layout:
// RunDate, LastTradingDate, Ticker, Name, Sector, LastPrice, LotCost, ADTV20,
// ATR14_pct, High20, DistToHigh20, ATRsBelowHigh, BreakoutLast3
function formatTop8Rows(sh, startRow, numRows) {
  if (numRows <= 0) return;
  sh.getRange(startRow, 1, numRows, 1).setNumberFormat("yyyy-mm-dd");      // RunDate
  sh.getRange(startRow, 2, numRows, 1).setNumberFormat("yyyy-mm-dd");      // LastTradingDate
  sh.getRange(startRow, 6, numRows, 1).setNumberFormat("#,##0");           // LastPrice (IDR)
  sh.getRange(startRow, 7, numRows, 1).setNumberFormat("#,##0");           // LotCost (IDR)
  sh.getRange(startRow, 8, numRows, 1).setNumberFormat("#,##0");           // ADTV20 (IDR)
  sh.getRange(startRow, 9, numRows, 1).setNumberFormat("0.00%");           // ATR14_pct
  sh.getRange(startRow, 10, numRows, 1).setNumberFormat("#,##0");          // High20 (IDR)
  sh.getRange(startRow, 11, numRows, 1).setNumberFormat("0.00%");          // DistToHigh20
  sh.getRange(startRow, 12, numRows, 1).setNumberFormat("0.00");           // ATRsBelowHigh (in ATRs, not %)
  borderRange(sh.getRange(startRow, 1, numRows, TOP_HEADERS.length));
}

// Column formats for Screen_Log's layout (see SCREENLOG_HEADERS comment for why it differs
// from formatTop8Rows above): High20 is column 14, not column 10.
function formatScreenLogRows(sh, startRow, numRows) {
  if (numRows <= 0) return;
  sh.getRange(startRow, 1, numRows, 1).setNumberFormat("yyyy-mm-dd");      // RunDate
  sh.getRange(startRow, 2, numRows, 1).setNumberFormat("yyyy-mm-dd");      // LastTradingDate
  sh.getRange(startRow, 6, numRows, 1).setNumberFormat("#,##0");           // LastPrice (IDR)
  sh.getRange(startRow, 7, numRows, 1).setNumberFormat("#,##0");           // LotCost (IDR)
  sh.getRange(startRow, 8, numRows, 1).setNumberFormat("#,##0");           // ADTV20 (IDR)
  sh.getRange(startRow, 9, numRows, 1).setNumberFormat("0.00%");           // ATR14_pct
  sh.getRange(startRow, 10, numRows, 1).setNumberFormat("0.00%");          // DistToHigh20
  sh.getRange(startRow, 11, numRows, 1).setNumberFormat("0.00");           // ATRsBelowHigh
  sh.getRange(startRow, 14, numRows, 1).setNumberFormat("#,##0");          // High20 (IDR)
  borderRange(sh.getRange(startRow, 1, numRows, SCREENLOG_HEADERS.length));
}

// One-time migration for an existing Screen_Log tab written before High20 existed: its
// header row (row 1) is never rewritten by appendToLog/appendToLogBulk once the sheet has
// data (they only write headers on a brand-new, empty sheet -- see isNew in both), so a
// pre-High20 Screen_Log is stuck showing the old 13-column header even after this update
// ships. If "High20" isn't in the current header, patch just row 1 to SCREENLOG_HEADERS;
// every already-written data row is left exactly as it was -- its new High20 cell (column
// 14, past its own last real value) simply reads blank, which is accurate: that value was
// never computed for it.
function getOrCreateScreenLogSheet(ss) {
  var sh = getOrCreateSheet(ss, "Screen_Log");
  if (sh.getLastRow() > 0) {
    var currentHeader = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
    if (currentHeader.indexOf("High20") === -1) {
      sh.getRange(1, 1, 1, SCREENLOG_HEADERS.length).setValues([SCREENLOG_HEADERS]);
      styleHeaderRow(sh, SCREENLOG_HEADERS.length);
    }
  }
  return sh;
}

// ---- Data tabs -------------------------------------------------------------------

function writeLatestTop8(ss, body) {
  var sh = getShortlistSheet(ss);
  sh.clear();
  sh.getRange(1, 1, 1, TOP_HEADERS.length).setValues([TOP_HEADERS]);
  styleHeaderRow(sh, TOP_HEADERS.length);

  var rows = (body.top || []).map(function (r) {
    return [body.run_date, body.last_trading_date, r.Ticker, r.Name, r.Sector, r.LastPrice, r.LotCost,
            r.ADTV20, r.ATR14_pct, r.High20, r.DistToHigh20, r.ATRsBelowHigh, r.BreakoutLast3, r.Source || "Live"];
  });
  if (rows.length) {
    sh.getRange(2, 1, rows.length, TOP_HEADERS.length).setValues(rows);
    formatTop8Rows(sh, 2, rows.length);
  }
  sh.setColumnWidths(1, TOP_HEADERS.length, 110);
  sh.setColumnWidth(4, 200); // Name needs more room
}

// Deletes any existing Screen_Log rows for this LastTradingDate before appending fresh
// ones, so re-running the same trading day's screen (e.g. re-triggering the GitHub
// Actions workflow while debugging) replaces that day's block instead of duplicating it.
function removeExistingLogRows(sh, lastTradingDate) {
  var lastRow = sh.getLastRow();
  if (lastRow < 2) return;
  var tz = Session.getScriptTimeZone();
  var values = sh.getRange(2, 2, lastRow - 1, 1).getValues(); // column B = LastTradingDate
  var rowsToDelete = [];
  for (var i = 0; i < values.length; i++) {
    var cell = values[i][0];
    var cellStr = (cell instanceof Date) ? Utilities.formatDate(cell, tz, "yyyy-MM-dd") : String(cell);
    if (cellStr === lastTradingDate) rowsToDelete.push(2 + i);
  }
  for (var j = rowsToDelete.length - 1; j >= 0; j--) {
    sh.deleteRow(rowsToDelete[j]);
  }
}

function appendToLog(ss, body) {
  var sh = getOrCreateScreenLogSheet(ss);
  var isNew = sh.getLastRow() === 0;
  if (isNew) {
    sh.getRange(1, 1, 1, SCREENLOG_HEADERS.length).setValues([SCREENLOG_HEADERS]);
    styleHeaderRow(sh, SCREENLOG_HEADERS.length);
    sh.setColumnWidths(1, SCREENLOG_HEADERS.length, 110);
    sh.setColumnWidth(4, 200);
  } else {
    removeExistingLogRows(sh, body.last_trading_date);
  }
  var rows = (body.top || []).map(function (r) {
    return [body.run_date, body.last_trading_date, r.Ticker, r.Name, r.Sector, r.LastPrice, r.LotCost,
            r.ADTV20, r.ATR14_pct, r.DistToHigh20, r.ATRsBelowHigh, r.BreakoutLast3, r.Source || "Live", r.High20];
  });
  if (rows.length) {
    var startRow = sh.getLastRow() + 1;
    sh.getRange(startRow, 1, rows.length, SCREENLOG_HEADERS.length).setValues(rows);
    formatScreenLogRows(sh, startRow, rows.length);
  }
}

// Backfill-mode counterpart to appendToLog(): writes a whole POST batch (many days) in
// ONE pass instead of looping appendToLog() once per day. That loop was the root cause of
// the GitHub Actions batch-11 timeout -- appendToLog() -> removeExistingLogRows() does a
// full-column read + linear scan of the ENTIRE Screen_Log sheet on every call, and by
// batch 11 (day ~300) Screen_Log had grown to ~5,400+ rows, so 30 such full-sheet scans
// inside one doPost call pushed Apps Script's own processing time past the client's
// 180s urllib timeout (see backfill_screen_log.py's post_bulk()). This version reads the
// existing LastTradingDate column exactly ONCE per batch, deletes any rows that collide
// with a date in this batch (only relevant on a re-run of already-posted days -- a normal
// first pass has nothing to delete), then writes every new row for the batch with a
// single setValues() call. Cost per POST call is now O(sheet size) once, not O(30 x sheet
// size), independent of batch_size.
function appendToLogBulk(ss, runs) {
  var sh = getOrCreateScreenLogSheet(ss);
  var isNew = sh.getLastRow() === 0;
  if (isNew) {
    sh.getRange(1, 1, 1, SCREENLOG_HEADERS.length).setValues([SCREENLOG_HEADERS]);
    styleHeaderRow(sh, SCREENLOG_HEADERS.length);
    sh.setColumnWidths(1, SCREENLOG_HEADERS.length, 110);
    sh.setColumnWidth(4, 200);
  }

  var batchDates = {};
  runs.forEach(function (body) { batchDates[body.last_trading_date] = true; });

  var lastRow = sh.getLastRow();
  if (lastRow >= 2) {
    var tz = Session.getScriptTimeZone();
    var values = sh.getRange(2, 2, lastRow - 1, 1).getValues(); // column B = LastTradingDate, ONE read
    var rowsToDelete = [];
    for (var i = 0; i < values.length; i++) {
      var cell = values[i][0];
      var cellStr = (cell instanceof Date) ? Utilities.formatDate(cell, tz, "yyyy-MM-dd") : String(cell);
      if (batchDates[cellStr]) rowsToDelete.push(2 + i);
    }
    for (var j = rowsToDelete.length - 1; j >= 0; j--) {
      sh.deleteRow(rowsToDelete[j]); // descending order so earlier indices stay valid
    }
  }

  var allRows = [];
  runs.forEach(function (body) {
    (body.top || []).forEach(function (r) {
      allRows.push([body.run_date, body.last_trading_date, r.Ticker, r.Name, r.Sector, r.LastPrice, r.LotCost,
                    r.ADTV20, r.ATR14_pct, r.DistToHigh20, r.ATRsBelowHigh, r.BreakoutLast3, r.Source || "Live", r.High20]);
    });
  });
  if (allRows.length) {
    var startRow = sh.getLastRow() + 1;
    sh.getRange(startRow, 1, allRows.length, SCREENLOG_HEADERS.length).setValues(allRows);
    formatScreenLogRows(sh, startRow, allRows.length);
  }
}

// Every ticker that passed all 5 filters today (not just the sector-capped shortlist) --
// answers "show me all 50" directly, with IncludedInShortlist marking which ones made
// the capped Shortlist / Screen_Log list.
function writeAllPassers(ss, body) {
  var sh = getOrCreateSheet(ss, "All_Passers");
  sh.clear();
  sh.getRange(1, 1, 1, ALLPASSERS_HEADERS.length).setValues([ALLPASSERS_HEADERS]);
  styleHeaderRow(sh, ALLPASSERS_HEADERS.length);

  var rows = (body.all_passers || []).map(function (r) {
    return [body.run_date, body.last_trading_date, r.Ticker, r.Name, r.Sector, r.LastPrice,
            r.ADTV20, r.ATR14_pct, r.High20, r.DistToHigh20, r.ATRsBelowHigh, r.BreakoutLast3, r.IncludedInShortlist];
  });
  if (rows.length) {
    sh.getRange(2, 1, rows.length, ALLPASSERS_HEADERS.length).setValues(rows);
    sh.getRange(2, 1, rows.length, 1).setNumberFormat("yyyy-mm-dd");   // RunDate
    sh.getRange(2, 2, rows.length, 1).setNumberFormat("yyyy-mm-dd");   // LastTradingDate
    sh.getRange(2, 6, rows.length, 1).setNumberFormat("#,##0");        // LastPrice
    sh.getRange(2, 7, rows.length, 1).setNumberFormat("#,##0");        // ADTV20
    sh.getRange(2, 8, rows.length, 1).setNumberFormat("0.00%");        // ATR14_pct
    sh.getRange(2, 9, rows.length, 1).setNumberFormat("#,##0");        // High20
    sh.getRange(2, 10, rows.length, 1).setNumberFormat("0.00%");       // DistToHigh20
    sh.getRange(2, 11, rows.length, 1).setNumberFormat("0.00");        // ATRsBelowHigh
    borderRange(sh.getRange(2, 1, rows.length, ALLPASSERS_HEADERS.length));
  }
  sh.setColumnWidths(1, ALLPASSERS_HEADERS.length, 105);
  sh.setColumnWidth(4, 200); // Name
  sh.setColumnWidth(5, 160); // Sector
}

// Every ticker in today's universe (~950-980), pass or fail, with each of the 5 filters'
// individual TRUE/FALSE outcome plus PassesAll -- lets you see WHY a specific ticker isn't
// in All_Passers or Shortlist, not just that it isn't. body.all_tickers is only set by
// daily_screen.py's LIVE path (build_payload only attaches it when source=="Live") --
// backfill_screen_log.py never sends this field, and backfill mode never calls this
// function at all (see doPost's mode==="backfill" branch), so this tab simply holds
// whatever the last live run wrote through a backfill, same as Shortlist/All_Passers.
function writeAllTickers(ss, body) {
  var sh = getOrCreateSheet(ss, "All_Tickers");
  sh.clear();
  sh.getRange(1, 1, 1, ALLTICKERS_HEADERS.length).setValues([ALLTICKERS_HEADERS]);
  styleHeaderRow(sh, ALLTICKERS_HEADERS.length);

  // Sorted by ADTV20 descending (most liquid first), not left in whatever order Python sent.
  // Two reasons: (1) it's a more useful default reading order for a ~950-980 row tab than an
  // arbitrary/alphabetical one, and (2) buildLiveWatchAllTickersTab() below reads the first
  // LIVE_WATCH_ALLTICKERS_MAX_ROWS rows of THIS tab to decide which tickers get a live
  // GOOGLEFINANCE price -- sorting here is what makes "first N rows" equivalent to "top N by
  // liquidity" without a second sort/lookup step in that function.
  var allTickers = (body.all_tickers || []).slice().sort(function (a, b) {
    return (b.ADTV20 || 0) - (a.ADTV20 || 0);
  });
  var rows = allTickers.map(function (r) {
    return [body.run_date, body.last_trading_date, r.Ticker, r.Name, r.Sector, r.LastPrice,
            r.ADTV20, r.ATR14_pct, r.High20, r.DistToHigh20, r.ATRsBelowHigh, r.BreakoutLast3,
            r.F1_Liquidity, r.F2_PriceRange, r.F3_VolMomentum, r.F4_NotZeroTradeFlag,
            r.F5_NoPendingCA, r.PassesAll, r.IncludedInShortlist];
  });
  if (rows.length) {
    sh.getRange(2, 1, rows.length, ALLTICKERS_HEADERS.length).setValues(rows);
    sh.getRange(2, 1, rows.length, 1).setNumberFormat("yyyy-mm-dd");   // RunDate
    sh.getRange(2, 2, rows.length, 1).setNumberFormat("yyyy-mm-dd");   // LastTradingDate
    sh.getRange(2, 6, rows.length, 1).setNumberFormat("#,##0");        // LastPrice
    sh.getRange(2, 7, rows.length, 1).setNumberFormat("#,##0");        // ADTV20
    sh.getRange(2, 8, rows.length, 1).setNumberFormat("0.00%");        // ATR14_pct
    sh.getRange(2, 9, rows.length, 1).setNumberFormat("#,##0");        // High20
    sh.getRange(2, 10, rows.length, 1).setNumberFormat("0.00%");       // DistToHigh20
    sh.getRange(2, 11, rows.length, 1).setNumberFormat("0.00");        // ATRsBelowHigh
    borderRange(sh.getRange(2, 1, rows.length, ALLTICKERS_HEADERS.length));
  }
  sh.setColumnWidths(1, ALLTICKERS_HEADERS.length, 105);
  sh.setColumnWidth(4, 200); // Name
  sh.setColumnWidth(5, 160); // Sector
}

// Per-sector counts for the current run: how many tickers exist, how many passed all 5
// filters, and how many survived the 2-per-sector cap into the shortlist. Gives an
// at-a-glance view even on a day when 50 tickers pass and All_Passers is long.
function writeSectorBreakdown(ss, body) {
  var sh = getOrCreateSheet(ss, "Sector_Breakdown");
  sh.clear();
  sh.getRange(1, 1, 1, SECTOR_HEADERS.length).setValues([SECTOR_HEADERS]);
  styleHeaderRow(sh, SECTOR_HEADERS.length);

  var rows = (body.sector_breakdown || []).map(function (r) {
    var passRate = r.Universe ? r.Passed / r.Universe : 0;
    return [body.run_date, r.Sector, r.Universe, r.Passed, r.Shortlisted, passRate];
  });
  if (rows.length) {
    sh.getRange(2, 1, rows.length, SECTOR_HEADERS.length).setValues(rows);
    sh.getRange(2, 1, rows.length, 1).setNumberFormat("yyyy-mm-dd"); // RunDate
    sh.getRange(2, 6, rows.length, 1).setNumberFormat("0.0%");       // PassRate
    borderRange(sh.getRange(2, 1, rows.length, SECTOR_HEADERS.length));
  }
  sh.setColumnWidths(1, SECTOR_HEADERS.length, 110);
  sh.setColumnWidth(2, 190); // Sector
}

function writeNearMisses(ss, body) {
  var sh = getOrCreateSheet(ss, "Near_Misses");
  sh.clear();
  sh.getRange(1, 1, 1, NEARMISS_HEADERS.length).setValues([NEARMISS_HEADERS]);
  styleHeaderRow(sh, NEARMISS_HEADERS.length);

  var rows = (body.near_misses || []).map(function (r) {
    return [body.run_date, r.Ticker, r.Name, r.ADTV20, (r.FailedFilters || []).join(", ")];
  });
  if (rows.length) {
    sh.getRange(2, 1, rows.length, NEARMISS_HEADERS.length).setValues(rows);
    sh.getRange(2, 1, rows.length, 1).setNumberFormat("yyyy-mm-dd"); // RunDate
    sh.getRange(2, 4, rows.length, 1).setNumberFormat("#,##0");      // ADTV20
    borderRange(sh.getRange(2, 1, rows.length, NEARMISS_HEADERS.length));
  }
  sh.setColumnWidths(1, 3, 110);
  sh.setColumnWidth(3, 200);
  sh.setColumnWidth(5, 260);
}

function writeMeta(ss, body) {
  var sh = getOrCreateSheet(ss, "Run_Info");
  sh.clear();
  sh.getRange(1, 1).setValue("Last run:");
  sh.getRange(1, 2).setValue(body.run_date + " (data as of " + body.last_trading_date + ")");
  sh.getRange(2, 1).setValue("Universe size:");
  sh.getRange(2, 2).setValue(body.universe_count);
  sh.getRange(3, 1).setValue("Passed all 5 filters:");
  sh.getRange(3, 2).setValue(body.pass_count + "  (see All_Passers for the full list, Sector_Breakdown for counts by sector)");
  sh.getRange(4, 1).setValue("In shortlist (Shortlist tab):");
  sh.getRange(4, 2).setValue((body.top || []).length + "  (max 2 per IDX-IC sector)");
  sh.getRange(5, 1).setValue("Caveats:");
  (body.caveats || []).forEach(function (c, i) {
    sh.getRange(6 + i, 1, 1, 2).merge().setValue(c).setWrap(true);
  });

  sh.getRange(1, 1, 5, 1).setFontWeight("bold").setBackground(SECTION_BG);
  sh.setColumnWidth(1, 200);
  sh.setColumnWidth(2, 480);
  borderRange(sh.getRange(1, 1, 4 + Math.max((body.caveats || []).length, 1), 2));
}

// ---- Guide tab ---------------------------------------------------------------------
// Static reference content -- does not depend on `body`, rebuilt every run so it always
// matches this version of the script. Safe to also trigger by hand from the Apps Script
// editor (function dropdown -> buildGuideTab -> Run) if you want to see it without
// waiting for the next scheduled run.

function writeSectionHeader(sh, row, text, span) {
  sh.getRange(row, 1, 1, span).merge().setValue(text)
    .setFontWeight("bold").setBackground(HEADER_BG).setFontColor(HEADER_FONT)
    .setFontSize(11);
}

function writeTableHeader(sh, row, headers) {
  sh.getRange(row, 1, 1, headers.length).setValues([headers])
    .setFontWeight("bold").setBackground(SECTION_BG);
}

function buildGuideTab(ss) {
  // ss is passed automatically by doPost. Running this manually from the Apps Script
  // editor's function dropdown calls it with zero arguments, so fall back to the
  // active spreadsheet -- without this, ss is undefined and getSheetByName throws.
  ss = ss || SpreadsheetApp.getActiveSpreadsheet();
  var sh = getOrCreateSheet(ss, "Guide");
  sh.clear();
  sh.setColumnWidth(1, 150);
  sh.setColumnWidth(2, 260);
  sh.setColumnWidth(3, 420);
  sh.setColumnWidth(4, 260);

  var row = 1;

  // Title
  sh.getRange(row, 1, 1, 4).merge().setValue("IDX Daily Screener — Guide")
    .setFontWeight("bold").setFontSize(14).setBackground(HEADER_BG).setFontColor(HEADER_FONT);
  row++;
  sh.getRange(row, 1, 1, 4).merge()
    .setValue("This tab explains every column, filter, ranking rule, and caveat used across this " +
              "workbook. It is rebuilt automatically on every run and does not depend on today's data " +
              "-- editing it by hand will be overwritten on the next run.")
    .setFontStyle("italic").setWrap(true);
  sh.setRowHeight(row, 40);
  row += 2;

  // 1. Column definitions
  writeSectionHeader(sh, row, "1. Column definitions", 4);
  row++;
  writeTableHeader(sh, row, ["Column", "Meaning", "Formula / definition", ""]);
  row++;
  var colDefStart = row;
  var colDefs = [
    ["RunDate", "Date this screen was executed (server clock, GitHub Actions runner = UTC).", "System timestamp at run time.", ""],
    ["LastTradingDate", "Most recent trading session reflected in the underlying data.", "MAX(Date) across stock_summary.parquet.", ""],
    ["Ticker", "IDX stock code.", "As published by IDX.", ""],
    ["Name", "Company name.", "As published by IDX.", ""],
    ["Sector", "IDX-IC (level 1) sector classification.", "From financial_ratios.parquet's company-profile snapshot -- a periodic dataset, not refreshed by every daily run. See caveats.", ""],
    ["LastPrice", "Closing price on LastTradingDate, in IDR.", "Close price, most recent session.", ""],
    ["LotCost", "Cost of one round lot (100 shares) at LastPrice, in IDR.", "LastPrice × 100.", ""],
    ["ADTV20", "Average Daily Traded Value over the last 20 sessions, in IDR. Used only to check the F1 liquidity gate -- not used to rank or sort the pass list.", "MEAN(Value) over the most recent 20 trading days.", ""],
    ["ATR14_pct", "14-day Average True Range, scaled to a % of LastPrice -- a volatility measure.",
      "TrueRange = MAX(High-Low, |High-PrevClose|, |Low-PrevClose|); ATR14 = MEAN(TrueRange) over 14 sessions; ATR14_pct = ATR14 / LastPrice.", ""],
    ["High20", "The 20-day-high price itself, in IDR -- the raw input DistToHigh20 and ATRsBelowHigh are both computed from. Shown so you can rebuild either by hand. Sits between ATR14_pct and DistToHigh20 in Shortlist/All_Passers/All_Tickers; on Screen_Log specifically it's the LAST column instead (added after that tab already had years of history under a 13-column schema -- inserting it in the middle there would've shifted every later field in every pre-High20 row). Blank on any Screen_Log row from before this field existed.", "MAX(High) over the last 20 trading sessions.", ""],
    ["DistToHigh20", "How far below the 20-day high LastPrice currently sits, as a %.", "(High20 − LastPrice) / High20.", ""],
    ["ATRsBelowHigh", "Same idea as DistToHigh20, but scaled by the stock's OWN volatility instead of price -- a quality measure for a breakout. 0 or negative = at/above the high; 1.0 = pulled back one full average day's range.", "(High20 − LastPrice) / ATR14 (ATR14 in IDR, i.e. ATR14_pct × LastPrice).", ""],
    ["BreakoutLast3", "TRUE if a new 20-day high was set within the last 3 sessions.", "MAX(High) over the last 3 days > MAX(High) over the prior days 4–20.", ""],
    ["IncludedInShortlist", "(All_Passers only) TRUE if this ticker survived the 2-per-sector cap into Shortlist / Screen_Log.", "See section 3 below.", ""],
    ["Source", "(Shortlist / Screen_Log only) 'Live' = a real daily run; 'Backfill' = a one-time historical replay. Weight backtest conclusions accordingly -- Backfill rows have hindsight bias on F5/Sector (see caveats), Live rows don't.", "Set by daily_screen.py / backfill_screen_log.py.", ""],
  ];
  sh.getRange(row, 1, colDefs.length, 4).setValues(colDefs).setWrap(true).setVerticalAlignment("top");
  borderRange(sh.getRange(colDefStart - 1, 1, colDefs.length + 1, 4));
  row += colDefs.length + 2;

  // 2. The 5 filters
  writeSectionHeader(sh, row, "2. The 5 filters (a ticker must pass ALL 5 to appear in All_Passers)", 4);
  row++;
  writeTableHeader(sh, row, ["Filter", "Threshold / definition", "", ""]);
  row++;
  var filterStart = row;
  var filters = [
    ["F1 — Liquidity", "ADTV20 ≥ Rp 5,000,000,000.", "", ""],
    ["F2 — Price range", "50 ≤ LastPrice ≤ 3,000 IDR (capital-constrained, 100-share lots).", "", ""],
    ["F3 — Volatility / momentum", "ATR14_pct ≥ 3% AND (\n" +
      "  • no breakout yet: DistToHigh20 ≤ 5% (consolidating near the old high), OR\n" +
      "  • breakout fired (BreakoutLast3 = TRUE): ATRsBelowHigh ≤ 1.0 (still holding near the fresh high, " +
      "not round-tripped back down -- excludes failed breakouts).\n" +
      "Each branch applies to a different setup; a ticker only needs the branch that matches its own BreakoutLast3 state.", "", ""],
    ["F4 — Not ARA/ARB-locked or under UMA notice",
      "Proxy only: excluded if BOTH Volume and Frequency were zero over the last 3 sessions. " +
      "This is NOT IDX's official suspension/UMA feed -- verify manually before acting.", "", ""],
    ["F5 — No pending corporate action", "No rights issue, split, delisting, etc. recorded in " +
      "corporate_actions.parquet with a record date (TanggalPencatatan) within the next 10 trading days.", "", ""],
  ];
  sh.getRange(row, 1, filters.length, 4).setValues(filters).setWrap(true).setVerticalAlignment("top");
  borderRange(sh.getRange(filterStart - 1, 1, filters.length + 1, 4));
  row += filters.length + 2;

  // 3. Ranking & shortlist construction
  writeSectionHeader(sh, row, "3. How the shortlist (Shortlist / Screen_Log) is built from All_Passers", 4);
  row++;
  var rankStart = row;
  var rankSteps = [
    "Step 1 -- rank every passer on the screen's OWN dimensions, not ADTV20 (ADTV20 only restates the F1 gate " +
      "and its wide range across the pass list would otherwise dominate the order): sort ascending by " +
      "ATRsBelowHigh (cleanest setups -- at/near their own high, scaled by volatility -- first), then " +
      "descending by ATR14_pct as a tiebreak (stronger move wins between two equally clean setups).",
    "Step 2 -- cap at 2 tickers per IDX-IC Sector, walking down the ranked list in order and skipping any " +
      "ticker once its sector already has 2 entries in the shortlist. This keeps the shortlist from being one " +
      "crowded sector on days when a single theme (e.g. commodities) dominates the passers.",
    "If Sector is unavailable for this run (financial_ratios.parquet missing, or every passer shows " +
      "'Unclassified'), the cap is skipped entirely and Run_Info / the caveats below will say so -- the " +
      "shortlist then falls back to the full ranked list with no sector cap applied.",
    "This ranking and the 2-per-sector cap are both modeling choices, not market facts -- e.g. ranking by " +
      "ATR14_pct first (raw momentum) instead of ATRsBelowHigh first (cleanliness) is an equally defensible " +
      "alternative ordering.",
  ];
  rankSteps.forEach(function (s, i) {
    sh.getRange(rankStart + i, 1, 1, 4).merge().setValue("• " + s).setWrap(true);
    sh.setRowHeight(rankStart + i, 55);
  });
  borderRange(sh.getRange(rankStart, 1, rankSteps.length, 4));
  row += rankSteps.length + 1;

  // 4. Where to look
  writeSectionHeader(sh, row, "4. Where to look", 4);
  row++;
  writeTableHeader(sh, row, ["Tab", "What's in it", "", ""]);
  row++;
  var tabStart = row;
  var tabs = [
    ["Shortlist", "Today's shortlist (renamed from Latest_Top8 -- row count is no longer fixed at 8): All_Passers ranked by ATRsBelowHigh/ATR14_pct and capped at 2 per sector (see section 3). Row count varies day to day. Overwritten every run.", "", ""],
    ["Screen_Log", "Running history: each day's Shortlist is appended here, keyed by LastTradingDate. Re-running the same trading day's screen replaces that day's block instead of duplicating it. Can include one-time historical rows from backfill_screen_log.py -- check the Source column ('Live' vs 'Backfill') before treating this as a clean backtest series.", "", ""],
    ["All_Passers", "EVERY ticker that passed all 5 filters today (e.g. all 50 on a high-pass day), with Sector and an IncludedInShortlist flag. Overwritten every run.", "", ""],
    ["All_Tickers", "The FULL universe (~950-980 tickers), pass or fail, with each of the 5 filters' individual TRUE/FALSE outcome plus PassesAll -- use this to see exactly why a specific ticker isn't in All_Passers or Shortlist. Live-run only, same as All_Passers -- untouched by backfill_screen_log.py. Overwritten every run.", "", ""],
    ["Sector_Breakdown", "Per-sector counts for today: universe size, how many passed, how many made the shortlist, and the pass rate. Overwritten every run.", "", ""],
    ["Near_Misses", "Tickers that failed exactly one filter, up to 10, sorted by ADTV20 (a deliberate exception to section 3 -- these failed, so ADTV20 isn't restating a gate they passed). Always populated now, not just on low-pass days.", "", ""],
    ["Run_Info", "Last run timestamp, universe size, pass count, shortlist size, and this run's specific data-quality caveats.", "", ""],
    ["Live_Watch", "Today's Shortlist tickers with a live(ish) price via GOOGLEFINANCE (Sheets-native, not the Python pipeline) -- ~20 min delayed, and not every smaller IDX ticker has data. For gauging how far price has moved since the screen flagged it, not for order timing. Formulas only, no data of their own -- rebuilt every run, always current as long as the Sheet is open.", "", ""],
    ["Live_Watch_Passers", "Same idea as Live_Watch, but sourced from All_Passers instead of Shortlist -- every ticker that passed all 5 filters today, not just the sector-capped shortlist. Provisioned for up to " + LIVE_WATCH_PASSERS_MAX_ROWS + " tickers.", "", ""],
    ["Live_Watch_AllTickers", "Same idea again, but sourced from All_Tickers (the full ~950-980 universe) -- capped to the top " + LIVE_WATCH_ALLTICKERS_MAX_ROWS + " tickers by ADTV20 (liquidity), not the whole universe. That cap is a deliberate choice, not a technical wall: GOOGLEFINANCE has no documented limit on concurrent formulas per sheet, and Live_Watch + Live_Watch_Passers + a full-universe version would run roughly 2,700 of them at once with no track record at that scale. Started at 250, lowered to " + LIVE_WATCH_ALLTICKERS_MAX_ROWS + " as a more conservative liquidity-weighted middle ground; raise LIVE_WATCH_ALLTICKERS_MAX_ROWS in the script if you want more coverage and are willing to test the sheet at that size.", "", ""],
  ];
  sh.getRange(row, 1, tabs.length, 4).setValues(tabs).setWrap(true).setVerticalAlignment("top");
  borderRange(sh.getRange(tabStart - 1, 1, tabs.length + 1, 4));
  row += tabs.length + 2;

  // 5. Caveats
  writeSectionHeader(sh, row, "5. Caveats — always check this run's Run_Info tab too", 4);
  row++;
  var caveatStart = row;
  var caveats = [
    "corporate_actions.parquet freshness varies run to run -- if Run_Info shows it's more than a few days stale, treat F5 as informational only, not a hard guarantee.",
    "F4 is a same-day zero-volume / zero-frequency proxy for suspension, not IDX's official ARA/ARB/UMA feed. A ticker can be genuinely illiquid (fails F4 for reasons unrelated to a halt) or, more rarely, halted without yet showing zero volume for 3 full sessions.",
    "Sector comes from financial_ratios.parquet, a periodic company-profile/financial-statement snapshot -- it is not refreshed by every daily run, only when `idx financial` + `idx parquet` are re-run. Newly listed tickers may show as 'Unclassified' until then; sector assignment itself changes rarely, so this is lower-risk than it would be for a ratio like ROE.",
    "This tool reports which tickers pass objective, mechanical filters, and how they're ranked. It does not give buy/sell recommendations, stop-losses, position sizes, or price targets -- those decisions and the risk are yours.",
  ];
  caveats.forEach(function (c, i) {
    sh.getRange(caveatStart + i, 1, 1, 4).merge().setValue("• " + c).setWrap(true);
    sh.setRowHeight(caveatStart + i, 40);
  });
  borderRange(sh.getRange(caveatStart, 1, caveats.length, 4));
  row += caveats.length + 1;

  sh.setFrozenRows(0); // guide is read top-to-bottom, no need to freeze anything
}

// ---- Live_Watch tabs -----------------------------------------------------------------
// Formulas only -- no data written by this script beyond the formula text itself. Each
// row's Ticker/Name/screen price are pulled from a source tab by cell reference, and the
// live price / today's change come from GOOGLEFINANCE, which recalculates on its own on a
// Sheets-managed cycle while the Sheet is open -- this function does not need to run again
// for those to update.
//
// Shared by two tabs because Shortlist and All_Passers both put Ticker/Name/LastPrice in
// columns C/D/F with data starting row 2 -- the exact layout this engine assumes. Sharing
// (instead of duplicating buildLiveWatchTab twice) means the semicolon-locale fix below
// has exactly one place to go out of sync if it's ever touched again, not two.
//
// NB: argument separator is ";" throughout, not ",". This Sheet's locale (Indonesia) uses
// a comma as the DECIMAL separator, so Sheets expects ";" between function arguments (same
// reason an Indonesian-locale sheet writes SUM(A1;A2) instead of SUM(A1,A2)). A
// comma-separated formula written by a script still parses as literal text against this
// locale's grammar and every cell shows #ERROR! -- this bit us once already (every column
// erroring uniformly, including ones with no GOOGLEFINANCE call, was the tell: a parse
// failure, not a data/coverage issue). Keep semicolons if you ever add formulas here.
function buildLiveWatchFromSource(ss, sourceTab, tabName, maxRows, capNote) {
  ss = ss || SpreadsheetApp.getActiveSpreadsheet(); // see buildGuideTab's comment on why
  var sh = getOrCreateSheet(ss, tabName);
  sh.clear();

  var headers = ["Ticker", "Name", "Screen LastPrice (IDR)", "Live Price (IDR, ~20min delay)",
                 "Change vs Screen", "Today's Change (live)"];
  sh.getRange(1, 1, 1, headers.length).setValues([headers]);
  styleHeaderRow(sh, headers.length);

  var tailNote = capNote ||
    ("Rows beyond today's actual " + sourceTab + " row count just show blank.");
  sh.getRange(2, 1, 1, headers.length).merge()
    .setValue("GOOGLEFINANCE (Sheets-native, not the Python pipeline), sourced from " + sourceTab + ": " +
              "~20 min delayed, and not every smaller/less-liquid IDX ticker has data (shows \"No data\" " +
              "when GOOGLEFINANCE can't find it). Use this to gauge how far price has moved since the " +
              "screen flagged it at LastTradingDate's close -- not as an order-timing or execution-price " +
              "reference. " + tailNote)
    .setFontStyle("italic").setWrap(true).setBackground(SECTION_BG);
  sh.setRowHeight(2, 40);

  var startRow = 3;
  var formulas = [];
  for (var i = 0; i < maxRows; i++) {
    var r = startRow + i;               // this Live_Watch* row
    var srcRow = 2 + i;                 // corresponding sourceTab row (its header is row 1 too)
    var ticker = sourceTab + "!C" + srcRow;
    var name = sourceTab + "!D" + srcRow;
    var screenPrice = sourceTab + "!F" + srcRow;
    formulas.push([
      "=IFERROR(IF(" + ticker + "=\"\";\"\";" + ticker + ");\"\")",
      "=IFERROR(IF(" + ticker + "=\"\";\"\";" + name + ");\"\")",
      "=IFERROR(IF(" + ticker + "=\"\";\"\";" + screenPrice + ");\"\")",
      "=IF(A" + r + "=\"\";\"\";IFERROR(GOOGLEFINANCE(\"IDX:\"&A" + r + ";\"price\");\"No data\"))",
      "=IF(OR(A" + r + "=\"\";NOT(ISNUMBER(D" + r + "));C" + r + "=0);\"\";(D" + r + "-C" + r + ")/C" + r + ")",
      "=IF(OR(A" + r + "=\"\";NOT(ISNUMBER(D" + r + ")));\"\";IFERROR(GOOGLEFINANCE(\"IDX:\"&A" + r + ";\"changepct\")/100;\"\"))",
    ]);
  }
  var range = sh.getRange(startRow, 1, maxRows, headers.length);
  range.setFormulas(formulas);
  range.setVerticalAlignment("middle");
  sh.getRange(startRow, 3, maxRows, 1).setNumberFormat("#,##0");      // Screen LastPrice
  sh.getRange(startRow, 4, maxRows, 1).setNumberFormat("#,##0");      // Live Price
  sh.getRange(startRow, 5, maxRows, 1).setNumberFormat("0.00%");      // Change vs Screen
  sh.getRange(startRow, 6, maxRows, 1).setNumberFormat("0.00%");      // Today's Change
  borderRange(sh.getRange(1, 1, maxRows + startRow - 1, headers.length));

  sh.setColumnWidth(1, 90);
  sh.setColumnWidth(2, 220);
  sh.setColumnWidth(3, 150);
  sh.setColumnWidth(4, 170);
  sh.setColumnWidth(5, 120);
  sh.setColumnWidth(6, 140);
  sh.setFrozenRows(2);
}

// Provisions LIVE_WATCH_MAX_ROWS rows regardless of today's actual shortlist size (theoretical
// max is MAX_PER_SECTOR x number of IDX-IC sectors+Unclassified = 2 x 12 = 24 in daily_screen.py
// as of this version) so it never silently truncates a large shortlist; rows beyond today's
// shortlist size just show blank (IF(...="","",...) above).
var LIVE_WATCH_MAX_ROWS = 30;
// All_Passers isn't sector-capped, so its row count runs higher and more variably than the
// shortlist's -- observed pass_count has ranged from the teens to the 60s across live and
// backfilled days. 100 gives headroom above that observed range; raise it if a day ever
// actually fills all 100 rows (Run_Info's "Passed all 5 filters" count says whether it did).
var LIVE_WATCH_PASSERS_MAX_ROWS = 100;
// The full universe is ~950-980 tickers -- live-tracking all of them would add roughly 1,960
// more concurrent GOOGLEFINANCE formulas on top of the ~780 Live_Watch + Live_Watch_Passers
// already use, with no documented ceiling found on how many GOOGLEFINANCE calls a Sheet can
// run concurrently before slowing down or erroring. Capped middle ground instead: only the
// most-liquid 100 tickers by ADTV20 (writeAllTickers() sorts All_Tickers by ADTV20 descending
// specifically so its first 100 rows ARE that top-100 set -- no separate sort needed here).
// Started at 250, lowered to 100 after the first live run -- raise/lower
// LIVE_WATCH_ALLTICKERS_MAX_ROWS again if 100 turns out to be too many or too few.
var LIVE_WATCH_ALLTICKERS_MAX_ROWS = 100;

function buildLiveWatchTab(ss) {
  buildLiveWatchFromSource(ss, SHORTLIST_TAB_NAME, "Live_Watch", LIVE_WATCH_MAX_ROWS);
}

function buildLiveWatchPassersTab(ss) {
  buildLiveWatchFromSource(ss, "All_Passers", "Live_Watch_Passers", LIVE_WATCH_PASSERS_MAX_ROWS);
}

function buildLiveWatchAllTickersTab(ss) {
  buildLiveWatchFromSource(ss, "All_Tickers", "Live_Watch_AllTickers", LIVE_WATCH_ALLTICKERS_MAX_ROWS,
    "All_Tickers holds the full ~950-980 ticker universe, sorted by ADTV20 descending -- this tab " +
    "only live-tracks the top " + LIVE_WATCH_ALLTICKERS_MAX_ROWS + " by that liquidity ranking, not " +
    "every ticker (see Guide for why). A ticker missing here just isn't in the top " +
    LIVE_WATCH_ALLTICKERS_MAX_ROWS + " today -- check All_Tickers directly for its filter results.");
}

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
//
// IMPORTANT: always reads exactly DAILY_PRICES_HEADERS.length columns (not
// sh.getLastColumn()) for this comparison. An earlier version used
// Math.max(sh.getLastColumn(), 1) as the read width, which means ANY stray content or
// formatting landing in column I or beyond -- e.g. a click/selection in the Sheet UI while
// a backfill run is in progress -- inflates getLastColumn() past 8, so currentHeader picks
// up extra trailing blank cells, currentHeader.join("|") no longer equals
// DAILY_PRICES_HEADERS.join("|"), and this function wipes the ENTIRE tab (sh.clear()) and
// starts over mid-backfill. That is what corrupted a real backfill run once already --
// batches from a stray-column event onward were kept, everything posted before it was
// silently lost even though the client-side script had already checkpointed those dates as
// successfully posted (Apps Script really did write them; something after that cleared the
// sheet). Fixed by reading a fixed-width range instead, so extra columns elsewhere on the
// row can never affect this check.
function getOrResetDailyPricesSheet(ss) {
  var sh = getOrCreateSheet(ss, DAILY_PRICES_TAB_NAME);
  var lastRow = sh.getLastRow();
  var needsReset = true;
  if (lastRow > 0) {
    var currentHeader = sh.getRange(1, 1, 1, DAILY_PRICES_HEADERS.length).getValues()[0];
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
// ── Row-count audit for "Daily All Tickers" (stock_summary) ────────────────
// One-off diagnostic: =COUNTA(C2:C) on this tab read 339405 against an
// expected 339408 from the clean source parquet (0 duplicate (StockCode,Date)
// pairs, 0 null Dates/StockCodes, verified independently) -- a 3-row gap that
// appeared AFTER the getOrResetDailyPricesSheet reset-bug fix above was
// already confirmed exact (that fix's own backfill re-run posted exactly the
// predicted 105,476 rows across 22 batches, all HTTP 200). This function
// re-counts the live sheet's rows per trading date (column C) and compares
// against EXPECTED_STOCK_SUMMARY_COUNTS -- the per-date row count computed
// from the clean source -- to find which specific date(s) are short, which is
// a far narrower place to look for the missing 3 rows than the raw total.
// Also flags any date value present in column C that ISN'T one of the 354
// expected trading dates (2025-03-11..2026-09-11), which would point to a
// stray/garbage value rather than a simple undercount.
//
// Run: open this project in the Apps Script editor, select "auditDailyPricesCounts"
// in the function dropdown, click Run, then View > Logs (or View > Executions)
// for the output. Read-only -- makes no changes to the sheet.
var EXPECTED_STOCK_SUMMARY_COUNTS = [
  ["2025-03-11",957], ["2025-03-12",957], ["2025-03-13",957], ["2025-03-14",957], ["2025-03-17",957], ["2025-03-18",957], ["2025-03-19",957], ["2025-03-20",957], ["2025-03-21",957], ["2025-03-24",957], ["2025-03-25",958], ["2025-03-26",958], ["2025-03-27",958], ["2025-04-08",958], ["2025-04-09",958], ["2025-04-10",958], ["2025-04-11",958], ["2025-04-14",959], ["2025-04-15",960], ["2025-04-16",960], ["2025-04-17",959], ["2025-04-21",959], ["2025-04-22",959], ["2025-04-23",959], ["2025-04-24",959], ["2025-04-25",959], ["2025-04-28",959], ["2025-04-29",959], ["2025-04-30",959], ["2025-05-02",959], ["2025-05-05",959], ["2025-05-06",959], ["2025-05-07",959], ["2025-05-08",960], ["2025-05-09",960], ["2025-05-14",960], ["2025-05-15",960], ["2025-05-16",960], ["2025-05-19",960], ["2025-05-20",960], ["2025-05-21",960], ["2025-05-22",960], ["2025-05-23",960], ["2025-05-26",960], ["2025-05-27",960], ["2025-05-28",960], ["2025-06-02",960], ["2025-06-03",960], ["2025-06-04",960], ["2025-06-05",960], ["2025-06-10",960], ["2025-06-11",960], ["2025-06-12",960], ["2025-06-13",960], ["2025-06-16",960], ["2025-06-17",960], ["2025-06-18",960], ["2025-06-19",960], ["2025-06-20",960], ["2025-06-23",960], ["2025-06-24",960], ["2025-06-25",960], ["2025-06-26",960], ["2025-06-30",960], ["2025-07-01",960], ["2025-07-02",960], ["2025-07-03",960], ["2025-07-04",960], ["2025-07-07",960], ["2025-07-08",962], ["2025-07-09",964], ["2025-07-10",968], ["2025-07-11",968], ["2025-07-14",968], ["2025-07-15",968], ["2025-07-16",968], ["2025-07-17",968], ["2025-07-18",968], ["2025-07-21",956], ["2025-07-22",956], ["2025-07-23",956], ["2025-07-24",956], ["2025-07-25",956], ["2025-07-28",956], ["2025-07-29",956], ["2025-07-30",956], ["2025-07-31",956], ["2025-08-01",956], ["2025-08-04",956], ["2025-08-05",956], ["2025-08-06",956], ["2025-08-07",956], ["2025-08-08",956], ["2025-08-11",956], ["2025-08-12",956], ["2025-08-13",956], ["2025-08-14",956], ["2025-08-15",956], ["2025-08-19",956], ["2025-08-20",956], ["2025-08-21",956], ["2025-08-22",956], ["2025-08-25",956], ["2025-08-26",956], ["2025-08-27",956], ["2025-08-28",956], ["2025-08-29",956], ["2025-09-01",956], ["2025-09-02",956], ["2025-09-03",956], ["2025-09-04",956], ["2025-09-08",956], ["2025-09-09",956], ["2025-09-10",956], ["2025-09-11",956], ["2025-09-12",956], ["2025-09-15",956], ["2025-09-16",956], ["2025-09-17",956], ["2025-09-18",956], ["2025-09-19",956], ["2025-09-22",956], ["2025-09-23",957], ["2025-09-24",957], ["2025-09-25",957], ["2025-09-26",957], ["2025-09-29",957], ["2025-09-30",957], ["2025-10-01",957], ["2025-10-02",956], ["2025-10-03",956], ["2025-10-06",956], ["2025-10-07",956], ["2025-10-08",956], ["2025-10-09",956], ["2025-10-10",956], ["2025-10-13",956], ["2025-10-14",956], ["2025-10-15",956], ["2025-10-16",956], ["2025-10-17",956], ["2025-10-20",956], ["2025-10-21",956], ["2025-10-22",956], ["2025-10-23",956], ["2025-10-24",956], ["2025-10-27",956], ["2025-10-28",956], ["2025-10-29",956], ["2025-10-30",955], ["2025-10-31",955], ["2025-11-03",955], ["2025-11-04",955], ["2025-11-05",955], ["2025-11-06",956], ["2025-11-07",956], ["2025-11-10",956], ["2025-11-11",956], ["2025-11-12",956], ["2025-11-13",956], ["2025-11-14",956], ["2025-11-17",956], ["2025-11-18",956], ["2025-11-19",956], ["2025-11-20",956], ["2025-11-21",956], ["2025-11-24",956], ["2025-11-25",956], ["2025-11-26",956], ["2025-11-27",956], ["2025-11-28",956], ["2025-12-01",956], ["2025-12-02",956], ["2025-12-03",956], ["2025-12-04",956], ["2025-12-05",956], ["2025-12-08",957], ["2025-12-09",957], ["2025-12-10",957], ["2025-12-11",957], ["2025-12-12",957], ["2025-12-15",957], ["2025-12-16",957], ["2025-12-17",958], ["2025-12-18",958], ["2025-12-19",958], ["2025-12-22",958], ["2025-12-23",958], ["2025-12-24",958], ["2025-12-29",958], ["2025-12-30",958], ["2026-01-02",958], ["2026-01-05",958], ["2026-01-06",958], ["2026-01-07",958], ["2026-01-08",958], ["2026-01-09",958], ["2026-01-12",958], ["2026-01-13",958], ["2026-01-14",958], ["2026-01-15",958], ["2026-01-19",958], ["2026-01-20",958], ["2026-01-21",958], ["2026-01-22",958], ["2026-01-23",958], ["2026-01-26",958], ["2026-01-27",958], ["2026-01-28",958], ["2026-01-29",958], ["2026-01-30",958], ["2026-02-02",958], ["2026-02-03",958], ["2026-02-04",958], ["2026-02-05",958], ["2026-02-06",958], ["2026-02-09",958], ["2026-02-10",958], ["2026-02-11",958], ["2026-02-12",958], ["2026-02-13",958], ["2026-02-18",958], ["2026-02-19",958], ["2026-02-20",958], ["2026-02-23",958], ["2026-02-24",958], ["2026-02-25",958], ["2026-02-26",958], ["2026-02-27",958], ["2026-03-02",958], ["2026-03-03",958], ["2026-03-04",958], ["2026-03-05",958], ["2026-03-06",958], ["2026-03-09",958], ["2026-03-10",958], ["2026-03-11",958], ["2026-03-12",958], ["2026-03-13",958], ["2026-03-16",958], ["2026-03-17",958], ["2026-03-25",958], ["2026-03-26",958], ["2026-03-27",958], ["2026-03-30",958], ["2026-03-31",958], ["2026-04-01",958], ["2026-04-02",958], ["2026-04-06",958], ["2026-04-07",958], ["2026-04-08",958], ["2026-04-09",958], ["2026-04-10",959], ["2026-04-13",959], ["2026-04-14",959], ["2026-04-15",959], ["2026-04-16",959], ["2026-04-17",959], ["2026-04-20",959], ["2026-04-21",959], ["2026-04-22",959], ["2026-04-23",959], ["2026-04-24",959], ["2026-04-27",959], ["2026-04-28",959], ["2026-04-29",959], ["2026-04-30",959], ["2026-05-04",959], ["2026-05-05",959], ["2026-05-06",959], ["2026-05-07",959], ["2026-05-08",959], ["2026-05-11",959], ["2026-05-12",959], ["2026-05-13",959], ["2026-05-18",959], ["2026-05-19",959], ["2026-05-20",959], ["2026-05-21",959], ["2026-05-22",959], ["2026-05-25",959], ["2026-05-26",959], ["2026-05-29",959], ["2026-06-02",959], ["2026-06-03",959], ["2026-06-04",959], ["2026-06-05",959], ["2026-06-08",959], ["2026-06-09",959], ["2026-06-10",959], ["2026-06-11",959], ["2026-06-12",959], ["2026-06-15",959], ["2026-06-17",959], ["2026-06-18",959], ["2026-06-19",959], ["2026-06-22",959], ["2026-06-23",959], ["2026-06-24",959], ["2026-06-25",959], ["2026-06-26",959], ["2026-06-29",959], ["2026-06-30",959], ["2026-07-01",959], ["2026-07-02",959], ["2026-07-03",959], ["2026-07-06",959], ["2026-07-07",961], ["2026-07-08",963], ["2026-07-09",964], ["2026-07-10",965], ["2026-07-13",965], ["2026-07-14",965], ["2026-07-15",965], ["2026-07-16",965], ["2026-07-17",965], ["2026-07-20",965], ["2026-07-21",965], ["2026-07-22",965], ["2026-07-23",965], ["2026-07-24",965], ["2026-07-27",965], ["2026-07-28",965], ["2026-07-29",965], ["2026-07-30",963], ["2026-07-31",963], ["2026-08-03",963], ["2026-08-04",963], ["2026-08-05",963], ["2026-08-06",963], ["2026-08-07",963], ["2026-08-10",963], ["2026-08-11",963], ["2026-08-12",963], ["2026-08-13",963], ["2026-08-14",963], ["2026-08-18",963], ["2026-08-19",963], ["2026-08-20",963], ["2026-08-21",963], ["2026-08-24",963], ["2026-08-26",963], ["2026-08-27",963], ["2026-08-28",963], ["2026-08-31",963], ["2026-09-01",963], ["2026-09-02",963], ["2026-09-03",963], ["2026-09-04",963], ["2026-09-07",963], ["2026-09-08",963], ["2026-09-09",963], ["2026-09-10",963], ["2026-09-11",963]
];

function auditDailyPricesCounts() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(DAILY_PRICES_TAB_NAME);
  if (!sh) {
    Logger.log("Sheet '" + DAILY_PRICES_TAB_NAME + "' not found.");
    return;
  }
  var lastRow = sh.getLastRow();
  if (lastRow < 2) {
    Logger.log("No data rows found.");
    return;
  }
  // Column C = Date (3rd column -- see DAILY_PRICES_HEADERS / the row shape
  // appendDailyPricesInit and appendDailyPrices both write).
  var dateVals = sh.getRange(2, 3, lastRow - 1, 1).getValues();
  var tz = Session.getScriptTimeZone();
  var actualCounts = {};
  for (var i = 0; i < dateVals.length; i++) {
    var d = dateVals[i][0];
    var key = (d instanceof Date) ? Utilities.formatDate(d, tz, "yyyy-MM-dd") : String(d).trim();
    actualCounts[key] = (actualCounts[key] || 0) + 1;
  }

  var mismatches = [];
  var expectedTotal = 0;
  var expectedDatesSeen = {};
  for (var j = 0; j < EXPECTED_STOCK_SUMMARY_COUNTS.length; j++) {
    var date = EXPECTED_STOCK_SUMMARY_COUNTS[j][0];
    var expected = EXPECTED_STOCK_SUMMARY_COUNTS[j][1];
    expectedDatesSeen[date] = true;
    expectedTotal += expected;
    var actual = actualCounts[date] || 0;
    if (actual !== expected) {
      mismatches.push([date, expected, actual, actual - expected]);
    }
  }

  var unexpectedDates = [];
  for (var key2 in actualCounts) {
    if (!expectedDatesSeen[key2]) unexpectedDates.push([key2, actualCounts[key2]]);
  }

  Logger.log("=== Daily All Tickers row-count audit ===");
  Logger.log("Total rows in sheet (excl. header): " + dateVals.length);
  Logger.log("Expected total (source parquet):    " + expectedTotal);
  Logger.log("Difference: " + (dateVals.length - expectedTotal));
  Logger.log("");
  if (mismatches.length === 0) {
    Logger.log("No per-date mismatches found against the 354 expected trading dates.");
  } else {
    Logger.log(mismatches.length + " date(s) with a row-count mismatch:");
    for (var k = 0; k < mismatches.length; k++) {
      Logger.log("  " + mismatches[k][0] + ": expected " + mismatches[k][1] +
        ", actual " + mismatches[k][2] + " (diff " + mismatches[k][3] + ")");
    }
  }
  Logger.log("");
  if (unexpectedDates.length === 0) {
    Logger.log("No unexpected date values found in column C.");
  } else {
    Logger.log(unexpectedDates.length + " unexpected date value(s) in column C (not among the 354 expected trading dates):");
    for (var m = 0; m < unexpectedDates.length; m++) {
      Logger.log("  '" + unexpectedDates[m][0] + "': " + unexpectedDates[m][1] + " row(s)");
    }
  }
}
