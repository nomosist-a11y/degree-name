// Plain node test for the pure helpers in qcsuite/web/static/editor_extras.js.
// Run: node tests/js/editor_extras.test.js   (no npm packages; exits non-zero on failure)
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const X = require(path.join(__dirname, "..", "..", "qcsuite", "web", "static", "editor_extras.js"));

let failed = 0, passed = 0;
const queue = [];
function test(name, fn) { queue.push([name, fn]); }
function near(a, b, eps = 1e-6, msg) {
  assert.ok(Math.abs(a - b) <= eps, `${msg || ""} expected ${b}, got ${a}`);
}

test("laneDb / dbToWire: -40..+12 dB, 0 dB at 10/13, wire 0 is Off", () => {
  near(X.laneDb(1), 12);
  near(X.laneDb(10 / 13), 0);
  near(X.dbToWire(0), 10 / 13);
  near(X.dbToWire(-39.5), 0.5 / 52);
  assert.equal(X.laneDb(0), null);
  assert.equal(X.laneDb(null), null);
  near(X.dbToWire(99), 1, 1e-12, "clamped");
});

test("laneText: Off at the bottom, signed dB with one decimal", () => {
  assert.deepEqual(X.laneText(0), { text: "Off", unit: "" });
  assert.deepEqual(X.laneText(10 / 13), { text: "0.0", unit: "dB" });
  assert.deepEqual(X.laneText(1), { text: "+12.0", unit: "dB" });
  assert.deepEqual(X.laneText(X.dbToWire(-3.1)), { text: "-3.1", unit: "dB" });
  assert.deepEqual(X.laneText(undefined), { text: "—", unit: "" });
});

test("laneSnap: 0.1 dB steps, below -39.5 dB snaps to Off", () => {
  near(X.laneDb(X.laneSnap(X.dbToWire(-6.04))), -6.0, 1e-9);
  assert.equal(X.laneSnap(0.005), 0);
  assert.equal(X.laneSnap(-1), 0);
  near(X.laneDb(X.laneSnap(0.0097)), -39.5, 1e-9);
  near(X.laneSnap(2), 1);
});

test("laneStep: 0.5 dB (0.1 fine), Off <-> floor", () => {
  near(X.laneDb(X.laneStep(10 / 13, +1, false)), 0.5, 1e-9);
  near(X.laneDb(X.laneStep(10 / 13, -1, true)), -0.1, 1e-9);
  near(X.laneDb(X.laneStep(0, +1, false)), -39.5, 1e-9);
  assert.equal(X.laneStep(X.dbToWire(-39.5), -1, false), 0);
  assert.equal(X.laneStep(0, -1, false), 0);
  near(X.laneStep(1, +1, false), 1);
});

test("panText / panSnap: C at 0.5, L/R 100 at the ends", () => {
  assert.equal(X.panText(0.5).text, "C");
  assert.equal(X.panText(0).text, "L100");
  assert.equal(X.panText(1).text, "R100");
  assert.equal(X.panText(0.75).text, "R50");
  assert.equal(X.panText(null).text, "—");
  near(X.panSnap(0.5012), 0.5);
  near(X.panSnap(0.7526), 0.755);
});

test("tapPush: keeps the last 8 taps, starts over after 2 s", () => {
  let t = [];
  for (let i = 0; i < 10; i++) t = X.tapPush(t, i * 500);
  assert.equal(t.length, 8);
  assert.equal(t[0], 1000);
  assert.deepEqual(X.tapPush(t, 4500 + 2001), [6501]);
  assert.deepEqual(X.tapPush([1000], 500), [500], "clock went back");
});

test("tapBpm: average interval of the taps, clamped 30..250", () => {
  assert.equal(X.tapBpm([]), null);
  assert.equal(X.tapBpm([0]), null);
  assert.equal(X.tapBpm([0, 500, 1000, 1500]), 120);
  assert.equal(X.tapBpm([0, 480, 1020, 1500]), 120);
  assert.equal(X.tapBpm([0, 100]), 250);
  assert.equal(X.tapBpm([0, 1900, 3800]), 31.6);
  assert.equal(X.tapBpm([0, 0]), null);
});

test("parseBpm: numbers only, clamped, one decimal", () => {
  assert.equal(X.parseBpm("120"), 120);
  assert.equal(X.parseBpm(" 98,46 "), 98.5);
  assert.equal(X.parseBpm("10"), 30);
  assert.equal(X.parseBpm("999"), 250);
  assert.equal(X.parseBpm(""), null);
  assert.equal(X.parseBpm("abc"), null);
});

test("wireToPct / pctToWire", () => {
  assert.equal(X.wireToPct(0.254), 25);
  assert.equal(X.wireToPct(null), null);
  assert.equal(X.pctToWire("50"), 0.5);
  assert.equal(X.pctToWire(150), 1);
  assert.equal(X.pctToWire("x"), null);
});

test("placePop: stays inside the viewport (phone width)", () => {
  const vw = 375, vh = 700;
  // anchor at the right edge, bottom of the screen
  let pos = X.placePop({ left: 330, right: 370, top: 660, bottom: 690 }, 240, 200, vw, vh);
  assert.ok(pos.left >= 8 && pos.left + 240 <= vw - 8, `left ${pos.left}`);
  assert.ok(pos.top >= 8 && pos.top + 200 <= vh - 8, `top ${pos.top}`);
  assert.equal(pos.top, 660 - 200 - 6, "opens above");
  // anchor at the left: aligned to its left edge, under it
  pos = X.placePop({ left: 20, right: 60, top: 100, bottom: 130 }, 240, 200, vw, vh);
  assert.deepEqual(pos, { left: 20, top: 136 });
  // taller / wider than the screen (CSS caps the size): pinned to the 8px margin
  pos = X.placePop({ left: 20, right: 60, top: 300, bottom: 330 }, 240, 900, vw, vh);
  assert.equal(pos.top, 8);
  pos = X.placePop({ left: 20, right: 60, top: 100, bottom: 130 }, 500, 100, vw, vh);
  assert.equal(pos.left, 8);
});

test("dragProp: 200 px for the whole range, 2000 px with Shift", () => {
  assert.equal(X.dragProp(0.5, -100, false), 1);
  assert.equal(X.dragProp(0.5, 50, false), 0.25);
  near(X.dragProp(0.5, 200, true), 0.4);
  assert.equal(X.dragProp(0, 10, false), 0);
});

test("scene colours and footswitches", () => {
  assert.equal(X.SCENE_COLORS.length, 8);
  for (const [hex] of X.SCENE_COLORS) assert.match(hex, /^#[0-9a-f]{6}$/);
  assert.deepEqual(X.FOOTSWITCHES, ["A", "B", "C", "D", "E", "F", "G", "H"]);
  assert.equal(X.stompSummary(null), "割り当てなし");
  assert.equal(X.stompSummary({ label: "Drive", momentary: true, blocks: [{ row: 0, col: 1 }] }),
    "Drive・1 ブロック・モーメンタリー");
  assert.equal(X.stompSummary({ label: "", momentary: false, blocks: [{}, {}] }), "2 ブロック");
});

(async () => {
  for (const [name, fn] of queue) {
    try { await fn(); passed++; console.log(`ok   ${name}`); }
    catch (e) { failed++; console.log(`FAIL ${name}\n     ${e && e.message}`); }
  }
  console.log(`editor_extras: ${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
