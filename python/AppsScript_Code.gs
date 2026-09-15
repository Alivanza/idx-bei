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
  buildLiveWatchAllTickersTab(ss); // same idea again, capped to the top-250-by-ADTV20 of All_Tickers

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
  var sh = getOrCreateSheet(ss, "Screen_Log");
  var isNew = sh.getLastRow() === 0;
  if (isNew) {
    sh.getRange(1, 1, 1, TOP_HEADERS.length).setValues([TOP_HEADERS]);
    styleHeaderRow(sh, TOP_HEADERS.length);
    sh.setColumnWidths(1, TOP_HEADERS.length, 110);
    sh.setColumnWidth(4, 200);
  } else {
    removeExistingLogRows(sh, body.last_trading_date);
  }
  var rows = (body.top || []).map(function (r) {
    return [body.run_date, body.last_trading_date, r.Ticker, r.Name, r.Sector, r.LastPrice, r.LotCost,
            r.ADTV20, r.ATR14_pct, r.High20, r.DistToHigh20, r.ATRsBelowHigh, r.BreakoutLast3, r.Source || "Live"];
  });
  if (rows.length) {
    var startRow = sh.getLastRow() + 1;
    sh.getRange(startRow, 1, rows.length, TOP_HEADERS.length).setValues(rows);
    formatTop8Rows(sh, startRow, rows.length);
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
  var sh = getOrCreateSheet(ss, "Screen_Log");
  var isNew = sh.getLastRow() === 0;
  if (isNew) {
    sh.getRange(1, 1, 1, TOP_HEADERS.length).setValues([TOP_HEADERS]);
    styleHeaderRow(sh, TOP_HEADERS.length);
    sh.setColumnWidths(1, TOP_HEADERS.length, 110);
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
                    r.ADTV20, r.ATR14_pct, r.High20, r.DistToHigh20, r.ATRsBelowHigh, r.BreakoutLast3, r.Source || "Live"]);
    });
  });
  if (allRows.length) {
    var startRow = sh.getLastRow() + 1;
    sh.getRange(startRow, 1, allRows.length, TOP_HEADERS.length).setValues(allRows);
    formatTop8Rows(sh, startRow, allRows.length);
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
    ["High20", "The 20-day-high price itself, in IDR -- the raw input DistToHigh20 and ATRsBelowHigh are both computed from. Shown so you can rebuild either by hand.", "MAX(High) over the last 20 trading sessions.", ""],
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
    ["Live_Watch_AllTickers", "Same idea again, but sourced from All_Tickers (the full ~950-980 universe) -- capped to the top " + LIVE_WATCH_ALLTICKERS_MAX_ROWS + " tickers by ADTV20 (liquidity), not the whole universe. That cap is a deliberate choice, not a technical wall: GOOGLEFINANCE has no documented limit on concurrent formulas per sheet, and Live_Watch + Live_Watch_Passers + a full-universe version would run roughly 2,700 of them at once with no track record at that scale. 250 was chosen as a liquidity-weighted middle ground; raise LIVE_WATCH_ALLTICKERS_MAX_ROWS in the script if you want more coverage and are willing to test the sheet at that size.", "", ""],
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
// most-liquid 250 tickers by ADTV20 (writeAllTickers() sorts All_Tickers by ADTV20 descending
// specifically so its first 250 rows ARE that top-250 set -- no separate sort needed here).
// Raise/lower LIVE_WATCH_ALLTICKERS_MAX_ROWS if 250 turns out to be too many or too few.
var LIVE_WATCH_ALLTICKERS_MAX_ROWS = 250;

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
