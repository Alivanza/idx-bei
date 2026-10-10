// Loads the ACTUAL addition file (AppsScript_Addition.gs, minus its header comment block)
// plus the small set of shared helpers it depends on (copied here verbatim from the real
// AppsScript_Code.gs, not reimplemented) into a vm sandbox with hand-built mock
// SpreadsheetApp objects, and exercises appendDailyPricesInit / appendDailyPrices against
// them. Mirrors the test methodology used throughout this project for every .gs change.

const vm = require("vm");
const fs = require("fs");
const assert = require("assert");

// vm.createContext runs code in a separate V8 realm, so arrays/strings that cross the
// boundary can fail assert.deepStrictEqual's reference checks even when their contents are
// identical (a Node vm quirk, not a property of the code under test). Compare via a plain
// JSON round-trip instead, which only cares about content.
function assertSameContent(actual, expected, msg) {
  assert.strictEqual(JSON.stringify(actual), JSON.stringify(expected), msg);
}

// ---- shared helpers, copied verbatim from AppsScript_Code.gs (not reimplemented) ----
const SHARED_HELPERS = `
var HEADER_BG = "#1f3864";
var HEADER_FONT = "#ffffff";
var BORDER_COLOR = "#cccccc";

function getOrCreateSheet(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  return sh;
}

function styleHeaderRow(sh, numCols) {
  sh.getRange(1, 1, 1, numCols)
    .setFontWeight("bold")
    .setBackground(HEADER_BG)
    .setFontColor(HEADER_FONT)
    .setVerticalAlignment("middle");
  sh.setFrozenRows(1);
}
`;

// ---- load the real addition file, stripping its leading /* ... */ instructions comment ----
let additionSrc = fs.readFileSync(__dirname + "/AppsScript_Addition.gs", "utf8");
additionSrc = additionSrc.replace(/^\/\*[\s\S]*?\*\/\s*/, ""); // strip the first block comment only

const fullSrc = SHARED_HELPERS + "\n" + additionSrc;

// ---- mock Sheet / Range / Spreadsheet ----
function makeMockSheet(name) {
  let data = []; // array of arrays, 0-indexed; data[r][c]
  let colWidths = {};
  let frozenRows = 0;

  function ensureSize(row, col, numRows, numCols) {
    const maxRow = row - 1 + numRows;
    while (data.length < maxRow) data.push([]);
    for (let r = row - 1; r < maxRow; r++) {
      const maxCol = col - 1 + numCols;
      while (data[r].length < maxCol) data[r].push(undefined);
    }
  }

  function makeRange(row, col, numRows, numCols) {
    numRows = numRows || 1;
    numCols = numCols || 1;
    ensureSize(row, col, numRows, numCols);
    const rangeObj = {
      getValues: () => {
        const out = [];
        for (let r = 0; r < numRows; r++) {
          const rowArr = [];
          for (let c = 0; c < numCols; c++) {
            rowArr.push(data[row - 1 + r][col - 1 + c]);
          }
          out.push(rowArr);
        }
        return out;
      },
      setValues: (vals) => {
        for (let r = 0; r < numRows; r++) {
          for (let c = 0; c < numCols; c++) {
            data[row - 1 + r][col - 1 + c] = vals[r][c];
          }
        }
        return rangeObj;
      },
      setNumberFormat: () => rangeObj,
      setFontWeight: () => rangeObj,
      setBackground: () => rangeObj,
      setFontColor: () => rangeObj,
      setVerticalAlignment: () => rangeObj,
      setWrap: () => rangeObj,
      merge: () => rangeObj,
      setValue: (v) => { data[row - 1][col - 1] = v; return rangeObj; },
    };
    return rangeObj;
  }

  return {
    getRange: (row, col, numRows, numCols) => makeRange(row, col, numRows, numCols),
    getLastRow: () => data.length,
    getLastColumn: () => (data[0] ? data[0].length : 0),
    clear: () => { data = []; },
    deleteRow: (r) => { data.splice(r - 1, 1); },
    setColumnWidths: (start, count, w) => { for (let i = 0; i < count; i++) colWidths[start + i] = w; },
    setColumnWidth: (c, w) => { colWidths[c] = w; },
    setFrozenRows: (n) => { frozenRows = n; },
    _getData: () => data, // test-only accessor
    _name: name,
  };
}

function makeMockSpreadsheet() {
  const sheets = {};
  return {
    getSheetByName: (name) => sheets[name] || null,
    insertSheet: (name) => { const sh = makeMockSheet(name); sheets[name] = sh; return sh; },
  };
}

const sandbox = {
  Session: { getScriptTimeZone: () => "Asia/Jakarta" },
  Utilities: {
    formatDate: (d, tz, fmt) => {
      const yyyy = d.getUTCFullYear();
      const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
      const dd = String(d.getUTCDate()).padStart(2, "0");
      return `${yyyy}-${mm}-${dd}`;
    },
  },
  console,
};
vm.createContext(sandbox);
vm.runInContext(fullSrc, sandbox);

// ============================= TESTS =============================
let passed = 0, failed = 0;
function test(name, fn) {
  try {
    fn();
    console.log("PASS:", name);
    passed++;
  } catch (e) {
    console.log("FAIL:", name, "--", e.message);
    failed++;
  }
}

// ---- 1. Fresh tab: header gets created correctly ----
test("getOrResetDailyPricesSheet creates header on a brand-new tab", () => {
  const ss = makeMockSpreadsheet();
  const sh = sandbox.getOrResetDailyPricesSheet(ss);
  const header = sh._getData()[0];
  assertSameContent(header, sandbox.DAILY_PRICES_HEADERS);
});

// ---- 2. Reset happens on the OLD self-join-formula tab (simulated as mismatched header) ----
test("getOrResetDailyPricesSheet clears a tab with the old/different header", () => {
  const ss = makeMockSpreadsheet();
  const sh = ss.insertSheet(sandbox.DAILY_PRICES_TAB_NAME);
  sh.getRange(1, 1, 1, 1).setValues([["SomeOldHeaderThatIsNotOurs"]]);
  sh.getRange(2, 1, 1, 1).setValues([["stale data"]]);
  const sh2 = sandbox.getOrResetDailyPricesSheet(ss);
  assert.strictEqual(sh2._getData().length, 1); // only the new header row left
  assertSameContent(sh2._getData()[0], sandbox.DAILY_PRICES_HEADERS);
});

// ---- 3. Reset does NOT happen once the tab already has our schema (idempotent across calls) ----
test("getOrResetDailyPricesSheet does not clear a tab already on our schema", () => {
  const ss = makeMockSpreadsheet();
  const sh = ss.insertSheet(sandbox.DAILY_PRICES_TAB_NAME);
  sh.getRange(1, 1, 1, sandbox.DAILY_PRICES_HEADERS.length).setValues([sandbox.DAILY_PRICES_HEADERS]);
  sh.getRange(2, 1, 1, sandbox.DAILY_PRICES_HEADERS.length).setValues([["BBCA", "Bank Central Asia", "2026-01-01", 9000, 9100, 8950, 9050, 12345600]]);
  const sh2 = sandbox.getOrResetDailyPricesSheet(ss);
  assert.strictEqual(sh2._getData().length, 2); // header + the one row, untouched
});

// ---- 4. appendDailyPricesInit appends rows and formats them ----
test("appendDailyPricesInit appends rows to a fresh tab", () => {
  const ss = makeMockSpreadsheet();
  const rows = [
    ["BBCA", "Bank Central Asia Tbk.", "2025-03-11", 8000, 8100, 7950, 8050, 50000000],
    ["TLKM", "Telkom Indonesia Tbk.", "2025-03-11", 3000, 3050, 2980, 3020, 30000000],
  ];
  sandbox.appendDailyPricesInit(ss, rows);
  const sh = ss.getSheetByName(sandbox.DAILY_PRICES_TAB_NAME);
  const data = sh._getData();
  assert.strictEqual(data.length, 3); // header + 2 rows
  assert.strictEqual(data[1][0], "BBCA");
  assert.strictEqual(data[2][0], "TLKM");
});

// ---- 5. appendDailyPricesInit across multiple chunks accumulates (doesn't wipe prior chunk) ----
test("appendDailyPricesInit across two chunks accumulates rows", () => {
  const ss = makeMockSpreadsheet();
  sandbox.appendDailyPricesInit(ss, [["BBCA", "Bank Central Asia Tbk.", "2025-03-11", 8000, 8100, 7950, 8050, 50000000]]);
  sandbox.appendDailyPricesInit(ss, [["BBCA", "Bank Central Asia Tbk.", "2025-03-12", 8050, 8150, 8000, 8100, 48000000]]);
  const sh = ss.getSheetByName(sandbox.DAILY_PRICES_TAB_NAME);
  const data = sh._getData();
  assert.strictEqual(data.length, 3); // header + 2 accumulated rows across both chunks
  assert.strictEqual(data[1][2], "2025-03-11");
  assert.strictEqual(data[2][2], "2025-03-12");
});

// ---- 6. appendDailyPrices dedupes same-date re-post (retry safety) ----
test("appendDailyPrices replaces same-date rows on a retried post, does not duplicate", () => {
  const ss = makeMockSpreadsheet();
  sandbox.appendDailyPricesInit(ss, [
    ["BBCA", "Bank Central Asia Tbk.", "2026-09-12", 9000, 9100, 8950, 9050, 12000000],
    ["TLKM", "Telkom Indonesia Tbk.", "2026-09-12", 3000, 3050, 2980, 3020, 8000000],
  ]);
  // simulate a retry of the SAME day's daily post, with slightly different (corrected) values
  sandbox.appendDailyPrices(ss, [
    ["BBCA", "Bank Central Asia Tbk.", "2026-09-12", 9000, 9120, 8950, 9080, 12500000],
    ["TLKM", "Telkom Indonesia Tbk.", "2026-09-12", 3000, 3060, 2980, 3040, 8100000],
  ], "2026-09-12");
  const sh = ss.getSheetByName(sandbox.DAILY_PRICES_TAB_NAME);
  const data = sh._getData();
  assert.strictEqual(data.length, 3); // header + 2 rows -- NOT 5 (no duplication)
  assert.strictEqual(data[1][6], 9080); // Close column -- the retried (corrected) value won
  assert.strictEqual(data[2][6], 3040);
});

// ---- 7. appendDailyPrices for a NEW date leaves prior dates' rows untouched ----
test("appendDailyPrices appending a new date does not disturb existing dates", () => {
  const ss = makeMockSpreadsheet();
  sandbox.appendDailyPricesInit(ss, [
    ["BBCA", "Bank Central Asia Tbk.", "2026-09-11", 9000, 9050, 8900, 8950, 11000000],
  ]);
  sandbox.appendDailyPrices(ss, [
    ["BBCA", "Bank Central Asia Tbk.", "2026-09-12", 8950, 9000, 8900, 8975, 10500000],
  ], "2026-09-12");
  const sh = ss.getSheetByName(sandbox.DAILY_PRICES_TAB_NAME);
  const data = sh._getData();
  assert.strictEqual(data.length, 3); // header + both days, both present
  assert.strictEqual(data[1][2], "2026-09-11");
  assert.strictEqual(data[1][6], 8950); // untouched
  assert.strictEqual(data[2][2], "2026-09-12");
});

// ---- 8. appendDailyPrices on a genuinely empty tab (first-ever daily call, no backfill run first) ----
test("appendDailyPrices works even if the tab has never been initialized", () => {
  const ss = makeMockSpreadsheet();
  sandbox.appendDailyPrices(ss, [
    ["BBCA", "Bank Central Asia Tbk.", "2026-09-15", 9100, 9150, 9050, 9120, 9800000],
  ], "2026-09-15");
  const sh = ss.getSheetByName(sandbox.DAILY_PRICES_TAB_NAME);
  const data = sh._getData();
  assert.strictEqual(data.length, 2);
  assertSameContent(data[0], sandbox.DAILY_PRICES_HEADERS);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
