/*
 * IDX Daily Screener receiver.
 *
 * SETUP:
 * 1. In your Google Sheet: Extensions > Apps Script.
 * 2. Delete whatever is in Code.gs and paste this whole file in.
 * 3. Change SHARED_SECRET below to the SAME string you put in config.json on your PC.
 * 4. Click Deploy > New deployment > select type "Web app".
 *    - Execute as: Me
 *    - Who has access: Anyone
 *    - Click Deploy, authorize when prompted, then copy the "Web app URL" it gives you.
 * 5. Paste that URL into config.json's "webapp_url" field on your PC.
 * 6. Re-run run_daily.ps1 once by hand to test -- check the Sheet updates.
 */

var SHARED_SECRET = "change-me-to-something-only-you-know"; // must match config.json

var TOP_HEADERS = ["RunDate", "LastTradingDate", "Ticker", "Name", "LastPrice", "LotCost",
                    "ADTV20", "ATR14_pct", "DistToHigh20", "BreakoutLast3"];
var NEARMISS_HEADERS = ["RunDate", "Ticker", "Name", "ADTV20", "FailedFilters"];

function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return ContentService.createTextOutput("Bad JSON: " + err).setMimeType(ContentService.MimeType.TEXT);
  }

  if (body.secret !== SHARED_SECRET) {
    return ContentService.createTextOutput("Forbidden: bad secret").setMimeType(ContentService.MimeType.TEXT);
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  writeLatestTop8(ss, body);
  appendToLog(ss, body);
  writeNearMisses(ss, body);
  writeMeta(ss, body);

  return ContentService.createTextOutput("OK").setMimeType(ContentService.MimeType.TEXT);
}

function getOrCreateSheet(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  return sh;
}

function writeLatestTop8(ss, body) {
  var sh = getOrCreateSheet(ss, "Latest_Top8");
  sh.clear();
  sh.getRange(1, 1, 1, TOP_HEADERS.length).setValues([TOP_HEADERS]).setFontWeight("bold");
  var rows = (body.top || []).map(function (r) {
    return [body.run_date, body.last_trading_date, r.Ticker, r.Name, r.LastPrice, r.LotCost,
            r.ADTV20, r.ATR14_pct, r.DistToHigh20, r.BreakoutLast3];
  });
  if (rows.length) sh.getRange(2, 1, rows.length, TOP_HEADERS.length).setValues(rows);
  sh.autoResizeColumns(1, TOP_HEADERS.length);
}

function appendToLog(ss, body) {
  var sh = getOrCreateSheet(ss, "Screen_Log");
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, TOP_HEADERS.length).setValues([TOP_HEADERS]).setFontWeight("bold");
  }
  var rows = (body.top || []).map(function (r) {
    return [body.run_date, body.last_trading_date, r.Ticker, r.Name, r.LastPrice, r.LotCost,
            r.ADTV20, r.ATR14_pct, r.DistToHigh20, r.BreakoutLast3];
  });
  if (rows.length) {
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, TOP_HEADERS.length).setValues(rows);
  }
}

function writeNearMisses(ss, body) {
  var sh = getOrCreateSheet(ss, "Near_Misses");
  sh.clear();
  sh.getRange(1, 1, 1, NEARMISS_HEADERS.length).setValues([NEARMISS_HEADERS]).setFontWeight("bold");
  var rows = (body.near_misses || []).map(function (r) {
    return [body.run_date, r.Ticker, r.Name, r.ADTV20, (r.FailedFilters || []).join(", ")];
  });
  if (rows.length) sh.getRange(2, 1, rows.length, NEARMISS_HEADERS.length).setValues(rows);
}

function writeMeta(ss, body) {
  var sh = getOrCreateSheet(ss, "Run_Info");
  sh.clear();
  sh.getRange(1, 1).setValue("Last run:");
  sh.getRange(1, 2).setValue(body.run_date + " (data as of " + body.last_trading_date + ")");
  sh.getRange(2, 1).setValue("Universe size:");
  sh.getRange(2, 2).setValue(body.universe_count);
  sh.getRange(3, 1).setValue("Passed all 5 filters:");
  sh.getRange(3, 2).setValue(body.pass_count);
  sh.getRange(4, 1).setValue("Caveats:");
  (body.caveats || []).forEach(function (c, i) {
    sh.getRange(5 + i, 1, 1, 2).merge().setValue(c).setWrap(true);
  });
  sh.autoResizeColumn(1);
}
