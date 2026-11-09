// エディター: grid view, parameter screen, knob/select/bypass writes, undo/redo, A/B.
// Pure maths lives in editor_core.js (EditorCore). Nothing here saves a preset.

const EC = window.EditorCore;
const PER_PAGE = 10;
const SVGNS = "http://www.w3.org/2000/svg";
const GESTURE_IDLE_MS = 500;   // wheel / arrow keys: gesture ends after this much quiet

const ED = {
  view: null,
  sel: null,          // {row, col, model_id}
  page: {},           // "row:col:model_id" -> page index
  gesture: null,      // the knob gesture in progress (one at a time)
  reloadWanted: false,
  loading: null,
};

// ---------------------------------------------------------------- HTTP

class EdError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}

// Like api() but keeps the HTTP status and JSON body (409 conflicts need both).
async function edFetch(path, body, { method = "POST", quiet = false } = {}) {
  const opts = { method };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers = { "Content-Type": "application/json" };
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (!quiet) toast(`通信できませんでした: ${e.message}`, true);
    throw new EdError(e.message, 0, null);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) {
    const message = (data && (data.error || data.detail)) || `${res.status} ${res.statusText}`;
    const conflict = res.status === 409 && data && data.conflict;
    if (!quiet) toast(conflict ? `${message}(本体の値が画面と違ったので、何も書き込まずに読み直します)` : message, true);
    throw new EdError(message, res.status, data);
  }
  return data;
}

// Every failed write: the page may be out of date, so read the grid again.
function writeFailed() {
  loadGrid();
}

// ---------------------------------------------------------------- helpers

function blockKey(b) { return `${b.row}:${b.col}:${b.model_id}`; }

function findBlock(sel) {
  if (!ED.view || !sel) return null;
  const r = ED.view.rows.find(x => x.row === sel.row);
  const b = r && r.cells[sel.col];
  return b && b.model_id === sel.model_id ? b : null;
}

function firstBlock() {
  if (!ED.view) return null;
  for (const r of ED.view.rows) for (const c of r.cells) if (c) return c;
  return null;
}

function svg(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  kids.forEach(k => n.append(k));
  return n;
}

function inertKnob(p, b) { return !!(p.inert || (b && b.responds === false)); }

function writeTarget(b, p) {
  return { row: b.row, col: b.col, model_id: b.model_id, param: p.name };
}

// Knob value field name: real units, or normalized 0..1 for placeholder knobs.
function valueField(p) { return p.placeholder ? "value" : "real"; }

function applyReadback(b, p, res) {
  const rb = res && res.param;
  if (!rb) return;
  if (p && "value" in rb && rb.value !== undefined) p.value = rb.value;
  if (p && "real" in rb && rb.real !== undefined && !p.placeholder) p.value = rb.real;
  if (p && "selected" in rb && rb.selected !== undefined) p.selected = rb.selected;
  if ("bypassed" in rb && rb.bypassed !== undefined) b.bypassed = rb.bypassed;
}

function afterWrite(res) {
  (res && res.warnings || []).forEach(w => toast(w, true));
  loadHistory();
}

// ---------------------------------------------------------------- icons

// Original line-art icons, 40x40, white strokes.
const ICONS = {
  amp: () => [
    svg("rect", { x: 5, y: 11, width: 30, height: 20, rx: 2.5 }),
    svg("path", { d: "M15 11V7.5h10V11" }),
    svg("path", { d: "M5 18h30" }),
    ...[11, 17, 23, 29].map(x => svg("circle", { cx: x, cy: 24.5, r: 1.6 })),
  ],
  od: () => [
    svg("rect", { x: 11, y: 5, width: 18, height: 30, rx: 3 }),
    svg("circle", { cx: 15.5, cy: 11, r: 1.8 }),
    svg("circle", { cx: 24.5, cy: 11, r: 1.8 }),
    svg("circle", { cx: 20, cy: 16.5, r: 2.2 }),
    svg("rect", { x: 15, y: 24, width: 10, height: 7, rx: 1.5 }),
  ],
  cab: () => [
    svg("rect", { x: 6, y: 6, width: 28, height: 28, rx: 2.5 }),
    svg("circle", { cx: 20, cy: 20, r: 9 }),
    svg("circle", { cx: 20, cy: 20, r: 3 }),
  ],
  dly: () => [
    svg("circle", { cx: 9, cy: 20, r: 4.2, fill: "currentColor" }),
    svg("circle", { cx: 19, cy: 20, r: 3.2, "fill-opacity": ".7", fill: "currentColor" }),
    svg("circle", { cx: 27.5, cy: 20, r: 2.3, "fill-opacity": ".45", fill: "currentColor" }),
    svg("circle", { cx: 34, cy: 20, r: 1.5, "fill-opacity": ".25", fill: "currentColor" }),
  ],
  rev: () => [12, 20, 28].map(y => svg("path", { d: `M5 ${y}q3.75-4.5 7.5 0t7.5 0t7.5 0t7.5 0` })),
  cmp: () => [
    ...[12, 20, 28].map(x => svg("path", { d: `M${x} 7v26` })),
    svg("rect", { x: 9, y: 22, width: 6, height: 4, rx: 1, fill: "#111" }),
    svg("rect", { x: 17, y: 12, width: 6, height: 4, rx: 1, fill: "#111" }),
    svg("rect", { x: 25, y: 18, width: 6, height: 4, rx: 1, fill: "#111" }),
  ],
  pit: () => [
    svg("path", { d: "M14 32V9M8 15l6-6 6 6" }),
    svg("path", { d: "M26 8v23M20 25l6 6 6-6" }),
  ],
  mod: () => [svg("path", { d: "M4 20C8 6 13 6 16 20S24 34 28 20 33 9 36 15" })],
  cap: () => [
    svg("circle", { cx: 20, cy: 20, r: 12 }),
    svg("circle", { cx: 20, cy: 20, r: 5.5 }),
    svg("path", { d: "M20 4v7M20 29v7M4 20h7M29 20h7" }),
  ],
  util: () => [
    svg("path", { d: "M16 5v7M24 5v7" }),
    svg("path", { d: "M11 12h18v6a9 9 0 0 1-18 0z" }),
    svg("path", { d: "M20 27v8" }),
  ],
};

function icon(color) {
  const make = ICONS[color] || ICONS.util;
  return svg("svg", { viewBox: "0 0 40 40", fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" }, ...make());
}

// ---------------------------------------------------------------- grid

async function loadGrid() {
  if (ED.gesture) { ED.reloadWanted = true; return; }   // redraw after the knob is let go
  if (ED.loading) return ED.loading;
  ED.loading = (async () => {
    try {
      ED.view = await edFetch("/api/editor/grid", undefined, { method: "GET" });
    } catch (e) {
      return;
    } finally {
      ED.loading = null;
    }
    if (!findBlock(ED.sel)) {
      const b = firstBlock();
      ED.sel = b ? { row: b.row, col: b.col, model_id: b.model_id } : null;
    }
    renderAll();
  })();
  return ED.loading;
}

function renderAll() {
  const v = ED.view;
  document.getElementById("scene").textContent = v.scene ? `シーン ${v.scene}` : "";
  const note = document.getElementById("edNote");
  note.textContent = v.note && !v.note.startsWith("保存していません") ? v.note : "";
  document.getElementById("edRepo").textContent = v.repo && v.repo.note
    ? `表示属性: ${v.repo.note}` : "";
  renderGrid();
  renderParams();
}

function renderGrid() {
  const box = document.getElementById("edGrid");
  const rows = [];
  for (const r of ED.view.rows) {
    const used = r.cells.some(Boolean);
    if (!used && !r.is_input) continue;
    const dead = r.reaches_output === false;
    const outTitle = dead ? "Out 1/2 に届かない" : "";
    rows.push(el("div", { class: "ed-row", role: "group", "aria-label": `行${r.row + 1}` },
      el("div", { class: "ed-rowlabel" },
        el("span", {}, `行${r.row + 1} ${r.in_port}`),
        el("span", { class: dead ? "danger" : "", title: outTitle }, `→ ${r.out_port}${dead ? "(届かない)" : ""}`)),
      el("div", { class: "ed-port in", title: `行${r.row + 1} 入力 ${r.in_port}` }, el("span", {}, r.in_port)),
      el("div", { class: "ed-cells" }, r.cells.map((c, col) => cellNode(c, r.row, col))),
      el("div", { class: `ed-port out ${dead ? "dead" : ""}`, title: `出力 ${r.out_port} ${outTitle}` },
        el("span", {}, r.out_port))));
  }
  box.replaceChildren(rows.length ? rows : el("p", { class: "ed-empty" }, "ブロックがありません。"));
}

function cellNode(c, row, col) {
  if (!c) return el("div", { class: "ed-cell empty", title: `行${row + 1} 列${col + 1}(空き)` }, el("span", { class: "ed-dot" }));
  const selected = ED.sel && ED.sel.row === row && ED.sel.col === col && ED.sel.model_id === c.model_id;
  const tip = [`${c.title || c.model}`, c.based_on, c.bypassed ? "(バイパス中)" : null,
    c.responds === false ? "パソコンから変えても音は変わらない" : null, ...(c.flags || [])].filter(Boolean).join("\n");
  const blk = el("div", {
    class: `ed-blk ${selected ? "sel" : ""} ${c.bypassed ? "bypassed" : ""}`,
    "data-c": c.color || "util", role: "button", tabindex: "0", "aria-pressed": String(!!selected),
    "aria-label": `行${row + 1} 列${col + 1} ${c.title || c.model}${c.bypassed ? " バイパス中" : ""}`, title: tip,
  }, icon(c.color), c.responds === false ? el("span", { class: "ed-mark", "aria-hidden": "true" }, "!") : null);
  const choose = () => selectBlock(c);
  blk.addEventListener("click", choose);
  blk.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); }
  });
  return el("div", { class: `ed-cell ${selected ? "selcell" : ""}` }, blk,
    el("span", { class: "ed-name" }, c.title || c.model));
}

// Selecting only changes the screen; nothing is written to the unit.
function selectBlock(c) {
  if (ED.gesture) endGesture(ED.gesture);
  ED.sel = { row: c.row, col: c.col, model_id: c.model_id };
  renderGrid();
  renderParams();
  const node = document.querySelector(".ed-blk.sel");
  if (node) node.focus({ preventScroll: true });
}

// ---------------------------------------------------------------- parameter screen

function renderParams() {
  const host = document.getElementById("edParams");
  const b = findBlock(ED.sel);
  if (!b) { host.replaceChildren(); return; }
  const key = blockKey(b);
  const params = b.params || [];
  const pages = Math.ceil(params.length / PER_PAGE);
  let page = Math.min(ED.page[key] || 0, Math.max(0, pages - 1));
  ED.page[key] = page;

  const byp = el("button", {
    type: "button", class: "cc-byp", "aria-pressed": String(!!b.bypassed),
    disabled: b.bypassed === null || b.bypassed === undefined,
    title: b.bypassed === null || b.bypassed === undefined ? "バイパスの状態が分かりません" : "クリックで切り替え(1 回で取り消しの 1 単位)",
  }, b.bypassed ? "バイパス中" : b.bypassed === false ? "オン" : "バイパス ?");
  byp.addEventListener("click", () => toggleBypass(b));

  const tabs = pages > 1 ? el("div", { class: "cc-pages", role: "group", "aria-label": "ページ" },
    Array.from({ length: pages }, (_, i) => {
      const t = el("button", { type: "button", "aria-pressed": String(i === page), "aria-label": `ページ ${i + 1}` }, String(i + 1));
      t.addEventListener("click", () => { ED.page[key] = i; renderParams(); });
      return t;
    })) : null;

  const banners = [];
  if (b.responds === false) {
    banners.push(el("div", { class: "cc-banner" },
      "このブロックはパソコンから変えても音が変わりません(読み戻せるが音は変わらない)"));
  }
  for (const f of b.flags || []) banners.push(el("div", { class: "cc-banner" }, f));
  if (b.based_on) banners.push(el("div", { class: "cc-banner based" }, b.based_on));

  const list = params.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  const cells = list.map(p => paramCell(b, p));
  const pad = (5 - (list.length % 5)) % 5;
  for (let k = 0; k < pad; k++) cells.push(el("div", { class: "cc-cell blank", "aria-hidden": "true" }));

  host.replaceChildren(el("div", { class: "cc", "data-c": b.color || "util" },
    el("div", { class: "cc-head" },
      el("span", { class: "cc-dots", "aria-hidden": "true" }, "⋮"),
      el("span", { class: "cc-title" }, el("span", { class: "cc-cat" }, b.category_label || b.category || ""),
        el("b", {}, b.title || b.model)),
      byp, tabs),
    banners,
    params.length ? el("div", { class: "cc-grid" }, cells)
      : el("div", { class: "cc-none" }, "表示できるパラメーターはありません。")));
}

function paramCell(b, p) {
  if (p.kind === "knob") return knobCell(b, p);
  if (p.kind === "select") return selectCell(b, p);
  return el("div", { class: "cc-cell text off", title: p.why_not || "" },
    el("span", { class: "cc-name" }, p.name),
    el("div", { class: "cc-body" }, el("span", { class: "cc-text", "aria-disabled": "true" }, p.text ?? "—")));
}

// ---- knob

function knobSvg(p, v) {
  const a = EC.angle(p, v);
  const t = (a - 90) * Math.PI / 180;
  const x = 50 + 27 * Math.cos(t), y = 50 + 27 * Math.sin(t);
  return svg("svg", { class: "cc-knob", viewBox: "0 0 100 100", "aria-hidden": "true" },
    svg("path", { d: EC.arcPath(44, -135, 135), fill: "none", stroke: "#050505", "stroke-width": "8" }),
    a > -134.5 ? svg("path", { d: EC.arcPath(44, -135, a), fill: "none", stroke: "var(--c)", "stroke-width": "8" }) : "",
    svg("circle", { cx: 50, cy: 50, r: 34, fill: "#151515", stroke: "#050505", "stroke-width": "4" }),
    svg("line", { x1: 50, y1: 50, x2: x.toFixed(2), y2: y.toFixed(2), stroke: "#fff", "stroke-width": "4.5", "stroke-linecap": "round" }));
}

function valueNode(p, v) {
  const f = EC.formatValue(p, v);
  return el("span", { class: "cc-val" }, f.text, f.unit ? el("i", {}, f.unit) : null);
}

function drawKnob(cell, p, v) {
  const f = EC.formatValue(p, v);
  cell.querySelector(".cc-val").replaceWith(valueNode(p, v));
  cell.querySelector(".cc-knob").replaceWith(knobSvg(p, v));
  if (cell.getAttribute("role") === "slider") {
    cell.setAttribute("aria-valuenow", String(v));
    cell.setAttribute("aria-valuetext", f.text + (f.unit ? ` ${f.unit}` : ""));
  }
}

function knobCell(b, p) {
  const live = p.writable && typeof p.value === "number";
  const inert = inertKnob(p, b);
  const r = EC.range(p);
  const attrs = { class: `cc-cell knob ${live ? "live" : "off"} ${inert ? "has-tag" : ""}` };
  if (live) {
    const f = EC.formatValue(p, p.value);
    Object.assign(attrs, {
      tabindex: "0", role: "slider", "aria-label": p.name,
      "aria-valuemin": String(r.min), "aria-valuemax": String(r.max), "aria-valuenow": String(p.value),
      "aria-valuetext": f.text + (f.unit ? ` ${f.unit}` : ""),
      title: "上下にドラッグ(Shift で細かく)/ホイール/矢印キー/ダブルクリックで初期値",
    });
  } else {
    attrs.title = p.why_not || "変更できません";
    attrs["aria-disabled"] = "true";
  }
  const cell = el("div", attrs,
    el("span", { class: "cc-name" }, p.name),
    inert ? el("span", { class: "cc-tag" }, "音は変わらない") : null,
    el("div", { class: "cc-body" }, valueNode(p, p.value), knobSvg(p, p.value)));
  if (live) wireKnob(cell, b, p);
  return cell;
}

function wireKnob(cell, b, p) {
  let drag = null;
  cell.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    cell.focus({ preventScroll: true });
    try { cell.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
    drag = { id: e.pointerId, y: e.clientY, prop: EC.proportion(p, currentValue(b, p)), fine: e.shiftKey };
  });
  cell.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    if (e.shiftKey !== drag.fine) {   // re-anchor when Shift changes mid-drag
      drag = { id: drag.id, y: e.clientY, prop: EC.proportion(p, currentValue(b, p)), fine: e.shiftKey };
      return;
    }
    const v = EC.dragValue(p, drag.prop, e.clientY - drag.y, drag.fine);
    if (v === currentValue(b, p)) return;
    gestureMove(cell, b, p, v, false);
  });
  const release = e => {
    if (!drag || e.pointerId !== drag.id) return;
    drag = null;
    if (ED.gesture && ED.gesture.cell === cell) endGesture(ED.gesture);
  };
  cell.addEventListener("pointerup", release);
  cell.addEventListener("pointercancel", release);
  cell.addEventListener("lostpointercapture", release);
  cell.addEventListener("wheel", e => {
    e.preventDefault();
    if (drag) return;
    const dy = e.deltaY || -e.deltaX;
    if (!dy) return;
    gestureMove(cell, b, p, EC.stepValue(p, currentValue(b, p), dy < 0 ? +1 : -1, e.shiftKey), true);
  }, { passive: false });
  cell.addEventListener("keydown", e => {
    let dir = 0;
    if (e.key === "ArrowUp" || e.key === "ArrowRight") dir = +1;
    else if (e.key === "ArrowDown" || e.key === "ArrowLeft") dir = -1;
    if (!dir || e.metaKey || e.ctrlKey || e.altKey) return;
    e.preventDefault();
    gestureMove(cell, b, p, EC.stepValue(p, currentValue(b, p), dir, e.shiftKey), true);
  });
  cell.addEventListener("dblclick", e => {
    e.preventDefault();
    resetKnob(b, p);
  });
}

function currentValue(b, p) {
  const g = ED.gesture;
  return g && g.p === p ? g.final : p.value;
}

// ---- knob gestures (begin / move* / end = one undo unit)

function startGesture(cell, b, p) {
  const g = {
    cell, b, p, startValue: p.value, final: p.value, id: null, failed: false, ending: null, idleTimer: null,
  };
  g.begin = edFetch("/api/editor/begin", { ...writeTarget(b, p), expect: p.value })
    .then(res => { g.id = res.gesture; return g.id; })
    .catch(err => { g.failed = true; throw err; });
  g.begin.catch(() => {});   // handled where awaited
  g.throttle = new EC.Throttle(async (value, seq) => {
    const id = await g.begin;
    await edFetch("/api/editor/move", { gesture: id, seq, [valueField(p)]: value }, { quiet: true });
  }, 100);
  ED.gesture = g;
  cell.classList.add("active");
  return g;
}

function gestureMove(cell, b, p, v, idleEnds) {
  let g = ED.gesture;
  if (g && g.p !== p) { endGesture(g); g = null; }
  if (g && g.ending) return;                 // the previous one is still finishing
  if (!g) {
    if (v === p.value) return;
    g = startGesture(cell, b, p);
  }
  if (g.failed) { endGesture(g); return; }
  g.final = v;
  g.cell = cell;
  drawKnob(cell, p, v);                       // local feedback at once
  g.throttle.push(v);
  if (g.idleTimer) clearTimeout(g.idleTimer);
  if (idleEnds) g.idleTimer = setTimeout(() => endGesture(g), GESTURE_IDLE_MS);
}

function endGesture(g) {
  if (g.ending) return g.ending;
  if (g.idleTimer) { clearTimeout(g.idleTimer); g.idleTimer = null; }
  g.ending = (async () => {
    let ok = false;
    try {
      const id = await g.begin;
      await g.throttle.cancel();             // end carries the final value itself
      const res = await edFetch("/api/editor/end", { gesture: id, [valueField(g.p)]: g.final });
      g.p.value = g.final;
      applyReadback(g.b, g.p, res);
      ok = true;
      afterWrite(res);
    } catch (e) {
      await g.throttle.cancel().catch(() => {});
    } finally {
      if (ED.gesture === g) ED.gesture = null;
      g.cell.classList.remove("active");
    }
    if (!ok || ED.reloadWanted) { ED.reloadWanted = false; loadGrid(); return; }
    if (g.cell.isConnected) drawKnob(g.cell, g.p, g.p.value);
  })();
  return g.ending;
}

// ---- one-shot writes

async function oneShot(body) {
  try {
    return await edFetch("/api/editor/set", body);
  } catch (e) {
    writeFailed();
    return null;
  }
}

async function resetKnob(b, p) {
  if (ED.gesture) await endGesture(ED.gesture);
  if (p.default === null || p.default === undefined) { toast(`${p.name} の初期値が分からないので戻せません`, true); return; }
  if (p.default === p.value) return;
  const res = await oneShot({ ...writeTarget(b, p), [valueField(p)]: p.default, expect: p.value });
  if (!res) return;
  p.value = p.default;
  applyReadback(b, p, res);
  afterWrite(res);
  renderParams();
}

async function chooseOption(b, p, idx) {
  if (ED.gesture) await endGesture(ED.gesture);
  if (idx === p.selected || !p.options) return;
  const current = p.selected === null || p.selected === undefined ? null : p.labels[p.selected];
  const res = await oneShot({ ...writeTarget(b, p), option: p.options[idx], expect: current });
  if (!res) return;
  p.selected = idx;
  applyReadback(b, p, res);
  afterWrite(res);
  renderParams();
}

async function toggleBypass(b) {
  if (ED.gesture) await endGesture(ED.gesture);
  if (b.bypassed === null || b.bypassed === undefined) return;
  const res = await oneShot({ row: b.row, col: b.col, model_id: b.model_id, bypassed: !b.bypassed, expect: b.bypassed });
  if (!res) return;
  b.bypassed = !b.bypassed;
  applyReadback(b, null, res);
  afterWrite(res);
  renderGrid();
  renderParams();
}

// ---- select

function selectCell(b, p) {
  const labels = p.labels || [];
  const live = p.writable && Array.isArray(p.options) && p.options.length === labels.length;
  const why = live ? "" : (p.why_not || "変更できません");
  const name = el("span", { class: "cc-name" }, p.name);
  let control;
  if (EC.selectStyle(labels) === "dropdown") {
    const sel = el("select", { class: "cc-sel", "aria-label": p.name, disabled: !live, title: why || null },
      p.selected === null || p.selected === undefined ? el("option", { value: "", disabled: true, selected: true }, "—") : null,
      labels.map((l, i) => el("option", { value: String(i), selected: i === p.selected }, l)));
    sel.addEventListener("change", () => chooseOption(b, p, Number(sel.value)));
    control = el("div", { class: "cc-selwrap" }, sel);
  } else {
    control = el("div", { class: "cc-tog", role: "group", "aria-label": p.name }, labels.map((l, i) => {
      const btn = el("button", { type: "button", class: i === p.selected ? "on" : "", disabled: !live,
        "aria-pressed": String(i === p.selected), title: why || null }, l);
      btn.addEventListener("click", () => chooseOption(b, p, i));
      return btn;
    }));
  }
  return el("div", { class: `cc-cell select ${live ? "" : "off"}`, title: why || null },
    name, el("div", { class: "cc-body" }, control));
}

// ---------------------------------------------------------------- history, undo, A/B, save

async function loadHistory() {
  let rows;
  try { rows = await api("/api/panel/history"); } catch { return; }
  const engines = { rules: "ルール", claude: "Claude", editor: "エディター" };
  document.getElementById("history").replaceChildren(rows.length ? table(rows.slice().reverse(), [
    ["at", "時刻"], ["instruction", "指示"], ["engine", "変換", v => engines[v] || v || ""],
    ["summary", "内容", v => (v || []).join(" / ")],
    ["undone", "状態", v => v ? "取り消し済み" : "適用中"],
  ]) : el("p", { class: "muted small" }, "(まだありません)"));
}

async function loadAB() {
  let ab;
  try { ab = await api("/api/panel/ab"); } catch { return; }
  for (const slot of ["A", "B"]) {
    const btn = document.getElementById(`ab${slot}`);
    btn.classList.toggle("on", ab.active === slot);
    btn.disabled = !(ab.slots && ab.slots[slot]);
    btn.title = ab.slots && ab.slots[slot] ? `${ab.slots[slot].label}(${ab.slots[slot].at})` : "まだ記録していません";
  }
}

async function undoRedo(which) {
  if (ED.gesture) await endGesture(ED.gesture);
  try {
    const r = await api(`/api/panel/${which}`, { method: "POST" });
    toast(`${which === "undo" ? "取り消しました" : "やり直しました"}: ${r.instruction || ""}`);
    (r.notes || []).forEach(n => toast(n));
  } catch { /* toast shown */ }
  await Promise.all([loadGrid(), loadHistory(), loadAB()]);
}

document.getElementById("btnReload").addEventListener("click", () => loadGrid());
document.getElementById("btnUndo").addEventListener("click", () => undoRedo("undo"));
document.getElementById("btnRedo").addEventListener("click", () => undoRedo("redo"));

for (const slot of ["A", "B"]) {
  document.getElementById(`cap${slot}`).addEventListener("click", async () => {
    try {
      await api("/api/panel/ab/capture", { method: "POST", body: { slot } });
      toast(`いまの状態を ${slot} に記録しました`);
    } catch { /* shown */ }
    loadAB();
  });
  document.getElementById(`ab${slot}`).addEventListener("click", async () => {
    if (ED.gesture) await endGesture(ED.gesture);
    try {
      const r = await api("/api/panel/ab/switch", { method: "POST", body: { slot } });
      toast(`${slot} にしました(${r.applied} 件の操作)`);
      (r.notes || []).forEach(n => toast(n));
    } catch { /* shown */ }
    loadGrid(); loadHistory(); loadAB();
  });
}

document.getElementById("btnSave").addEventListener("click", async () => {
  const name = document.getElementById("saveName").value.trim();
  if (!name) { toast("名前を入れてください", true); return; }
  try {
    const plan = await api("/api/panel/save", { method: "POST", body: { name } });
    showPlan(plan, () => toast("保存しました"));
  } catch { /* shown */ }
});

document.addEventListener("keydown", e => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === "z" || e.key === "Z")) {
    e.preventDefault();
    undoRedo(e.shiftKey ? "redo" : "undo");
  }
});

// Changes made in another tab (言葉で操作, the unit's screen) show up when we come back.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { loadGrid(); loadHistory(); loadAB(); }
});

// Leaving mid-gesture: close it so the server records one undo unit.
window.addEventListener("pagehide", () => {
  const g = ED.gesture;
  if (!g || !g.id || !navigator.sendBeacon) return;
  const body = { gesture: g.id, [valueField(g.p)]: g.final };
  navigator.sendBeacon("/api/editor/end", new Blob([JSON.stringify(body)], { type: "application/json" }));
});

// Keep the sticky notice right under the (sticky, wrapping) header.
function trackHeader() {
  const head = document.querySelector("header.top");
  if (!head) return;
  const set = () => document.documentElement.style.setProperty("--ed-head", `${head.offsetHeight}px`);
  set();
  if (window.ResizeObserver) new ResizeObserver(set).observe(head);
}

const headerReady = renderHeader("editor");
trackHeader();
headerReady.then(() => { loadGrid(); loadHistory(); loadAB(); });
