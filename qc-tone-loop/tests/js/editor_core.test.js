// Plain node test for qcsuite/web/static/editor_core.js.
// Run: node tests/js/editor_core.test.js   (no npm packages; exits non-zero on failure)
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const C = require(path.join(__dirname, "..", "..", "qcsuite", "web", "static", "editor_core.js"));

let failed = 0, passed = 0;
const queue = [];
function test(name, fn) { queue.push([name, fn]); }
function near(a, b, eps = 1e-6, msg) {
  assert.ok(Math.abs(a - b) <= eps, `${msg || ""} expected ${b}, got ${a}`);
}

// Fixtures modelled on the spec's ModelRepo excerpt.
const OUTPUT = { name: "OUTPUT", kind: "knob", min: -60, max: 12, skew: 3.8018, steps: null,
  units: "dB", int: false, min_string: "OFF", max_string: null, default: 0, value: 0 };
const GAIN = { name: "GAIN", kind: "knob", min: 0, max: 10, skew: 1, steps: null, units: "",
  int: false, min_string: null, max_string: null, default: 5, value: 6.2 };
const ATTACK = { name: "ATTACK", kind: "knob", min: 0.016, max: 0.8, skew: 0.6, steps: 300,
  units: "ms", int: false, min_string: null, max_string: null, default: 0.8 };
const MIX = { name: "MIX", kind: "knob", min: 0, max: 100, skew: 1, steps: 1001, units: "%" };
const FREQ = { name: "FREQ", kind: "knob", min: 20, max: 20000, skew: 0.3, steps: null, units: "Hz" };
const INPUT = { name: "INPUT", kind: "knob", min: -48, max: 0, skew: 1, steps: 481, units: "dB" };
const RATIO_INT = { name: "VOICES", kind: "knob", min: 1, max: 8, skew: 1, steps: null, int: true, units: "" };
const PH = { name: "X", kind: "knob", placeholder: true, min: null, max: null, units: "dB", value: 0.62 };

test("exports", () => {
  for (const k of ["proportion", "angle", "valueFromProportion", "formatValue", "stepValue",
    "selectStyle", "arcPath", "Throttle", "quantize", "dragValue", "range", "stepInterval",
    "portLabel", "groupKeyForCategory"]) {
    assert.ok(k in C, `missing export ${k}`);
  }
});

test("proportion: linear, clamped, skewed", () => {
  near(C.proportion(GAIN, 5), 0.5);
  near(C.proportion(GAIN, -3), 0);
  near(C.proportion(GAIN, 99), 1);
  // Brit 2203 OUTPUT at 0.0 dB: ((0+60)/72)^3.8018
  near(C.proportion(OUTPUT, 0), Math.pow(60 / 72, 3.8018));
  near(C.proportion(OUTPUT, -60), 0);
  near(C.proportion(OUTPUT, 12), 1);
  near(C.proportion(PH, 0.25), 0.25);
  near(C.proportion({ min: 1, max: 1 }, 1), 0, 0, "zero span");
});

test("angle: 270 degree sweep from -135", () => {
  near(C.angle(GAIN, 0), -135);
  near(C.angle(GAIN, 10), 135);
  near(C.angle(GAIN, 5), 0);
  near(C.angle(OUTPUT, 0), -135 + 270 * Math.pow(60 / 72, 3.8018));
});

test("valueFromProportion: inverse skew", () => {
  near(C.valueFromProportion(GAIN, 0.5), 5);
  near(C.valueFromProportion(GAIN, -1), 0);
  near(C.valueFromProportion(GAIN, 2), 10);
  const prop = C.proportion(OUTPUT, 0);
  near(C.valueFromProportion(OUTPUT, prop), 0, 1e-9);
  near(C.valueFromProportion(OUTPUT, 1), 12);
  near(C.valueFromProportion(OUTPUT, 0), -60);
  for (const v of [-50, -12.5, -3, 0, 6, 11]) near(C.valueFromProportion(OUTPUT, C.proportion(OUTPUT, v)), v, 1e-9);
});

test("valueFromProportion: steps quantization and int rounding", () => {
  const interval = (0.8 - 0.016) / 299;
  for (const prop of [0.1, 0.33, 0.5, 0.77, 0.99]) {
    const v = C.valueFromProportion(ATTACK, prop);
    const k = (v - 0.016) / interval;
    near(k, Math.round(k), 1e-6, `ATTACK at ${prop} on the 300-step grid`);
    assert.ok(v >= 0.016 && v <= 0.8);
  }
  near(C.valueFromProportion(ATTACK, 1), 0.8);
  near(C.valueFromProportion(ATTACK, 0), 0.016);
  // INPUT: 481 steps over 48 dB = 0.1 dB
  near(C.valueFromProportion(INPUT, 0.50123), -23.9);
  near(C.valueFromProportion(INPUT, 0.5), -24);
  assert.equal(C.valueFromProportion(INPUT, 0.123456), -42.1);
  assert.equal(C.valueFromProportion(RATIO_INT, 0.5), 5);   // 1 + 7*0.5 = 4.5 -> 5
  assert.equal(C.quantize(RATIO_INT, 3.4), 3);
  assert.equal(C.quantize(GAIN, 12), 10);
});

test("formatValue: Brit 2203 OUTPUT", () => {
  assert.deepEqual(C.formatValue(OUTPUT, 0), { text: "0.0", unit: "dB" });
  assert.deepEqual(C.formatValue(OUTPUT, -60), { text: "OFF", unit: "" });
  assert.deepEqual(C.formatValue(OUTPUT, -61), { text: "OFF", unit: "" });
  assert.deepEqual(C.formatValue(OUTPUT, -12.34), { text: "-12.3", unit: "dB" });
  assert.deepEqual(C.formatValue(OUTPUT, -0.04), { text: "-0.04", unit: "dB" });
  assert.deepEqual(C.formatValue(OUTPUT, 12), { text: "12.0", unit: "dB" });
});

test("formatValue: Legendary 87 ATTACK (ms below 10 keeps decimals)", () => {
  assert.deepEqual(C.formatValue(ATTACK, 0.8), { text: "0.80", unit: "ms" });
  assert.deepEqual(C.formatValue(ATTACK, 0.016), { text: "0.02", unit: "ms" });
  assert.deepEqual(C.formatValue({ ...ATTACK, max: 50 }, 12.6), { text: "13", unit: "ms" });
  assert.deepEqual(C.formatValue({ ...ATTACK, max: 50 }, 9.96), { text: "10.0", unit: "ms" });
});

test("formatValue: % and BPM integer, Hz >= 10 integer, int flag, max_string", () => {
  assert.deepEqual(C.formatValue(MIX, 17.4), { text: "17", unit: "%" });
  assert.deepEqual(C.formatValue(MIX, 0.4), { text: "0", unit: "%" });
  assert.deepEqual(C.formatValue({ units: "BPM", min: 40, max: 300 }, 120.6), { text: "121", unit: "BPM" });
  assert.deepEqual(C.formatValue(FREQ, 80.4), { text: "80", unit: "Hz" });
  assert.deepEqual(C.formatValue(FREQ, 10), { text: "10", unit: "Hz" });
  assert.deepEqual(C.formatValue({ ...FREQ, min: 0 }, 9.5), { text: "9.5", unit: "Hz" });
  assert.deepEqual(C.formatValue({ ...FREQ, min: 0 }, 0.5), { text: "0.50", unit: "Hz" });
  assert.deepEqual(C.formatValue(RATIO_INT, 4), { text: "4", unit: "" });
  assert.deepEqual(C.formatValue(GAIN, 6.2), { text: "6.2", unit: "" });
  assert.deepEqual(C.formatValue(GAIN, 0), { text: "0.0", unit: "" });
  assert.deepEqual(C.formatValue({ ...GAIN, max_string: "MAX" }, 10), { text: "MAX", unit: "" });
  assert.deepEqual(C.formatValue(GAIN, null), { text: "—", unit: "" });
});

test("formatValue: placeholder = 2 decimals, no unit", () => {
  assert.deepEqual(C.formatValue(PH, 0.62), { text: "0.62", unit: "" });
  assert.deepEqual(C.formatValue(PH, 0), { text: "0.00", unit: "" });
  assert.deepEqual(C.formatValue(PH, 1), { text: "1.00", unit: "" });
});

test("stepValue: 1/100 and 1/1000 of the proportion range", () => {
  near(C.stepValue(GAIN, 5, +1, false), 5.1);
  near(C.stepValue(GAIN, 5, -1, false), 4.9);
  near(C.stepValue(GAIN, 5, +1, true), 5.01);
  assert.equal(C.stepValue(GAIN, 10, +1, false), 10);
  assert.equal(C.stepValue(GAIN, 0, -1, false), 0);
  // skewed: one percent of the proportion, not of the value
  const up = C.stepValue(OUTPUT, 0, +1, false);
  near(C.proportion(OUTPUT, up), C.proportion(OUTPUT, 0) + 0.01, 1e-9);
  near(C.stepValue(PH, 0.5, +1, false), 0.51);
});

test("stepValue: one step when the steps are coarser", () => {
  const coarse = { min: 0, max: 10, skew: 1, steps: 11, units: "" };   // 1.0 per step
  assert.equal(C.stepValue(coarse, 5, +1, false), 6);
  assert.equal(C.stepValue(coarse, 5, -1, true), 4);
  assert.equal(C.stepValue(coarse, 10, +1, false), 10);
  assert.equal(C.stepValue(RATIO_INT, 3, +1, true), 4);
  // MIX 1001 steps = 0.1 %, finer than 1 %: moves 1 %
  near(C.stepValue(MIX, 50, +1, false), 51);
  // fine on MIX (0.1 %) equals the step: one step
  near(C.stepValue(MIX, 50, +1, true), 50.1);
  // ATTACK: every press moves at least one grid step and stays on the grid
  const interval = (0.8 - 0.016) / 299;
  let v = 0.016;
  for (let i = 0; i < 20; i++) {
    const n = C.stepValue(ATTACK, v, +1, true);
    assert.ok(n - v >= interval - 1e-9, "moves at least one step");
    const k = (n - 0.016) / interval;
    near(k, Math.round(k), 1e-6);
    v = n;
  }
});

test("dragValue: 200 px full range, fine 2000 px", () => {
  near(C.dragValue(GAIN, 0.5, -100, false), 10);
  near(C.dragValue(GAIN, 0.5, 100, false), 0);
  near(C.dragValue(GAIN, 0.5, -20, false), 6);
  near(C.dragValue(GAIN, 0.5, -20, true), 5.1);
});

test("selectStyle", () => {
  assert.equal(C.selectStyle(["LP", "HP"]), "buttons");
  assert.equal(C.selectStyle(["ON"]), "buttons");
  assert.equal(C.selectStyle(["4", "8", "12", "20", "All"]), "dropdown");
  assert.equal(C.selectStyle(["A", "B", "C"]), "dropdown");
});

test("arcPath", () => {
  assert.equal(C.arcPath(44, -135, 135), "M18.89 81.11A44 44 0 1 1 81.11 81.11");
  assert.equal(C.arcPath(44, -135, 0), "M18.89 81.11A44 44 0 0 1 50.00 6.00");
  assert.equal(C.arcPath(10, 0, 90, 20, 20), "M20.00 10.00A10 10 0 0 1 30.00 20.00");
});

const sleep = ms => new Promise(r => setTimeout(r, ms));

test("Throttle: latest value wins, one in flight, seq increases, final value sent", async () => {
  const sent = [];
  let inFlight = 0, maxInFlight = 0;
  const t = new C.Throttle(async (v, seq) => {
    inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
    await sleep(15);
    sent.push([v, seq]);
    inFlight--;
  }, 20);
  for (let i = 1; i <= 50; i++) { t.push(i); if (i % 10 === 0) await sleep(5); }
  await t.flush();
  assert.equal(maxInFlight, 1, "at most one send in flight");
  assert.ok(sent.length < 50, `throttled (${sent.length} sends)`);
  assert.equal(sent[0][0], 1, "first value goes out at once");
  assert.equal(sent[sent.length - 1][0], 50, "final value always sent");
  sent.forEach(([, seq], i) => assert.equal(seq, i + 1, "seq increases by one"));
  for (let i = 1; i < sent.length; i++) assert.ok(sent[i][0] > sent[i - 1][0], "never sends an older value");
});

test("Throttle: flush right away, idle flush, errors do not stop it", async () => {
  const sent = [];
  const errs = [];
  const t = new C.Throttle(async v => { if (v === "bad") throw new Error("x"); sent.push(v); }, 1000,
    { onError: e => errs.push(e) });
  await t.flush();   // idle
  t.push("bad");
  t.push("a");       // waits for the 1000 ms interval ...
  t.push("b");
  const t0 = Date.now();
  await t.flush();   // ... unless flushed
  assert.ok(Date.now() - t0 < 500, "flush skips the interval");
  assert.deepEqual(sent, ["b"]);
  assert.equal(errs.length, 1);
});

test("Throttle: cancel drops the pending value and waits for the one in flight", async () => {
  const sent = [];
  const t = new C.Throttle(async v => { await sleep(10); sent.push(v); }, 50);
  t.push(1); t.push(2); t.push(3);
  await t.cancel();
  assert.deepEqual(sent, [1]);
  assert.ok(t.idle);
  await sleep(80);
  assert.deepEqual(sent, [1], "nothing sent after cancel");
});


test("portLabel: grid port names", () => {
  const cases = { INPUT_1: "In 1", INPUT_1_2: "In 1/2", RETURN_1: "Ret 1", RETURN_1_2: "Ret 1/2",
    PREV_ROW: "Prev. row", USB_5: "USB 5", USB_7_8: "USB 7/8", NEXT_ROW_3: "Row 3",
    NEXT_ROW_3_4: "Row 3/4", MULTIPLE: "Multi Out", XLR_1_2: "Out 1/2", XLR_1: "Out 1",
    OUT_3_4: "Out 3/4", SEND_1: "Send 1", SEND_1_2: "Send 1/2", SIDECHAIN_BUFFER: "S/C" };
  for (const [k, v] of Object.entries(cases)) assert.equal(C.portLabel(k), v, k);
  assert.equal(C.portLabel("EMPTY"), null);
  assert.equal(C.portLabel(null), null);
  assert.equal(C.portLabel("WEIRD_PORT"), "WEIRD PORT");
});

test("groupKeyForCategory: block categories -> sidebar groups", () => {
  const cases = { "Guitar Amplifier": "amp", "Bass Amplifier": "amp", "Cabsim Guitar (M)": "cab",
    "Cabsim Bass (ST)": "cab", "Guitar Overdrive": "overdrive", "Bass Overdrive": "overdrive",
    "Neural Capture": "capture", "Neural Capture V2": "capture", "Delay": "delay", "Reverb": "reverb",
    "Compressor": "compressor", "Pitch": "pitch", "Modulation": "modulation", "Filter": "filter",
    "Wah": "wah", "Equalizer": "eq", "Morph": "morph", "Synth": "synth", "IRLoaders": "ir",
    "Utility": "utility", "FX Loop": "fxloop", "Loopers": "looper", "": "utility" };
  for (const [k, v] of Object.entries(cases)) assert.equal(C.groupKeyForCategory(k), v, k);
});

(async () => {
  for (const [name, fn] of queue) {
    try { await fn(); passed++; console.log(`ok   ${name}`); }
    catch (e) { failed++; console.log(`FAIL ${name}\n     ${e && e.message}`); }
  }
  console.log(`editor_core: ${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
