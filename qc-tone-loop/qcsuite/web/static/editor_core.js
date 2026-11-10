// エディターの純粋な計算(DOM なし)。ブラウザでは window.EditorCore、node では module.exports。
// 表示規則は API 契約(editor.format_value / knob_angle)と同じ:
//   proportion = clamp((v-min)/(max-min), 0, 1) ** skew   (JUCE と同じ)
//   angle      = -135 + 270 * proportion                    (度、0 = 12 時、時計回り)
(function () {
  "use strict";

  function num(x, fallback) {
    return typeof x === "number" && isFinite(x) ? x : fallback;
  }
  function clamp(x, lo, hi) {
    return Math.min(hi, Math.max(lo, x));
  }
  // Drops float noise such as 0.30000000000000004 without changing real values.
  function tidy(x) {
    return parseFloat(x.toPrecision(12));
  }

  // Range of a knob. Placeholder knobs (range unknown in real units) work on 0..1, linear.
  function range(p) {
    if (p.placeholder) return { min: 0, max: 1, skew: 1, steps: null, int: false };
    const min = num(p.min, 0);
    const max = num(p.max, 1);
    const skew = num(p.skew, 1) > 0 ? num(p.skew, 1) : 1;
    const steps = num(p.steps, 0) > 1 ? Math.round(p.steps) : null;
    return { min, max, skew, steps, int: !!p.int };
  }

  function proportion(p, v) {
    const r = range(p);
    const span = r.max - r.min;
    if (!(span > 0)) return 0;
    const x = clamp((num(v, r.min) - r.min) / span, 0, 1);
    return Math.pow(x, r.skew);
  }

  function angle(p, v) {
    return -135 + 270 * proportion(p, v);
  }

  // Snaps a real value onto the knob's grid: steps, integer rounding, then clamp.
  function quantize(p, v) {
    const r = range(p);
    const span = r.max - r.min;
    let out = clamp(v, Math.min(r.min, r.max), Math.max(r.min, r.max));
    if (r.steps && span > 0) {
      const interval = span / (r.steps - 1);
      out = r.min + Math.round((out - r.min) / interval) * interval;
    }
    if (r.int) out = Math.round(out);
    out = clamp(out, Math.min(r.min, r.max), Math.max(r.min, r.max));
    return tidy(out);
  }

  // Inverse of proportion(): v = min + span * prop ** (1/skew), then quantize.
  function valueFromProportion(p, prop) {
    const r = range(p);
    const x = clamp(num(prop, 0), 0, 1);
    const v = r.min + (r.max - r.min) * Math.pow(x, 1 / r.skew);
    return quantize(p, v);
  }

  function fixed(v, digits) {
    const s = v.toFixed(digits);
    return parseFloat(s) === 0 ? s.replace(/^-/, "") : s;
  }

  // -> {text, unit}. unit is shown grey after the value ("" when none).
  function formatValue(p, v) {
    if (typeof v !== "number" || !isFinite(v)) return { text: "—", unit: "" };
    if (p.placeholder) return { text: fixed(v, 2), unit: "" };
    const r = range(p);
    if (p.min_string && v <= r.min) return { text: String(p.min_string), unit: "" };
    if (p.max_string && v >= r.max) return { text: String(p.max_string), unit: "" };
    const unit = p.units ? String(p.units) : "";
    const u = unit.toLowerCase();
    let text;
    if (p.int || u === "%" || u === "bpm" || ((u === "hz" || u === "ms") && Math.abs(v) >= 10)) {
      text = fixed(Math.round(v), 0);
    } else if (v !== 0 && Math.abs(v) < 1) {
      text = fixed(v, 2);
    } else {
      text = fixed(v, 1);
    }
    return { text, unit };
  }

  // Smallest real-value move the knob can make (steps interval, 1 for int), or 0.
  function stepInterval(p) {
    const r = range(p);
    const span = r.max - r.min;
    let interval = 0;
    if (r.steps && span > 0) interval = span / (r.steps - 1);
    if (r.int) interval = Math.max(interval, 1);
    return interval;
  }

  // Arrow keys / wheel: 1/100 of the proportion range (1/1000 when fine), or one step
  // when the knob has steps coarser than that. direction: +1 / -1.
  function stepValue(p, v, direction, fine) {
    const r = range(p);
    const dir = direction < 0 ? -1 : 1;
    const cur = quantize(p, num(v, r.min));
    let next = valueFromProportion(p, proportion(p, cur) + dir * (fine ? 0.001 : 0.01));
    const interval = stepInterval(p);
    if (interval > 0 && (next - cur) * dir < interval - 1e-12) {
      next = quantize(p, cur + dir * interval);
    }
    if ((next - cur) * dir < 0) next = cur;
    return next;
  }

  // Vertical drag: about 200 px for the whole range (2000 px when fine). dy > 0 = down.
  function dragValue(p, startProp, dy, fine) {
    return valueFromProportion(p, clamp(startProp - dy / (fine ? 2000 : 200), 0, 1));
  }

  function selectStyle(labels) {
    return labels && labels.length > 2 ? "dropdown" : "buttons";
  }

  // SVG arc on a 100x100 box centred at (cx, cy); angles in degrees, 0 = 12 o'clock, clockwise.
  function arcPoint(r, a, cx, cy) {
    const t = (a - 90) * Math.PI / 180;
    return [cx + r * Math.cos(t), cy + r * Math.sin(t)];
  }
  function arcPath(r, a0, a1, cx, cy) {
    cx = num(cx, 50); cy = num(cy, 50);
    const p0 = arcPoint(r, a0, cx, cy), p1 = arcPoint(r, a1, cx, cy);
    const large = (a1 - a0) > 180 ? 1 : 0;
    return "M" + p0[0].toFixed(2) + " " + p0[1].toFixed(2) + "A" + r + " " + r + " 0 " + large +
      " 1 " + p1[0].toFixed(2) + " " + p1[1].toFixed(2);
  }

  // Latest-value sender: push() keeps only the newest value, at most one send in
  // flight, sends at least intervalMs apart. sendFn(value, seq) may return a promise;
  // seq increases by one per send. Errors go to opts.onError and do not stop it.
  class Throttle {
    constructor(sendFn, intervalMs = 100, opts = {}) {
      this.sendFn = sendFn;
      this.intervalMs = intervalMs;
      this.onError = opts.onError || null;
      this.seq = 0;
      this.pending = false;
      this.value = undefined;
      this.inFlight = null;
      this.timer = null;
      this.lastSent = -Infinity;
      this.waiters = [];
      this.flushing = false;
      this.sent = [];   // values actually handed to sendFn (handy for tests / debugging)
    }
    push(value) {
      this.value = value;
      this.pending = true;
      this._pump(false);
    }
    get idle() {
      return !this.pending && !this.inFlight;
    }
    // Sends the pending value now (skipping the interval) and resolves when idle.
    flush() {
      if (this.idle) return Promise.resolve();
      const done = new Promise(resolve => this.waiters.push(resolve));
      this.flushing = true;
      this._pump(true);
      return done;
    }
    // Drops the pending value (something else will send the final one) and resolves
    // when the send in flight, if any, has finished.
    cancel() {
      this.pending = false;
      this.value = undefined;
      if (this.timer) { clearTimeout(this.timer); this.timer = null; }
      if (this.idle) { this._wake(); return Promise.resolve(); }
      return new Promise(resolve => this.waiters.push(resolve));
    }
    _wake() {
      this.flushing = false;
      const w = this.waiters; this.waiters = [];
      w.forEach(fn => fn());
    }
    _pump(now) {
      if (this.inFlight || !this.pending) {
        if (this.idle) this._wake();
        return;
      }
      const wait = (now || this.flushing) ? 0 : this.lastSent + this.intervalMs - Date.now();
      if (wait > 0) {
        if (!this.timer) this.timer = setTimeout(() => { this.timer = null; this._pump(false); }, wait);
        return;
      }
      if (this.timer) { clearTimeout(this.timer); this.timer = null; }
      const value = this.value;
      this.pending = false;
      this.value = undefined;
      const seq = ++this.seq;
      this.sent.push(value);
      this.lastSent = Date.now();
      this.inFlight = Promise.resolve()
        .then(() => this.sendFn(value, seq))
        .catch(err => { if (this.onError) this.onError(err); })
        .then(() => {
          this.inFlight = null;
          this._pump(false);
        });
    }
  }


  // Grid port names (API) -> the short labels the panels show, or null for EMPTY.
  //   INPUT_1 -> "In 1", INPUT_1_2 -> "In 1/2", RETURN_1 -> "Ret 1", PREV_ROW -> "Prev. row",
  //   USB_5 -> "USB 5", NEXT_ROW_3_4 -> "Row 3/4", MULTIPLE -> "Multi Out", XLR_1_2 -> "Out 1/2",
  //   OUT_3_4 -> "Out 3/4", SEND_1 -> "Send 1", SIDECHAIN_BUFFER -> "S/C".
  const PORT_WORDS = { INPUT: "In", RETURN: "Ret", USB: "USB", NEXT_ROW: "Row", XLR: "Out",
    OUT: "Out", SEND: "Send" };
  function portLabel(port) {
    if (!port || port === "EMPTY") return null;
    if (port === "PREV_ROW") return "Prev. row";
    if (port === "MULTIPLE") return "Multi Out";
    if (port === "SIDECHAIN_BUFFER") return "S/C";
    const m = /^([A-Z_]+?)_(\d+(?:_\d+)*)$/.exec(String(port));
    if (m && PORT_WORDS[m[1]]) return PORT_WORDS[m[1]] + " " + m[2].split("_").join("/");
    return String(port).replace(/_/g, " ");
  }

  // A block's category (API "category") -> the sidebar group key (/api/editor/catalog).
  // Used only when the block's model is not in the catalog (plugins are found by id).
  const GROUP_RULES = [
    [/neural capture|capture/, "capture"], [/cabsim|cab sim/, "cab"], [/ir ?loader/, "ir"],
    [/amplifier|\bamp\b/, "amp"], [/overdrive|drive/, "overdrive"], [/delay/, "delay"],
    [/reverb/, "reverb"], [/compressor/, "compressor"], [/pitch/, "pitch"],
    [/modulation/, "modulation"], [/filter/, "filter"], [/wah/, "wah"], [/equali[sz]er|\beq\b/, "eq"],
    [/morph/, "morph"], [/synth/, "synth"], [/fx ?loop/, "fxloop"], [/looper/, "looper"],
    [/utility/, "utility"], [/plugin/, "plugins"],
  ];
  function groupKeyForCategory(category) {
    const c = String(category || "").toLowerCase();
    for (const [re, key] of GROUP_RULES) if (re.test(c)) return key;
    return "utility";
  }

  const EditorCore = {
    range, proportion, angle, quantize, valueFromProportion, formatValue, stepInterval,
    stepValue, dragValue, selectStyle, arcPath, Throttle, portLabel, groupKeyForCategory,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = EditorCore;
  else window.EditorCore = EditorCore;
})();
