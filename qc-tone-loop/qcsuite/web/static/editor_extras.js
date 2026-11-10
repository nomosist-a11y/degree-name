// エディター: settings that are not blocks, the way Cortex Control shows them.
//
//  * row output (Out port menu): VOLUME (dB, Off at the bottom), PAN, MUTE, SOLO;
//    M / S badges on the Out port while active
//  * row input gate (In port menu): NOISE REDUCTION %, INPUT GAIN dB, on / off
//  * splitter / mixer of a split row (Out port menu, 分岐) and the split's mute
//  * scenes (right-click a scene button): colour, copy (cannot be undone), swap;
//    the scene colour shows as a bar on the button
//  * per-scene values and expression pedals for a block parameter (long press or
//    right-click a knob / select); "S" and "EXP1/2" tags on the parameter
//  * tempo (status bar): BPM field and TAP (also the T key)
//  * footswitches: block menu assigns A-H (letter badge on the block); the preset ⋮
//    menu opens the overview (label, momentary, blocks)
//  * Gig View button (the server refuses it: it is on the safety rules' list)
//
// Every change is POST /api/editor/extra/<action> with the value the page showed as
// `expect`; the server writes nothing on a mismatch (409) and the page reads the grid
// again. One change = one undo step (scene copy excepted). Knobs here send ONE write
// when let go. Nothing is saved.
//
// The pure helpers at the top have no DOM (node tests: tests/js/editor_extras.test.js).
"use strict";

(function () {
  // ------------------------------------------------------------ pure helpers
  const LANE_FLOOR_DB = -39.5;     // lowest numeric step; below it the knob reads Off (wire 0)
  const TAP_RESET_MS = 2000;
  const TAP_KEEP = 8;
  const TEMPO_MIN = 30, TEMPO_MAX = 250;
  // Eight swatches like the unit's scene colour picker. The unit's exact palette is not
  // published: these are the mock unit's default scene colours (fake_data.SCENE_COLORS).
  const SCENE_COLORS = [
    ["#e5484d", "赤"], ["#f76b15", "オレンジ"], ["#ffc53d", "黄"], ["#46a758", "緑"],
    ["#12a594", "青緑"], ["#0090ff", "青"], ["#8e4ec6", "紫"], ["#d6409f", "ピンク"],
  ];
  const FOOTSWITCHES = "ABCDEFGH".split("");

  function clamp(x, lo, hi) { return Math.min(hi, Math.max(lo, x)); }
  function round1(x) { return Math.round(x * 10) / 10; }

  // Row VOLUME / splitter and mixer LEVEL: dB = -40 + 52 * wire (pyquadcortex lane_level_db).
  function laneDb(wire) {
    if (typeof wire !== "number" || !isFinite(wire) || wire <= 0) return null;
    return -40 + 52 * wire;
  }
  function dbToWire(db) { return (clamp(db, -40, 12) + 40) / 52; }
  // Snap a dragged wire value onto 0.1 dB steps; below the numeric floor it is Off (0).
  function laneSnap(wire) {
    const w = clamp(wire, 0, 1);
    const db = round1(-40 + 52 * w);
    if (db < LANE_FLOOR_DB) return 0;
    return dbToWire(Math.min(12, db));
  }
  function laneStep(wire, dir, fine) {
    const db = laneDb(wire);
    if (db === null) return dir > 0 ? dbToWire(LANE_FLOOR_DB) : 0;
    const next = round1(db + (dir > 0 ? 1 : -1) * (fine ? 0.1 : 0.5));
    if (next < LANE_FLOOR_DB) return 0;
    return dbToWire(Math.min(12, next));
  }
  function laneText(wire) {
    if (typeof wire !== "number" || !isFinite(wire)) return { text: "—", unit: "" };
    const db = laneDb(wire);
    if (db === null) return { text: "Off", unit: "" };
    const r = round1(db);
    return { text: `${r > 0 ? "+" : ""}${(r === 0 ? 0 : r).toFixed(1)}`, unit: "dB" };
  }
  function panText(wire) {
    if (typeof wire !== "number" || !isFinite(wire)) return { text: "—", unit: "" };
    const amount = Math.round((wire - 0.5) * 200);
    return { text: amount === 0 ? "C" : amount < 0 ? `L${-amount}` : `R${amount}`, unit: "" };
  }
  function centeredPct(wire) {
    if (typeof wire !== "number" || !isFinite(wire)) return { text: "—", unit: "" };
    const n = Math.round((wire - 0.5) * 200);
    return { text: String(n === 0 ? 0 : n), unit: "%" };
  }
  function panSnap(wire) { return Math.round(clamp(wire, 0, 1) * 200) / 200; }

  // Tap tempo: taps (ms, ascending) -> BPM from the average interval of the last taps.
  function tapPush(taps, now, resetMs = TAP_RESET_MS) {
    const last = taps.length ? taps[taps.length - 1] : null;
    if (last === null || now - last > resetMs || now < last) return [now];
    return [...taps, now].slice(-TAP_KEEP);
  }
  function tapBpm(taps) {
    if (!taps || taps.length < 2) return null;
    const span = taps[taps.length - 1] - taps[0];
    if (!(span > 0)) return null;
    const avg = span / (taps.length - 1);
    return round1(clamp(60000 / avg, TEMPO_MIN, TEMPO_MAX));
  }
  function parseBpm(text) {
    const v = Number(String(text).trim().replace(",", "."));
    if (!isFinite(v) || String(text).trim() === "") return null;
    return round1(clamp(v, TEMPO_MIN, TEMPO_MAX));
  }

  // Expression sweep: wire 0..1 <-> whole percent.
  function wireToPct(w) { return typeof w === "number" && isFinite(w) ? Math.round(clamp(w, 0, 1) * 100) : null; }
  function pctToWire(p) {
    const v = Number(p);
    return isFinite(v) ? clamp(v, 0, 100) / 100 : null;
  }

  // Popover position: under the anchor (above when it does not fit), always inside the
  // viewport with an 8px margin. rect = anchor's getBoundingClientRect().
  function placePop(rect, w, h, vw, vh) {
    const m = 8;
    let left = Math.min(vw - w - m, Math.max(m, rect.right - w));
    if (rect.left + w < vw - m && rect.left < vw / 2) left = Math.max(m, rect.left);
    left = Math.max(m, Math.min(left, vw - w - m));
    let top = rect.bottom + 6;
    if (top + h > vh - m) top = rect.top - h - 6;
    top = Math.max(m, Math.min(top, vh - h - m));
    return { left: Math.max(0, left), top: Math.max(0, top) };
  }

  // Linear knob drag: about 200 px for the whole range (2000 px with Shift). dy > 0 = down.
  function dragProp(startProp, dy, fine) { return clamp(startProp - dy / (fine ? 2000 : 200), 0, 1); }

  function stompSummary(entry) {
    if (!entry) return "割り当てなし";
    const n = (entry.blocks || []).length;
    return `${entry.label ? `${entry.label}・` : ""}${n} ブロック${entry.momentary ? "・モーメンタリー" : ""}`;
  }

  const Core = { laneDb, dbToWire, laneSnap, laneStep, laneText, panText, centeredPct, panSnap, tapPush, tapBpm,
    parseBpm, wireToPct, pctToWire, placePop, dragProp, stompSummary, SCENE_COLORS, FOOTSWITCHES,
    LANE_FLOOR_DB };
  if (typeof module !== "undefined" && module.exports) module.exports = Core;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // ------------------------------------------------------------ page state
  const X = {
    holding: 0,          // knob drags / pending key-wheel commits (the grid is not redrawn meanwhile)
    taps: [],
    tapTimer: null,
    tapShown: null,      // BPM shown while tapping
    gig: false,
    gigRefused: null,
    lastLong: 0,
  };
  const where = (r, c) => `行${r + 1} 列${c + 1}`;
  const nameOf = b => b.title || b.model;
  const rowOf = row => (ED.view && ED.view.rows || []).find(r => r.row === row) || null;

  // ------------------------------------------------------------ one call per change

  async function post(action, body, okText) {
    if (ED.busy) { toast("前の操作の途中です(書き込んでいません)", true); loadGrid(); return null; }
    if (ED.gesture) await endGesture(ED.gesture);
    ED.busy = true;
    let res = null;
    try {
      res = await edFetch(`/api/editor/extra/${action}`, body);
      (res.warnings || []).forEach(w => toast(w, true));
      if (res.id !== null && res.id !== undefined) {
        if (okText) toast(`${okText}(保存はしていません。取り消しで戻せます)`);
      } else if (action === "scene_copy" && res.note) {
        toast(`${okText || "コピーしました"}。${res.note}`);
      }
    } catch (e) {
      /* edFetch showed why (409: changed on the unit, nothing written; 400: refused) */
    } finally {
      ED.busy = false;
    }
    ED.reloadWanted = false;
    await loadGrid();
    loadHistory();
    return res;
  }

  function reloadIfWanted() {
    if (X.holding === 0 && ED.reloadWanted && !ED.gesture) { ED.reloadWanted = false; loadGrid(); }
  }

  // ------------------------------------------------------------ own popovers (share editor.js's openPop)

  function openPopover(anchor, node) {
    const again = openPop && openPop.anchor === anchor;
    closeMenu();
    if (again) return null;
    node.classList.add("pop", "ex-pop");
    node.setAttribute("role", "dialog");
    document.body.append(node);
    const r = anchor.getBoundingClientRect();
    const pos = placePop(r, node.offsetWidth, node.offsetHeight, window.innerWidth, window.innerHeight);
    node.style.left = `${pos.left}px`;
    node.style.top = `${pos.top}px`;
    anchor.setAttribute("aria-expanded", "true");
    openPop = { node, anchor };
    return node;
  }

  function liveAnchor(node, fallbackSel) {
    if (node && node.isConnected) return node;
    return document.querySelector(fallbackSel) || document.body;
  }

  // ------------------------------------------------------------ knob (one write on release)
  //
  // opt: {name, value, min, max, fmt(v)->{text,unit}, snap(v), step(v,dir,fine), live, why,
  //       commit(v)}. While dragging the value is drawn locally; letting go sends once.

  function knob(opt) {
    const live = !!opt.live && typeof opt.value === "number";
    const p = { min: opt.min, max: opt.max };
    const span = opt.max - opt.min;
    const snap = opt.snap || (v => v);
    let cur = live ? opt.value : null;
    const attrs = { class: `cc-cell knob ${live ? "live" : "off"}` };
    if (live) {
      Object.assign(attrs, { tabindex: "0", role: "slider", "aria-label": opt.name,
        "aria-valuemin": String(opt.min), "aria-valuemax": String(opt.max), "aria-valuenow": String(cur),
        title: "上下にドラッグ(Shift で細かく)/ホイール/矢印キー。離したときに 1 回だけ書き込みます" });
    } else {
      attrs.title = opt.why || "変更できません";
      attrs["aria-disabled"] = "true";
    }
    const val = v => {
      const f = opt.fmt(v);
      return el("span", { class: "cc-val" }, f.text, f.unit ? el("i", {}, f.unit) : null);
    };
    const shown = typeof opt.value === "number" ? opt.value : opt.min;
    const cell = el("div", attrs, el("span", { class: "cc-name" }, opt.name),
      el("div", { class: "cc-body" }, val(typeof opt.value === "number" ? opt.value : null), knobSvg(p, shown)));
    if (!live) return cell;
    const draw = v => {
      cell.querySelector(".cc-val").replaceWith(val(v));
      cell.querySelector(".cc-knob").replaceWith(knobSvg(p, v));
      const f = opt.fmt(v);
      cell.setAttribute("aria-valuenow", String(v));
      cell.setAttribute("aria-valuetext", f.text + (f.unit ? ` ${f.unit}` : ""));
    };
    const prop = v => (span > 0 ? clamp((v - opt.min) / span, 0, 1) : 0);
    const changed = () => Math.abs(cur - opt.value) > 1e-9;
    const finish = () => {
      X.holding = Math.max(0, X.holding - 1);
      cell.classList.remove("active");
      if (changed()) opt.commit(cur);
      else reloadIfWanted();
    };
    let drag = null;
    cell.addEventListener("pointerdown", e => {
      if (e.button !== 0 || ED.busy) return;
      e.preventDefault();
      cell.focus({ preventScroll: true });
      try { cell.setPointerCapture(e.pointerId); } catch { /* pointer gone */ }
      drag = { id: e.pointerId, y: e.clientY, prop: prop(cur), fine: e.shiftKey };
      X.holding++;
      cell.classList.add("active");
    });
    cell.addEventListener("pointermove", e => {
      if (!drag || e.pointerId !== drag.id) return;
      if (e.shiftKey !== drag.fine) { drag = { ...drag, y: e.clientY, prop: prop(cur), fine: e.shiftKey }; return; }
      const v = snap(opt.min + span * dragProp(drag.prop, e.clientY - drag.y, drag.fine));
      if (v === cur) return;
      cur = v;
      draw(v);
    });
    const release = e => {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      finish();
    };
    cell.addEventListener("pointerup", release);
    cell.addEventListener("pointercancel", release);
    cell.addEventListener("lostpointercapture", release);
    // Wheel / arrow keys: the value moves at once, one write after 500 ms of quiet.
    let idle = null;
    const nudge = (dir, fine) => {
      const v = opt.step ? opt.step(cur, dir, fine) : snap(cur + dir * span * (fine ? 0.001 : 0.01));
      const next = clamp(v, Math.min(opt.min, opt.max), Math.max(opt.min, opt.max));
      if (next === cur) return;
      if (!idle) { X.holding++; cell.classList.add("active"); }
      else clearTimeout(idle);
      cur = next;
      draw(next);
      idle = setTimeout(() => { idle = null; finish(); }, 500);
    };
    cell.addEventListener("wheel", e => {
      e.preventDefault();
      if (drag || ED.busy) return;
      const dy = e.deltaY || -e.deltaX;
      if (dy) nudge(dy < 0 ? +1 : -1, e.shiftKey);
    }, { passive: false });
    cell.addEventListener("keydown", e => {
      let dir = 0;
      if (e.key === "ArrowUp" || e.key === "ArrowRight") dir = +1;
      else if (e.key === "ArrowDown" || e.key === "ArrowLeft") dir = -1;
      if (!dir || e.metaKey || e.ctrlKey || e.altKey) return;
      e.preventDefault();
      e.stopPropagation();
      if (!ED.busy) nudge(dir, e.shiftKey);
    });
    return cell;
  }

  // Two-way switch cell (OFF / ON) in the cc-tog style; value: bool or null (unknown).
  function toggleCell(name, value, labels, onPick, why) {
    const live = typeof value === "boolean" && !why;
    return el("div", { class: `cc-cell select ${live ? "" : "off"}`, title: why || null },
      el("span", { class: "cc-name" }, name),
      el("div", { class: "cc-body" }, el("div", { class: "cc-tog", role: "group", "aria-label": name },
        [false, true].map((v, i) => {
          const on = value === v;
          const b = el("button", { type: "button", class: on ? "on" : "", disabled: !live, "aria-pressed": String(on) }, labels[i]);
          b.addEventListener("click", () => { if (!on) onPick(v); });
          return b;
        }))));
  }

  function padCells(cells) {
    const pad = (5 - (cells.length % 5)) % 5;
    for (let k = 0; k < pad; k++) cells.push(el("div", { class: "cc-cell blank", "aria-hidden": "true" }));
    return cells;
  }

  // ------------------------------------------------------------ row panels in the parameter area

  function openPanel(kind, row) {
    if (ED.gesture) endGesture(ED.gesture);
    ED.exPanel = { kind, row };
    ED.sel = null;
    ED.sbGroup = null;
    renderGrid();
    renderParams();
    renderSidebar();
    const host = document.getElementById("edParams");
    if (host && SB_DRAWER_MQ.matches) host.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function closePanel() {
    if (!ED.exPanel) return;
    ED.exPanel = null;
    renderGrid();
    renderParams();
  }

  function panelShell(title, sub, kids, power) {
    const close = el("button", { type: "button", class: "cc-power ex-close", "aria-label": "閉じる", title: "閉じる(Esc)" },
      svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: "M7 7l10 10M17 7 7 17" })));
    close.addEventListener("click", closePanel);
    return el("div", { class: "cc ex-panel", "data-c": "util" },
      el("div", { class: "cc-head" },
        el("span", { class: "cc-dots ex-dot", "aria-hidden": "true" }),
        el("span", { class: "cc-title" }, el("span", { class: "cc-cat" }, title), sub ? el("b", {}, sub) : null),
        power, close),
      kids);
  }

  function lanePanel(r) {
    const L = r.lane || {};
    const row = r.row;
    const vol = knob({ name: "VOLUME", value: L.volume, min: 0, max: 1, fmt: laneText, snap: laneSnap, step: laneStep,
      live: typeof L.volume === "number", why: "VOLUME が読めません",
      commit: v => post("lane", v <= 0 ? { row, param: "VOLUME", value: 0, expect: L.volume }
        : { row, param: "VOLUME", db: round1(laneDb(v)), expect: L.volume },
      `行${row + 1} の出力 VOLUME を ${laneText(v).text}${laneText(v).unit ? ` ${laneText(v).unit}` : ""} にしました`) });
    const pan = knob({ name: "PAN", value: L.pan, min: 0, max: 1, fmt: panText, snap: panSnap,
      step: (v, dir, fine) => panSnap(v + dir * (fine ? 0.005 : 0.01)),
      live: typeof L.pan === "number", why: "PAN が読めません",
      commit: v => post("lane", { row, param: "PAN", value: v, expect: L.pan }, `行${row + 1} の PAN を ${panText(v).text} にしました`) });
    const mute = toggleCell("MUTE", L.mute, ["OFF", "ON"],
      on => post("lane", { row, param: "MUTE", on, expect: L.mute }, `行${row + 1} の出力を${on ? "ミュートしました" : "ミュート解除しました"}`));
    const solo = toggleCell("SOLO", L.solo, ["OFF", "ON"],
      on => post("lane", { row, param: "SOLO", on, expect: L.solo }, `行${row + 1} の SOLO を${on ? "オン" : "オフ"}にしました`));
    return panelShell("出力", `行${row + 1} ${EC.portLabel(r.out_port) || ""}`,
      [el("div", { class: "cc-banner based" }, "この行の出力の音量・定位です。保存はしません。VOLUME はいちばん下で Off(無音)。"),
        el("div", { class: "cc-grid" }, padCells([vol, pan, mute, solo]))]);
  }

  function gatePanel(r) {
    const G = r.gate || {};
    const row = r.row;
    const ranges = G.ranges || {};
    const nrR = ranges.noise_reduction || [0, 100];
    const igR = ranges.input_gain_db || [-24, 24];   // the server clamps to the catalog's range
    const nr = knob({ name: "NOISE REDUCTION", value: G.noise_reduction, min: nrR[0], max: nrR[1],
      fmt: v => (typeof v === "number" ? { text: String(Math.round(v)), unit: "%" } : { text: "—", unit: "" }),
      snap: v => Math.round(v), step: (v, dir, fine) => Math.round(v) + dir * (fine ? 1 : 1),
      live: typeof G.noise_reduction === "number", why: "単位の範囲がカタログから分からないため変えられません",
      commit: v => post("gate", { row, param: "NOISE REDUCTION", real: v, expect: G.noise_reduction },
        `行${row + 1} の NOISE REDUCTION を ${Math.round(v)}% にしました`) });
    const gainFmt = v => (typeof v === "number" ? { text: `${v > 0 ? "+" : ""}${(Math.abs(v) < 0.05 ? 0 : v).toFixed(1)}`, unit: "dB" }
      : { text: "—", unit: "" });
    const ig = knob({ name: "INPUT GAIN", value: G.input_gain_db, min: igR[0], max: igR[1], fmt: gainFmt,
      snap: v => round1(v), step: (v, dir, fine) => round1(v + dir * (fine ? 0.1 : 0.5)),
      live: typeof G.input_gain_db === "number", why: "単位の範囲がカタログから分からないため変えられません",
      commit: v => post("gate", { row, param: "INPUT GAIN", real: v, expect: G.input_gain_db },
        `行${row + 1} の INPUT GAIN を ${gainFmt(v).text} dB にしました`) });
    const unknown = typeof G.bypassed !== "boolean";
    const power = el("button", { type: "button", class: "cc-power", disabled: unknown,
      "aria-pressed": unknown ? null : String(!G.bypassed),
      "aria-label": unknown ? "ゲートの状態が分かりません" : G.bypassed ? "ゲートはオフ(押すとオン)" : "ゲートはオン(押すとオフ)",
      title: unknown ? "ゲートの状態が分かりません" : `ゲート${G.bypassed ? "オフ" : "オン"}。クリックで切り替え` }, POWER_ICON());
    power.addEventListener("click", () => {
      if (unknown) return;
      post("gate", { row, param: "BYPASS", on: !G.bypassed, expect: G.bypassed },
        `行${row + 1} の入力ゲートを${G.bypassed ? "オン" : "オフ"}にしました`);
    });
    return panelShell("入力ゲート", `行${row + 1} ${EC.portLabel(r.in_port) || ""}`,
      [el("div", { class: "cc-banner based" }, `入力のノイズゲートとゲインです。右上のボタンでゲートのオン/オフ(いま${unknown ? "不明" : G.bypassed ? "オフ" : "オン"})。保存はしません。`),
        el("div", { class: "cc-grid" }, padCells([nr, ig]))], power);
  }

  function subCell(r, which, sp) {
    const row = r.row;
    const word = which === "splitter" ? "スプリッター" : "ミキサー";
    const why = sp.writable ? "" : (sp.kind === "select" ? "選択肢の名前が本体から読めないため変えられません" : "ここでは変えられません");
    if (sp.kind === "select") {
      const opts = sp.options || [];
      const curIdx = opts.indexOf(sp.text);
      const live = sp.writable && opts.length > 0;
      const pick = option => post(which, { row, param: sp.name, option, expect: sp.text },
        `行${row + 1} の${word} ${sp.name} を ${option} にしました`);
      let control;
      if (opts.length > 2) {
        const sel = el("select", { class: "cc-sel", "aria-label": sp.name, disabled: !live, title: why || null },
          curIdx < 0 ? el("option", { value: "", disabled: true, selected: true }, sp.text || "—") : null,
          opts.map((o, i) => el("option", { value: String(i), selected: i === curIdx }, o)));
        sel.addEventListener("change", () => pick(opts[Number(sel.value)]));
        control = el("div", { class: "cc-selwrap" }, sel);
      } else if (opts.length) {
        control = el("div", { class: "cc-tog", role: "group", "aria-label": sp.name }, opts.map((o, i) => {
          const b = el("button", { type: "button", class: i === curIdx ? "on" : "", disabled: !live,
            "aria-pressed": String(i === curIdx), title: why || null }, o);
          b.addEventListener("click", () => { if (i !== curIdx) pick(o); });
          return b;
        }));
      } else {
        control = el("span", { class: "cc-text", "aria-disabled": "true" }, sp.text || "—");
      }
      return el("div", { class: `cc-cell select ${live ? "" : "off"}`, title: why || null },
        el("span", { class: "cc-name" }, sp.name), el("div", { class: "cc-body" }, control));
    }
    const isLevel = sp.units === "dB" && /LEVEL/i.test(sp.name);
    const isPan = /^(PAN|BALANCE)/i.test(sp.name) && !isLevel;
    // PAN / BALANCE: centre at wire 0.5. Shown as the server shows it ("0 %") when it has units,
    // else as L/C/R. Only the live value while turning; the read-back text replaces it.
    const panFmt = sp.units === "%" ? centeredPct : panText;
    const full = f => f.text + (f.unit ? ` ${f.unit}` : "");
    const okText = v => (isLevel || isPan ? `行${row + 1} の${word} ${sp.name} を ${full(isLevel ? laneText(v) : panFmt(v))} にしました`
      : `行${row + 1} の${word} ${sp.name} を変えました`);
    if (isLevel) {
      return knob({ name: sp.name, value: sp.value, min: 0, max: 1, fmt: laneText, snap: laneSnap, step: laneStep,
        live: sp.writable && typeof sp.value === "number", why,
        commit: v => post(which, v <= 0 ? { row, param: sp.name, value: 0, expect: sp.value }
          : { row, param: sp.name, db: round1(laneDb(v)), expect: sp.value }, okText(v)) });
    }
    if (isPan) {
      return knob({ name: sp.name, value: sp.value, min: 0, max: 1, fmt: panFmt, snap: panSnap,
        step: (v, dir, fine) => panSnap(v + dir * (fine ? 0.005 : 0.01)),
        live: sp.writable && typeof sp.value === "number", why,
        commit: v => post(which, { row, param: sp.name, value: v, expect: sp.value }, okText(v)) });
    }
    // Other knobs: the server's text for the value it read; while turning, the knob position in %.
    const start = sp.value;
    const fmt = v => (v === start || typeof v !== "number"
      ? { text: sp.text || "—", unit: "" } : { text: String(Math.round(v * 100)), unit: "%" });
    return knob({ name: sp.name, value: sp.value, min: 0, max: 1, fmt, snap: v => Math.round(v * 1000) / 1000,
      live: sp.writable && typeof sp.value === "number", why,
      commit: v => post(which, { row, param: sp.name, value: v, expect: sp.value },
        `行${row + 1} の${word} ${sp.name} を変えました`) });
  }

  function splitPanel(r) {
    const row = r.row;
    const kids = [];
    if (!r.split) {
      kids.push(el("div", { class: "cc-none" }, `行${row + 1} には分岐がありません。Out のメニューの「分岐を作る…」で作れます。`));
      return panelShell("分岐", `行${row + 1}`, kids);
    }
    kids.push(el("div", { class: "cc-banner based" },
      `列${r.split[0] + 1} で分岐${r.split[1] < 0 ? "(合流しない)" : `、列${r.split[1] + 1} で合流`}。並列の経路は行${row + 2}。保存はしません。`));
    const muteCell = toggleCell("MUTE", typeof r.split_muted === "boolean" ? r.split_muted : null, ["OFF", "ON"],
      muted => post("split_mute", { row, muted, expect: r.split_muted }, `行${row + 1} の分岐を${muted ? "ミュートしました" : "ミュート解除しました"}`),
      typeof r.split_muted === "boolean" ? "" : "分岐のミュートが読めません");
    for (const [which, word] of [["splitter", "スプリッター"], ["mixer", "ミキサー"]]) {
      const st = r[which];
      kids.push(el("div", { class: "ex-sec" }, word));
      const cells = st && st.params && st.params.length ? st.params.map(sp => subCell(r, which, sp)) : [];
      if (which === "mixer") cells.push(muteCell);
      kids.push(cells.length ? el("div", { class: "cc-grid" }, padCells(cells))
        : el("div", { class: "cc-none" }, `${word}の設定を本体から読めませんでした。`));
    }
    return panelShell("分岐", `行${row + 1} SPLIT / MIX`, kids);
  }

  // Called first by renderParams(); true = the panel area now shows a row panel.
  function renderPanel(host) {
    const P = ED.exPanel;
    if (!P) return false;
    if (ED.sel) { ED.exPanel = null; return false; }
    const r = rowOf(P.row);
    if (!r) { ED.exPanel = null; return false; }
    const node = P.kind === "lane" ? lanePanel(r) : P.kind === "gate" ? gatePanel(r) : splitPanel(r);
    host.replaceChildren(node);
    return true;
  }

  // ------------------------------------------------------------ grid decorations

  function decorateGrid(box) {
    if (!ED.view) return;
    for (const r of ED.view.rows) {
      const out = box.querySelector(`.ed-port.out[data-row="${r.row}"]`);
      const inp = box.querySelector(`.ed-port.in[data-row="${r.row}"]`);
      const L = r.lane || {};
      if (out && (L.mute || L.solo || L.volume_off)) {
        out.append(el("span", { class: "ex-pb", "aria-hidden": "true" },
          L.mute ? el("b", { class: "m", title: "ミュート中" }, "M") : null,
          L.solo ? el("b", { class: "s", title: "ソロ" }, "S") : null,
          !L.mute && L.volume_off ? el("b", { class: "m", title: "VOLUME Off" }, "0") : null));
        out.title += `${L.mute ? "\nミュート中" : ""}${L.solo ? "\nソロ" : ""}${L.volume_off ? "\nVOLUME Off" : ""}`;
      }
      const P = ED.exPanel;
      if (P && P.row === r.row) {
        const node = P.kind === "gate" ? inp : out;
        if (node) node.classList.add("ex-on");
      }
      for (const c of r.cells) {
        if (!c || !c.stomp) continue;
        const cell = box.querySelector(`.ed-cell[data-row="${c.row}"][data-col="${c.col}"]`);
        if (cell) {
          cell.append(el("span", { class: "ex-fs", title: `フットスイッチ ${c.stomp}` }, c.stomp));
          cell.title += `\nフットスイッチ ${c.stomp}`;
        }
      }
    }
  }

  // Extra items for the In / Out port menu (editor_layout.js portMenu).
  function portMenuItems(r, side) {
    if (side === "in") {
      const G = r.gate || {};
      return [{ label: `入力ゲート…${typeof G.bypassed === "boolean" ? (G.bypassed ? "(オフ)" : "(オン)") : ""}`,
        onClick: () => openPanel("gate", r.row) }, { sep: true }, { head: "入力ポート" }];
    }
    const L = r.lane || {};
    const now = [laneText(L.volume).text + (laneText(L.volume).unit ? " dB" : ""), L.mute ? "ミュート" : null, L.solo ? "ソロ" : null]
      .filter(Boolean).join("・");
    return [{ label: `出力の音量と定位…${now && now !== "—" ? `(${now})` : ""}`,
      onClick: () => openPanel("lane", r.row) }, { sep: true }, { head: "出力ポート" }];
  }

  function splitMenuItem(r) {
    return { label: `スプリッター/ミキサーの設定…${r.split_muted ? "(ミュート中)" : ""}`, onClick: () => openPanel("split", r.row) };
  }

  // ------------------------------------------------------------ footswitches

  function blockMenuItems(b) {
    return [
      { sep: true },
      { head: `フットスイッチ${b.stomp ? `(いま ${b.stomp})` : ""}` },
      { label: b.stomp ? "別のフットスイッチに割り当て…" : "フットスイッチに割り当て…", onClick: () => stompPicker(b) },
      b.stomp ? { label: `割り当てを外す(${b.stomp})`, onClick: () => assignStomp(b, null) } : null,
    ];
  }

  function stompPicker(b) {
    const anchor = liveAnchor(document.querySelector(`.ed-cell[data-row="${b.row}"][data-col="${b.col}"]`), "#edParams .cc-dots");
    const map = (ED.view && ED.view.stomp) || {};
    openMenu(anchor, [
      { head: `${nameOf(b)} をどのフットスイッチに?` },
      ...FOOTSWITCHES.map(fs => ({
        label: `${fs}  ${map[fs] ? stompSummary(map[fs]) : "空き"}`, on: fs === b.stomp, disabled: fs === b.stomp,
        onClick: () => assignStomp(b, fs) })),
      { sep: true },
      { info: "ストンプモードのフットスイッチです。1 つのスイッチに複数のブロックを割り当てられます。" },
    ]);
  }

  function assignStomp(b, fs) {
    return post("stomp", { row: b.row, col: b.col, model_id: b.model_id, footswitch: fs, expect: b.stomp ?? null },
      fs ? `${nameOf(b)} をフットスイッチ ${fs} に割り当てました` : `${nameOf(b)} のフットスイッチの割り当てを外しました`);
  }

  function openStomps() {
    const list = el("div", { class: "ex-fslist" });
    const close = el("button", { type: "button", class: "primary" }, "閉じる");
    const dlg = el("dialog", { class: "ed-dlg ed-dlg-wide ex-fsdlg" }, el("h3", {}, "フットスイッチ(ストンプ)"),
      el("p", { class: "ed-dlg-note" }, "ブロックの割り当ては、ブロックの ⋮ メニュー(右クリック)から。名前は Enter かほかを押すと送ります。モーメンタリーは 1 つのブロックだけを動かすスイッチで効きます(本体の仕様)。保存はしません。"),
      list, el("div", { class: "row" }, close));
    const fill = () => {
      const map = (ED.view && ED.view.stomp) || {};
      list.replaceChildren(...FOOTSWITCHES.map(fs => {
        const e = map[fs] || null;
        const input = el("input", { type: "text", maxlength: "30", value: e ? e.label || "" : "", placeholder: e ? "名前なし" : "割り当てなし",
          disabled: !e, "aria-label": `フットスイッチ ${fs} の名前` });
        let sent = false;
        const send = async () => {
          if (!e || sent) return;
          const label = input.value.trim();
          if (label === (e.label || "")) return;
          sent = true;
          await post("stomp_label", { footswitch: fs, label, expect: e.label || "" }, `フットスイッチ ${fs} の名前を変えました`);
          if (dlg.open) fill();
        };
        input.addEventListener("keydown", ev => {
          if (ev.key === "Enter" && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); send(); }
        });
        input.addEventListener("change", send);
        const one = !!e && e.blocks.length === 1;
        const mom = el("button", { type: "button", class: `ex-mom ${e && e.momentary ? "on" : ""}`, disabled: !e,
          "aria-pressed": String(!!(e && e.momentary)),
          title: !e ? "割り当てがありません" : one ? "押している間だけ効く(モーメンタリー)/押すたびに切り替え(ラッチ)" : "2 つ以上のブロックを動かすスイッチでは、本体がモーメンタリーを受け付けません" },
        e && e.momentary ? "モーメンタリー" : "ラッチ");
        mom.addEventListener("click", async () => {
          await post("stomp_momentary", { footswitch: fs, momentary: !e.momentary, expect: e.momentary },
            `フットスイッチ ${fs} を${e.momentary ? "ラッチ" : "モーメンタリー"}にしました`);
          if (dlg.open) fill();
        });
        const blocks = e ? e.blocks.map(p => {
          const c = cellAt(p.row, p.col);
          return `${where(p.row, p.col)} ${c ? nameOf(c) : "?"}`;
        }).join(" / ") : "—";
        return el("div", { class: `ex-fsrow ${e ? "" : "idle"}` },
          el("span", { class: "ex-fsl" }, fs), input, mom, el("span", { class: "ex-fsb" }, blocks));
      }));
    };
    const done = () => { dlg.close(); dlg.remove(); };
    close.addEventListener("click", done);
    dlg.addEventListener("cancel", ev => { ev.preventDefault(); done(); });
    fill();
    document.body.append(dlg);
    dlg.showModal();
    close.focus();
  }

  // ------------------------------------------------------------ scenes

  function decorateScene(btn, i) {
    const colors = (ED.view && ED.view.scene_colors) || [];
    const c = colors[i];
    if (typeof c === "string" && /^#[0-9a-f]{6}$/i.test(c)) {
      btn.classList.add("ex-scol");
      btn.style.setProperty("--scol", c);
    }
  }

  function sceneMenuItems(i, btn) {
    return [{ sep: true },
      { label: "色を変える…", onClick: () => colorPop(btn, i) },
      { label: "シーンをコピー…", onClick: () => scenePick(btn, i, false) },
      { label: "シーンと入れ替え…", onClick: () => scenePick(btn, i, true) }];
  }

  const sceneBtn = (btn, i) => (btn && btn.isConnected ? btn : document.querySelectorAll(".pb-scene")[i] || document.body);

  function colorPop(btn, i) {
    const s = "ABCDEFGH"[i];
    const cur = ((ED.view && ED.view.scene_colors) || [])[i] || null;
    const node = el("div", { "aria-label": `シーン ${s} の色` },
      el("div", { class: "pop-h" }, `シーン ${s} の色`),
      el("div", { class: "ex-sw" }, SCENE_COLORS.map(([hex, name]) => {
        const on = !!cur && cur.toLowerCase() === hex;
        const b = el("button", { type: "button", class: on ? "on" : "", style: `--sw:${hex}`, "aria-label": name,
          "aria-pressed": String(on), title: name });
        b.addEventListener("click", () => {
          closeMenu();
          if (!on) post("scene_color", { scene: i, color: hex, expect: cur }, `シーン ${s} の色を${name}にしました`);
        });
        return b;
      })),
      el("div", { class: "pop-info" }, "本体のシーンの色が変わります。保存はしません。"));
    if (openPopover(sceneBtn(btn, i), node)) {
      const first = node.querySelector("button");
      if (first) first.focus({ preventScroll: true });
    }
  }

  function scenePick(btn, i, swap) {
    const labels = (ED.view && ED.view.scene_labels) || [];
    const s = "ABCDEFGH"[i];
    const lab = j => ((labels[j] || "").trim() ? `: ${labels[j].trim()}` : "");
    openMenu(sceneBtn(btn, i), [
      { head: swap ? `シーン ${s} と入れ替えるシーン` : `シーン ${s} をどこにコピー?` },
      ...FOOTSWITCHES.map((t, j) => (j === i ? null : {
        label: `シーン ${t}${lab(j)}`,
        onClick: () => (swap ? swapScenes(i, j) : copyScene(i, j)) })),
      { sep: true },
      { info: swap ? "2 つのシーンの中身(値・バイパス・名前・色)を入れ替えます。取り消しで戻せます。"
        : "コピー先のシーンは上書きされます。この操作は取り消せません。" },
    ]);
  }

  async function copyScene(a, b) {
    const S = "ABCDEFGH";
    if (!await confirmDialog("シーンのコピー",
      `シーン ${S[a]} の中身(値・バイパス・名前・色)でシーン ${S[b]} を上書きします。この操作は取り消せません。保存はしません。`,
      "上書きする")) return;
    post("scene_copy", { from: a, to: b, swap: false }, `シーン ${S[a]} をシーン ${S[b]} にコピーしました`);
  }

  function swapScenes(a, b) {
    const S = "ABCDEFGH";
    post("scene_copy", { from: a, to: b, swap: true }, `シーン ${S[a]} とシーン ${S[b]} を入れ替えました`);
  }

  // ------------------------------------------------------------ per-scene values, expression pedals

  function paramTags(p) {
    const tags = [];
    if (p.scene_mode) tags.push(el("span", { class: "ex-ptag s", title: "シーンごとに値を覚えます" }, "S"));
    if (p.expression === 1 || p.expression === 2) {
      tags.push(el("span", { class: "ex-ptag e",
        title: `エクスプレッションペダル ${p.expression}(${wireToPct(p.exp_min) ?? "?"}%〜${wireToPct(p.exp_max) ?? "?"}%)` },
      `EXP${p.expression}`));
    }
    return tags;
  }

  // Called by editor.js paramCell() for every knob / select cell.
  function decorateParam(cell, b, p) {
    if (p.kind !== "knob" && p.kind !== "select") return cell;
    const name = cell.querySelector(".cc-name");
    const tags = paramTags(p);
    if (name && tags.length) name.append(...tags);
    const open = () => {
      X.lastLong = Date.now();
      paramMenu(cell, b, p);
    };
    cell.addEventListener("contextmenu", e => {
      e.preventDefault();
      if (Date.now() - X.lastLong < 800) return;   // the long press already opened it
      if (ED.gesture && ED.gesture.cell === cell) return;
      paramMenu(cell, b, p);
    });
    let timer = null, start = null, fired = false;
    const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
    cell.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      fired = false;
      start = { x: e.clientX, y: e.clientY, id: e.pointerId };
      cancel();
      timer = setTimeout(() => {
        timer = null;
        if (ED.gesture && ED.gesture.cell === cell) return;   // it turned into a knob drag
        fired = true;
        try { if (cell.hasPointerCapture(start.id)) cell.releasePointerCapture(start.id); } catch { /* gone */ }
        open();
      }, 600);
    });
    cell.addEventListener("pointermove", e => {
      if (timer && start && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 6) cancel();
    });
    cell.addEventListener("pointerup", cancel);
    cell.addEventListener("pointercancel", cancel);
    // A long press on a select's button must not also pick that option.
    cell.addEventListener("click", e => {
      if (fired) { fired = false; e.stopPropagation(); e.preventDefault(); }
    }, true);
    return cell;
  }

  function paramMenu(cell, b, p) {
    const exp = p.expression === 1 || p.expression === 2 ? p.expression : 0;
    const tgt = { row: b.row, col: b.col, model_id: b.model_id, param: p.name };
    openMenu(cell.isConnected ? cell : liveAnchor(null, "#edParams .cc-dots"), [
      { head: `${p.name}(${nameOf(b)})` },
      { label: `シーンごとに値を覚える${p.scene_mode ? "(オン)" : ""}`, on: !!p.scene_mode,
        title: "オンにすると、このパラメーターはシーンごとに別の値を持ちます",
        onClick: () => post("scene_mode", { ...tgt, enabled: !p.scene_mode, expect: !!p.scene_mode },
          `${p.name} を${p.scene_mode ? "全シーン共通" : "シーンごと"}にしました`) },
      { sep: true },
      { head: `エクスプレッションペダル${exp ? `(いま EXP${exp}・${wireToPct(p.exp_min) ?? "?"}%〜${wireToPct(p.exp_max) ?? "?"}%)` : ""}` },
      { label: exp === 1 ? "ペダル1 の範囲を変える…" : "エクスプレッションペダル1に割り当て…", on: exp === 1, onClick: () => expressionDialog(b, p, 1) },
      { label: exp === 2 ? "ペダル2 の範囲を変える…" : "エクスプレッションペダル2に割り当て…", on: exp === 2, onClick: () => expressionDialog(b, p, 2) },
      { label: "割り当てを外す", disabled: !exp,
        onClick: () => post("expression", { ...tgt, pedal: 0, min: p.exp_min ?? 0, max: p.exp_max ?? 1, expect: expExpect(p) },
          `${p.name} のペダルの割り当てを外しました`) },
      { sep: true },
      { info: "ペダルに割り当てたパラメーターはシーンで変わらなくなります(マニュアルより)。保存はしません。" },
    ]);
  }

  function expExpect(p) {
    const e = p.expression === 1 || p.expression === 2 ? p.expression : 0;
    return { pedal: e, min: e ? p.exp_min : null, max: e ? p.exp_max : null };
  }

  function expressionDialog(b, p, pedal) {
    const same = p.expression === pedal;
    const lo = el("input", { type: "number", min: "0", max: "100", step: "1", inputmode: "numeric",
      value: String(same ? wireToPct(p.exp_min) ?? 0 : 0), "aria-label": "かかと側(%)" });
    const hi = el("input", { type: "number", min: "0", max: "100", step: "1", inputmode: "numeric",
      value: String(same ? wireToPct(p.exp_max) ?? 100 : 100), "aria-label": "つま先側(%)" });
    const ok = el("button", { type: "button", class: "primary" }, same ? "変更" : "割り当てる");
    const cancel = el("button", { type: "button" }, "やめる");
    const dlg = el("dialog", { class: "ed-dlg" }, el("h3", {}, `${p.name} をペダル${pedal}に`),
      el("p", {}, `ペダルを動かしたときに ${p.name} が動く範囲を、つまみの位置の % で決めます(0% = 左いっぱい、100% = 右いっぱい)。`
        + "割り当てると、このパラメーターはシーンで変わらなくなります。保存はしません。取り消しで戻せます。"),
      el("div", { class: "ed-dlg-fields ex-expf" }, el("label", {}, "かかと ", lo, " %"), el("label", {}, "つま先 ", hi, " %")),
      el("div", { class: "row" }, cancel, ok));
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener("click", close);
    dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
    const go = () => {
      const min = pctToWire(lo.value), max = pctToWire(hi.value);
      if (min === null || max === null || lo.value === "" || hi.value === "") { toast("0〜100 の数を入れてください", true); return; }
      close();
      post("expression", { row: b.row, col: b.col, model_id: b.model_id, param: p.name, pedal, min, max, expect: expExpect(p) },
        `${p.name} をペダル${pedal}(${Math.round(min * 100)}%〜${Math.round(max * 100)}%)に割り当てました`);
    };
    ok.addEventListener("click", go);
    for (const input of [lo, hi]) input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    document.body.append(dlg);
    dlg.showModal();
    lo.select();
  }

  // ------------------------------------------------------------ tempo (status bar)

  const tempoBtn = () => document.getElementById("stTempo");
  const writable = () => !!(ED.view && ED.view.tempo_writable);
  const TEMPO_WHY = "このプリセットのテンポは画面から変えられません(カタログが BPM の範囲を示していないため)。本体で変えてください。";

  function sendTempo(bpm) {
    const v = ED.view || {};
    if (!writable()) { toast(TEMPO_WHY, true); return; }
    if (typeof v.tempo === "number" && Math.abs(v.tempo - bpm) < 0.05) { decorateStatus(); return; }
    post("tempo", { bpm, expect: typeof v.tempo === "number" ? v.tempo : null }, `テンポを ${bpm} BPM にしました`);
  }

  function tap() {
    if (!ED.view) return;
    if (!writable()) { toast(TEMPO_WHY, true); return; }
    X.taps = tapPush(X.taps, performance.now());
    const bpm = tapBpm(X.taps);
    X.tapShown = bpm;
    const btn = tempoBtn();
    if (btn) {
      btn.textContent = bpm ? `TAP ${Math.round(bpm)}BPM` : "TAP …";
      btn.classList.remove("ex-tapflash");
      void btn.offsetWidth;
      btn.classList.add("ex-tapflash");
    }
    const live = document.querySelector(".ex-tempo .ex-tapv");
    if (live) live.textContent = bpm ? `${bpm} BPM` : "もう一度…";
    if (X.tapTimer) clearTimeout(X.tapTimer);
    // The taps have settled when nothing comes for 2 s: send once, start over.
    X.tapTimer = setTimeout(() => {
      X.tapTimer = null;
      const final = tapBpm(X.taps);
      X.taps = [];
      X.tapShown = null;
      if (final) sendTempo(final); else decorateStatus();
    }, TAP_RESET_MS);
  }

  function openTempo() {
    const btn = tempoBtn();
    if (!btn || !ED.view) return;
    const v = ED.view;
    const ok = writable();
    const input = el("input", { type: "number", min: String(TEMPO_MIN), max: String(TEMPO_MAX), step: "0.1",
      inputmode: "decimal", value: typeof v.tempo === "number" ? String(v.tempo) : "", disabled: !ok, "aria-label": "BPM" });
    const set = el("button", { type: "button", class: "ex-tbtn", disabled: !ok }, "決定");
    const tapBtn = el("button", { type: "button", class: "ex-tap", disabled: !ok, title: "拍に合わせて数回押す(キーボードの T でも)" }, "TAP");
    const node = el("div", { class: "ex-tempo", "aria-label": "テンポ" },
      el("div", { class: "pop-h" }, "テンポ"),
      el("div", { class: "ex-trow" }, input, el("span", { class: "ex-unit" }, "BPM"), set),
      tapBtn, el("div", { class: "ex-tapv", "aria-live": "polite" }, ""),
      el("div", { class: "pop-info" }, ok ? `${TEMPO_MIN}〜${TEMPO_MAX} BPM。TAP は最後に押してから 2 秒で送ります。保存はしません。` : TEMPO_WHY));
    const go = () => {
      const bpm = parseBpm(input.value);
      if (bpm === null) { toast("BPM を数で入れてください", true); input.focus(); return; }
      closeMenu();
      sendTempo(bpm);
    };
    set.addEventListener("click", go);
    input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    tapBtn.addEventListener("pointerdown", e => { e.preventDefault(); tap(); });
    tapBtn.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); tap(); } });
    if (openPopover(btn, node) && ok) input.select();
  }

  // Called by editor.js renderStatus().
  function decorateStatus() {
    const btn = tempoBtn();
    if (btn) {
      const v = ED.view || {};
      if (X.tapShown) btn.textContent = `TAP ${Math.round(X.tapShown)}BPM`;
      else btn.textContent = typeof v.tempo === "number" ? `TEMPO ${Math.round(v.tempo)}BPM` : "TEMPO —";
      btn.title = ED.view && !writable() ? `${TEMPO_WHY}` : "クリックでテンポを変える(T キーでタップ)";
      btn.classList.toggle("ro", !!ED.view && !writable());
    }
    const gig = document.getElementById("stGig");
    if (gig) {
      gig.classList.toggle("on", X.gig);
      gig.setAttribute("aria-pressed", String(X.gig));
      gig.title = X.gigRefused ? `ギグビュー: ${X.gigRefused}` : "本体の画面をギグビューに切り替えます(音は変わりません)";
      gig.classList.toggle("ro", !!X.gigRefused);
    }
  }

  async function gigView() {
    if (X.gigRefused) { toast(X.gigRefused, true); return; }
    const shown = !X.gig;
    try {
      await edFetch("/api/editor/extra/gig_view", { shown });
      X.gig = shown;
      toast(shown ? "本体の画面をギグビューにしました(音は変わりません)" : "本体の画面をギグビューから戻しました");
    } catch (e) {
      if (e.status === 400) X.gigRefused = e.message || "本体が受け付けませんでした";
    }
    decorateStatus();
  }

  // ------------------------------------------------------------ wiring

  const tb = tempoBtn();
  if (tb) tb.addEventListener("click", openTempo);
  const gb = document.getElementById("stGig");
  if (gb) gb.addEventListener("click", gigView);

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && ED.exPanel && !openPop && !document.querySelector("dialog[open]")
      && !typingTarget(e.target) && document.getElementById("historyDrawer").hidden
      && !document.getElementById("sidebar").classList.contains("open")) {
      closePanel();
      return;
    }
    if ((e.key === "t" || e.key === "T") && !e.metaKey && !e.ctrlKey && !e.altKey && !e.repeat) {
      if (typingTarget(e.target) || document.querySelector("dialog[open]")) return;
      if (e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      tap();
    }
  });

  window.EdExtras = { ...Core, renderPanel, closePanel, openPanel, decorateGrid, decorateParam, portMenuItems,
    splitMenuItem, blockMenuItems, decorateScene, sceneMenuItems, decorateStatus, openStomps, openTempo, tap,
    busy: () => X.holding > 0 };
})();
