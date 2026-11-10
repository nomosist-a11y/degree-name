// エディター: app shell (top bar, device list, grid, parameter panel, status bar),
// knob/select/bypass writes, block replace, undo/redo, A/B, history drawer, sandbox save.
// Pure maths lives in editor_core.js (EditorCore). Nothing here saves a preset.

const EC = window.EditorCore;
const PER_PAGE = 10;
const SVGNS = "http://www.w3.org/2000/svg";
const GESTURE_IDLE_MS = 500;   // wheel / arrow keys: gesture ends after this much quiet
const NCOLS = 8;
const SB_DRAWER_MQ = window.matchMedia("(max-width: 899px)");

const ED = {
  view: null,
  sel: null,          // {row, col, model_id}  (model_id null = empty cell)
  page: {},           // "row:col:model_id" -> page index
  gesture: null,      // the knob gesture in progress (one at a time)
  lastEnd: Promise.resolve(),   // the previous gesture's /end (a new /begin waits for it)
  reloadWanted: false,
  loading: null,
  catalog: null,      // /api/editor/catalog (loaded once)
  catIndex: new Map(),// model id -> {group, model}
  sbGroup: null,      // group key shown in the sidebar list (null = category list)
  sbBass: false,
  sbQuery: "",
  ab: null,
  busy: false,        // a replace is running
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
    if (!quiet) toast(conflictText(res.status, data, message), true);
    throw new EdError(message, res.status, data);
  }
  return data;
}

function conflictText(status, data, message) {
  return status === 409 && data && data.conflict
    ? `${message}(本体の値が画面と違ったので、何も書き込まずに読み直します)` : message;
}

// Every failed write: the page may be out of date, so read the grid again.
function writeFailed() {
  loadGrid();
}

// ---------------------------------------------------------------- helpers

function blockKey(b) { return `${b.row}:${b.col}:${b.model_id}`; }

function cellAt(row, col) {
  if (!ED.view) return null;
  const r = ED.view.rows.find(x => x.row === row);
  return (r && r.cells[col]) || null;
}

function findBlock(sel) {
  if (!sel || sel.model_id === null || sel.model_id === undefined) return null;
  const b = cellAt(sel.row, sel.col);
  return b && b.model_id === sel.model_id ? b : null;
}

function selIsEmptyCell() {
  return !!(ED.sel && (ED.sel.model_id === null || ED.sel.model_id === undefined) && !cellAt(ED.sel.row, ED.sel.col));
}

function firstBlock() {
  if (!ED.view) return null;
  for (const r of ED.view.rows) for (const c of r.cells) if (c) return c;
  return null;
}

function svg(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  kids.forEach(k => { if (k) n.append(k); });
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

function typingTarget(t) {
  return !!(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable));
}

// ---------------------------------------------------------------- icons

// Original line-art icons, 40x40, white strokes; one per sidebar group key.
function capIcon() {
  const kids = [];
  for (let i = 0; i < 9; i++) {
    const y = 8.5 + i * 2.875;
    const dy = y - 20, w = Math.sqrt(Math.max(0, 132 - dy * dy));
    if (w < 1) continue;
    const right = svg("path", { d: `M${(20 - w * 0.15).toFixed(2)} ${y.toFixed(2)}H${(20 + w).toFixed(2)}`, "stroke-width": "1.6" });
    const left = svg("path", { d: `M${(20 - w).toFixed(2)} ${y.toFixed(2)}H${(20 - w * 0.45).toFixed(2)}`,
      "stroke-width": "1.6", "stroke-dasharray": i % 2 ? "1.6 2" : "" });
    kids.push(left, right);
  }
  return kids;
}
const ICONS = {
  plugins: () => [
    svg("path", { d: "M15 6v7M25 6v7" }),
    svg("path", { d: "M10.5 13h19v5.5a9.5 9.5 0 0 1-19 0z" }),
    svg("path", { d: "M20 28v7" }),
  ],
  amp: () => [
    svg("rect", { x: 4, y: 12, width: 32, height: 17, rx: 2.5 }),
    svg("path", { d: "M8 17.5h24" }),
    ...[10, 14.5, 19].map(x => svg("circle", { cx: x, cy: 23.5, r: 1.1, fill: "currentColor", stroke: "none" })),
  ],
  capture: capIcon,
  cab: () => [
    svg("rect", { x: 6, y: 6, width: 28, height: 28, rx: 3 }),
    svg("circle", { cx: 20, cy: 20, r: 8.5 }),
    svg("circle", { cx: 20, cy: 20, r: 1.8, fill: "currentColor", stroke: "none" }),
    ...[[10.5, 10.5], [29.5, 10.5], [10.5, 29.5], [29.5, 29.5]].map(([x, y]) =>
      svg("circle", { cx: x, cy: y, r: 0.9, fill: "currentColor", stroke: "none" })),
  ],
  overdrive: () => [
    svg("rect", { x: 11, y: 5, width: 18, height: 30, rx: 3 }),
    svg("circle", { cx: 15.8, cy: 10.8, r: 1.7 }),
    svg("circle", { cx: 24.2, cy: 10.8, r: 1.7 }),
    svg("path", { d: "M14.5 17h11" }),
    svg("circle", { cx: 20, cy: 26, r: 2.8 }),
  ],
  delay: () => [svg("path", { d: "M4 30 10 8l6 22 5-15 5 15 4-9 4 9 3-4" })],
  reverb: () => [
    svg("circle", { cx: 9, cy: 20, r: 1.8, fill: "currentColor", stroke: "none" }),
    ...[7, 14, 21].map(r => svg("path", {
      d: `M${(9 + r * 0.707).toFixed(2)} ${(20 - r * 0.707).toFixed(2)}A${r} ${r} 0 0 1 ${(9 + r * 0.707).toFixed(2)} ${(20 + r * 0.707).toFixed(2)}` })),
  ],
  compressor: () => [
    svg("path", { d: "M20 4v7M16.5 7.5 20 11l3.5-3.5" }),
    svg("path", { d: "M20 36v-7M16.5 32.5 20 29l3.5 3.5" }),
    svg("path", { d: "M5 20c2.5-4 5-4 7.5 0s5 4 7.5 0 5-4 7.5 0 5 4 7.5 0" }),
  ],
  eq: () => [
    ...[12, 20, 28].map(x => svg("path", { d: `M${x} 7v26` })),
    svg("rect", { x: 9, y: 22, width: 6, height: 4, rx: 1, fill: "#111" }),
    svg("rect", { x: 17, y: 12, width: 6, height: 4, rx: 1, fill: "#111" }),
    svg("rect", { x: 25, y: 18, width: 6, height: 4, rx: 1, fill: "#111" }),
  ],
  filter: () => [svg("path", { d: "M4 30c8 0 10-19 16-19s8 19 16 19" }), svg("path", { d: "M4 34h32", "stroke-width": "1.2" })],
  wah: () => [
    svg("path", { d: "M6 32h28" }),
    svg("path", { d: "M8 28.5 33 17.5l1.2 3-24 11z" }),
    svg("circle", { cx: 12, cy: 32, r: 1.4, fill: "currentColor", stroke: "none" }),
  ],
  pitch: () => [
    svg("path", { d: "M14 32V9M8 15l6-6 6 6" }),
    svg("path", { d: "M26 8v23M20 25l6 6 6-6" }),
  ],
  modulation: () => [svg("path", { d: "M4 20C8 6 13 6 16 20S24 34 28 20 33 9 36 15" })],
  morph: () => [svg("circle", { cx: 15, cy: 20, r: 9 }), svg("circle", { cx: 25, cy: 20, r: 9 })],
  synth: () => [
    svg("rect", { x: 5, y: 9, width: 30, height: 22, rx: 2 }),
    ...[12.5, 20, 27.5].map(x => svg("path", { d: `M${x} 20v11` })),
    ...[10, 17.5, 25, 30].slice(0, 3).map(x => svg("rect", { x: x, y: 9, width: 5, height: 11, fill: "currentColor", stroke: "none" })),
  ],
  ir: () => [svg("path", { d: "M4 26h9l2-18 2.5 26 2-14 2 7 2-3h12.5" })],
  utility: () => [svg("path", { d: "M5 27h8V13h14v14h8" })],
  fxloop: () => [
    svg("path", { d: "M14 27H9a4 4 0 0 1-4-4v-8a4 4 0 0 1 4-4h22a4 4 0 0 1 4 4v8a4 4 0 0 1-4 4H20" }),
    svg("path", { d: "M24 23l-4 4 4 4" }),
  ],
  looper: () => [svg("path", { d: "M30.5 14.5A11 11 0 1 0 31 24" }), svg("path", { d: "M31.5 7.5v7h-7" })],
};

function icon(key) {
  const make = ICONS[key] || ICONS.utility;
  return svg("svg", { viewBox: "0 0 40 40", fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" }, ...make());
}

// Sidebar group of a block: by model id in the catalog (plugins!), else by category.
function groupKeyOf(b) {
  const hit = ED.catIndex.get(b.model_id);
  if (hit) return hit.group.key;
  if (b.color === "cap") return "capture";
  return EC.groupKeyForCategory(b.category);
}

function groupByKey(key) {
  return ED.catalog ? ED.catalog.groups.find(g => g.key === key) || null : null;
}

// ---------------------------------------------------------------- grid

async function loadGrid() {
  if (ED.gesture) { ED.reloadWanted = true; return; }   // redraw after the knob is let go
  if (window.EdLayout && EdLayout.dragging()) { ED.reloadWanted = true; return; }   // a block is in the air
  if (ED.loading) return ED.loading;
  ED.loading = (async () => {
    try {
      ED.view = await edFetch("/api/editor/grid", undefined, { method: "GET" });
      if (QC.status && QC.status.mode !== "mock" && !QC.status.connected) {
        // The first grid read connected the unit: show it in the badge.
        api("/api/status").then(st => { QC.status = st; showModeBadge(st); }).catch(() => {});
      }
    } catch (e) {
      return;
    } finally {
      ED.loading = null;
    }
    if (ED.gesture) { ED.reloadWanted = true; return; }   // a drag began while reading
    if (ED.sel && ED.sel.model_id !== null && ED.sel.model_id !== undefined && !findBlock(ED.sel)) {
      // the block changed under us (another tab, the unit): keep the cell selected
      const now = cellAt(ED.sel.row, ED.sel.col);
      ED.sel = { row: ED.sel.row, col: ED.sel.col, model_id: now ? now.model_id : null };
      if (now) ED.sbGroup = groupKeyOf(now);
    }
    if (ED.sel === null && !ED.everSelected) {
      const b = firstBlock();
      if (b) { ED.sel = { row: b.row, col: b.col, model_id: b.model_id }; ED.sbGroup = groupKeyOf(b); syncBass(b); }
      ED.everSelected = true;
    }
    renderAll();
  })();
  return ED.loading;
}

function renderAll() {
  renderPresetBar();
  renderStatus();
  renderGrid();
  renderParams();
  renderSidebar();
}

function renderPresetBar() {
  const v = ED.view || {};
  const box = document.getElementById("presetName");
  const p = v.preset || {};
  const name = p.name || "(名前が読めません)";
  box.classList.toggle("dirty", p.dirty === true);
  box.replaceChildren(el("span", {}, name + (p.dirty === true ? "*" : "")));
  box.title = (p.dirty === true ? `${name}(本体に保存前の編集があります)` : p.dirty === false ? name : `${name}(編集の有無は不明)`)
    + "\nクリックでプリセットの一覧";
  const labels = v.scene_labels || [];
  const scenes = "ABCDEFGH".split("");
  const btn = (s, i) => EdLayout.sceneButton(s, i, labels, v.scene);
  document.getElementById("scenes").replaceChildren(
    el("div", { class: "pb-sgrp" }, scenes.slice(0, 4).map((s, i) => btn(s, i))),
    el("div", { class: "pb-sgrp" }, scenes.slice(4).map((s, i) => btn(s, i + 4))));
}

function renderStatus() {
  const v = ED.view || {};
  document.getElementById("stTempo").textContent =
    typeof v.tempo === "number" ? `TEMPO ${Math.round(v.tempo)}BPM` : "TEMPO —";
  document.getElementById("stScene").textContent = v.scene ? `シーン ${v.scene}` : "シーン —";
  const note = document.getElementById("edNote");
  note.textContent = v.note && !v.note.startsWith("保存していません") ? v.note : "";
  const app = document.getElementById("stApp");
  app.textContent = `qcsuite v${(QC.status && QC.status.version) || "?"}`;
  app.title = v.repo && v.repo.note ? `表示属性: ${v.repo.note}` : "";
}

function portNode(port, side, dead, rowNo) {
  const label = EC.portLabel(port);
  const where = side === "in" ? "入力" : "出力";
  if (!label) {
    return el("div", { class: `ed-port ${side} add`, "data-row": rowNo - 1, "data-side": side, title: `行${rowNo} ${where}: なし` },
      svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: "M12 4v16M4 12h16" })));
  }
  const words = label.split(" ");
  const first = words.shift();
  return el("div", { class: `ed-port ${side} ${dead ? "dead" : ""}`, "data-row": rowNo - 1, "data-side": side,
    title: `行${rowNo} ${where}: ${port}${dead ? "(Out 1/2 に届かない)" : ""}` },
    el("span", {}, first), words.length ? el("span", {}, words.join(" ")) : null);
}

function renderGrid() {
  const box = document.getElementById("edGrid");
  if (!ED.view) return;
  const rows = ED.view.rows.map(r => {
    const used = r.cells.some(Boolean);
    const live = used || (r.in_port && r.in_port !== "EMPTY");
    const dead = r.reaches_output === false && live;
    return el("div", { class: `ed-row ${live ? "live" : "idle"}`, role: "group", "aria-label": `行${r.row + 1}` },
      portNode(r.in_port, "in", false, r.row + 1),
      el("div", { class: "ed-cells" }, r.cells.map((c, col) => cellNode(c, r.row, col))),
      portNode(r.out_port, "out", dead, r.row + 1));
  });
  box.replaceChildren(rows.length ? rows : el("p", { class: "ed-empty" }, "グリッドを読めませんでした。"));
  if (rows.length) EdLayout.decorate(box);
}

function cellNode(c, row, col) {
  const selected = !!(ED.sel && ED.sel.row === row && ED.sel.col === col);
  const where = `行${row + 1} 列${col + 1}`;
  if (!c) {
    const cell = el("div", { class: `ed-cell empty ${selected ? "selcell" : ""}`, role: "button",
      tabindex: selected ? "0" : "-1", "aria-pressed": String(selected), "data-row": row, "data-col": col,
      title: `${where}(空き)。左の一覧からドラッグ(または選んでダブルクリック)で置けます`, "aria-label": `${where} 空き` });
    cell.addEventListener("click", () => selectCell(row, col));
    cell.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectCell(row, col); } });
    return cell;
  }
  const tip = [`${where} ${c.title || c.model}`, c.category_label || c.category, c.based_on,
    c.bypassed ? "(バイパス中)" : null,
    c.responds === false ? "パソコンから変えても音は変わらない" : null, ...(c.flags || [])].filter(Boolean).join("\n");
  const blk = el("div", {
    class: `ed-blk ${selected ? "sel" : ""} ${c.bypassed ? "bypassed" : ""}`, "data-c": c.color || "util",
  }, icon(groupKeyOf(c)),
    c.responds === false ? el("span", { class: "ed-mark", "aria-hidden": "true" }, "!") : null);
  const cell = el("div", { class: `ed-cell ${selected ? "selcell" : ""}`, role: "button",
    tabindex: selected ? "0" : "-1", "aria-pressed": String(selected), "data-row": row, "data-col": col,
    "aria-label": `${where} ${c.title || c.model}${c.bypassed ? " バイパス中" : ""}`,
    title: `${tip}\nダブルクリックでバイパスの切り替え` }, blk);
  // Double-click toggles bypass, as on Cortex Control. The first click selects
  // and re-renders the grid, so a native dblclick can land on a replaced node;
  // the click count of the second click is reliable.
  cell.addEventListener("click", e => {
    if (e.detail === 2) {
      const b = cellAt(row, col);
      if (b) toggleBypass(b);
      return;
    }
    if (e.detail > 2) return;
    selectCell(row, col);
  });
  cell.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectCell(row, col); } });
  return cell;
}

function syncBass(b) {
  const hit = ED.catIndex.get(b.model_id);
  ED.sbBass = !!(hit && hit.model.bass);
}

// Selecting only changes the screen; nothing is written to the unit.
function selectCell(row, col, { focus = true } = {}) {
  if (ED.gesture) endGesture(ED.gesture);
  const c = cellAt(row, col);
  ED.sel = { row, col, model_id: c ? c.model_id : null };
  ED.sbQuery = "";
  if (c) { ED.sbGroup = groupKeyOf(c); syncBass(c); } else ED.sbGroup = null;
  renderGrid();
  renderParams();
  renderSidebar();
  if (focus) focusSelectedCell();
}

function focusSelectedCell() {
  const node = document.querySelector(".ed-cell.selcell");
  if (node) node.focus({ preventScroll: true });
}

function clearSelection() {
  if (ED.gesture) endGesture(ED.gesture);
  ED.sel = null;
  ED.sbGroup = null;
  renderGrid(); renderParams(); renderSidebar();
}

// Arrow keys: move the selection between cells (Left/Right) and rows (Up/Down).
function moveSelection(dr, dc) {
  if (!ED.view) return;
  const rows = ED.view.rows.map(r => r.row);
  if (!rows.length) return;
  let row = ED.sel ? ED.sel.row : rows[0];
  let col = ED.sel ? ED.sel.col : 0;
  if (ED.sel) {
    const i = Math.max(0, rows.indexOf(row));
    row = rows[Math.min(rows.length - 1, Math.max(0, i + dr))];
    col = Math.min(NCOLS - 1, Math.max(0, col + dc));
  }
  selectCell(row, col);
}

// ---------------------------------------------------------------- sidebar (device list)

async function loadCatalog() {
  if (ED.catalog) return ED.catalog;
  try {
    ED.catalog = await edFetch("/api/editor/catalog", undefined, { method: "GET" });
  } catch {
    ED.catalog = { groups: [] };
  }
  ED.catIndex = new Map();
  for (const g of ED.catalog.groups || []) for (const m of g.models || []) {
    if (!ED.catIndex.has(m.id)) ED.catIndex.set(m.id, { group: g, model: m });
  }
  const b = findBlock(ED.sel);
  if (b) { ED.sbGroup = groupKeyOf(b); syncBass(b); }
  if (ED.view) { renderGrid(); }
  renderSidebar();
  return ED.catalog;
}

function renderSidebar() {
  const sb = document.getElementById("sidebar");
  if (!ED.catalog) { sb.replaceChildren(el("div", { class: "sb-none" }, "デバイス一覧を読み込み中…")); return; }
  const groups = ED.catalog.groups || [];
  if (!groups.length) { sb.replaceChildren(el("div", { class: "sb-none" }, "デバイス一覧を読めませんでした。")); return; }
  const g = ED.sbGroup ? groupByKey(ED.sbGroup) : null;
  if (!g) { sb.replaceChildren(categoryList(groups)); return; }
  sb.replaceChildren(rail(groups, g), deviceList(g));
}

function categoryList(groups) {
  const empty = selIsEmptyCell();
  return el("div", { class: "sb-cats" },
    empty ? el("div", { class: "sb-hint" }, `行${ED.sel.row + 1} 列${ED.sel.col + 1}(空き)に置くデバイスを選びます。カテゴリを選んで、名前をダブルクリック。`) : null,
    groups.map(g => {
      const b = el("button", { type: "button", class: "sb-cat", "data-c": g.color || "util", title: `${g.label}(${g.models.length})` },
        el("span", { class: "sb-tile" }, icon(g.key)), el("span", { class: "sb-catname" }, g.label));
      b.addEventListener("click", () => openGroup(g.key));
      return b;
    }));
}

function openGroup(key) {
  ED.sbGroup = key;
  ED.sbQuery = "";
  const b = findBlock(ED.sel);
  const g = groupByKey(key);
  if (b && groupKeyOf(b) === key) syncBass(b);
  else if (g && g.has_bass && !g.models.some(m => !m.bass)) ED.sbBass = true;
  else ED.sbBass = false;
  renderSidebar();
  const first = document.querySelector(".sb-search");
  if (first && !SB_DRAWER_MQ.matches) first.focus({ preventScroll: true });
}

function rail(groups, current) {
  return el("nav", { class: "sb-rail", "aria-label": "カテゴリ" }, groups.map(g => {
    const on = g.key === current.key;
    const b = el("button", { type: "button", class: `sb-railbtn ${on ? "on" : ""}`, "data-c": g.color || "util",
      title: on ? `${g.label}(もう一度押すとカテゴリ一覧)` : g.label, "aria-label": g.label, "aria-pressed": String(on) }, icon(g.key));
    b.addEventListener("click", () => {
      if (on) { ED.sbGroup = null; renderSidebar(); } else openGroup(g.key);
    });
    return b;
  }));
}

function deviceList(g) {
  const b = findBlock(ED.sel);
  const target = targetText();
  const head = el("div", { class: "sb-head" },
    el("span", { class: "sb-title" }, g.label),
    el("button", { type: "button", class: "sb-back", title: "カテゴリ一覧に戻る",
      onclick: () => { ED.sbGroup = null; renderSidebar(); } }, "一覧"));
  let seg = null;
  if (g.has_bass) {
    const mk = (bass, label) => el("button", { type: "button", class: ED.sbBass === bass ? "on" : "",
      "aria-pressed": String(ED.sbBass === bass), onclick: () => { ED.sbBass = bass; renderSidebar(); } }, label);
    seg = el("div", { class: "sb-seg", role: "group", "aria-label": "ギター/ベース" }, mk(false, "GUITAR"), mk(true, "BASS"));
  }
  const search = el("input", { class: "sb-search", type: "search", placeholder: "名前で絞り込む", value: ED.sbQuery,
    "aria-label": `${g.label} を名前で絞り込む` });
  const items = el("div", { class: "sb-items", role: "listbox", "aria-label": `${g.label} のデバイス` });
  const pane = EdPhotos.previewPane();
  EdPhotos.hookList(items, pane);
  const fill = () => {
    const q = ED.sbQuery.trim().toLowerCase();
    const list = g.models.filter(m => (!g.has_bass || !!m.bass === ED.sbBass) && (!q || m.name.toLowerCase().includes(q)));
    items.replaceChildren(list.length ? list.map(m => {
      const item = deviceItem(m, b);
      EdPhotos.hookItem(item, m, pane);
      return item;
    }) : el("div", { class: "sb-none" }, "該当なし"));
  };
  search.addEventListener("input", () => { ED.sbQuery = search.value; fill(); });
  fill();
  const hint = el("div", { class: "sb-hint" }, target ? `ダブルクリックで ${target} を差し替え(保存はしません)` :
    "グリッドでブロックか空きセルを選ぶと、ダブルクリックで置けます");
  const list = el("div", { class: "sb-list", "data-c": g.color || "util" }, head, seg, search, items, pane, hint);
  requestAnimationFrame(() => { const cur = items.querySelector(".sb-item.cur"); if (cur) cur.scrollIntoView({ block: "nearest" }); });
  return list;
}

function targetText() {
  if (!ED.sel) return "";
  const b = findBlock(ED.sel);
  const where = `行${ED.sel.row + 1} 列${ED.sel.col + 1}`;
  return b ? `${where} の ${b.title || b.model}` : selIsEmptyCell() ? `${where}(空き)` : "";
}

function deviceItem(m, b) {
  const cur = !!(b && b.model_id === m.id);
  const ok = m.usable !== false;
  const item = el("div", { class: `sb-item ${ok ? "ok" : "na"} ${cur ? "cur" : ""}`, role: "option", tabindex: "0",
    "aria-selected": String(cur), "aria-disabled": ok ? null : "true",
    title: ok ? `${m.name}${m.based_on ? `\n${m.based_on}` : ""}\nダブルクリック(または Enter)で置く` : `${m.name}\n${m.why_not || "置けません"}` },
    el("span", { class: "nm" }, m.name));
  const more = el("button", { type: "button", class: "sb-more", "aria-label": `${m.name} の情報`, tabindex: "-1" }, "…");
  more.addEventListener("click", e => {
    e.stopPropagation();
    openMenu(more, [
      { head: m.category || "" },
      { info: m.name },
      m.based_on ? { info: m.based_on } : null,
      !ok ? { info: m.why_not || "置けません" } : null,
      { sep: true },
      { label: cur ? "いま置かれています" : "ここに置く(確認あり)", disabled: !ok || cur || !targetText(), onClick: () => placeModel(m) },
    ]);
  });
  item.append(more);
  item.addEventListener("dblclick", e => { e.preventDefault(); placeModel(m); });
  EdLayout.wireDevice(item, m);
  item.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); placeModel(m); } });
  return item;
}

function confirmDialog(title, text, okLabel) {
  return new Promise(resolve => {
    const ok = el("button", { type: "button", class: "primary" }, okLabel || "OK");
    const cancel = el("button", { type: "button" }, "やめる");
    const dlg = el("dialog", { class: "ed-dlg" }, el("h3", {}, title), el("p", {}, text),
      el("div", { class: "row" }, cancel, ok));
    const done = v => { dlg.close(); dlg.remove(); resolve(v); };
    ok.addEventListener("click", () => done(true));
    cancel.addEventListener("click", () => done(false));
    dlg.addEventListener("cancel", e => { e.preventDefault(); done(false); });
    document.body.append(dlg);
    dlg.showModal();
    ok.focus();
  });
}

// Double-click on a device: replace the selected block (or fill the empty cell).
async function placeModel(m) {
  if (ED.busy) return;
  if (m.usable === false) { toast(m.why_not || `${m.name} は置けません`, true); return; }
  if (!ED.sel) { toast("先にグリッドでブロックか空きセルを選んでください", true); return; }
  const { row, col } = ED.sel;
  const b = findBlock(ED.sel);
  if (!b && !selIsEmptyCell()) { toast("選んだセルの状態が分かりません。読み直します", true); loadGrid(); return; }
  if (b && b.model_id === m.id) { toast(`${m.name} はもう置かれています`); return; }
  const where = `行${row + 1} 列${col + 1}`;
  const text = b ? `${where} の ${b.title || b.model} を ${m.name} に差し替えますか? 保存はしません。取り消しで戻せます。`
    : `${where} に ${m.name} を置きますか? 保存はしません。取り消しで戻せます。`;
  if (!await confirmDialog(b ? "ブロックの差し替え" : "ブロックを置く", text, b ? "差し替える" : "置く")) return;
  if (ED.gesture) await endGesture(ED.gesture);
  if (SB_DRAWER_MQ.matches) closeDrawer();   // phones: show the grid again
  ED.busy = true;
  try {
    const res = await edFetch("/api/editor/replace", { row, col, model_id: b ? b.model_id : null, new_model_id: m.id });
    (res.warnings || []).forEach(w => toast(w, true));
    toast(res.note || `${where} を ${m.name} にしました(保存はしていません)`);
    ED.sel = { row, col, model_id: res.model_id ?? m.id };
  } catch (e) {
    /* edFetch showed the reason (409: changed on the unit, 400: refused) */
  } finally {
    ED.busy = false;
  }
  await loadGrid();
  const now = findBlock(ED.sel);
  if (now) { ED.sbGroup = groupKeyOf(now); renderSidebar(); }
  loadHistory();
}

// ---------------------------------------------------------------- parameter screen

const POWER_ICON = () => svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  svg("path", { d: "M12 3.5v8" }), svg("path", { d: "M7.2 6.6a7.5 7.5 0 1 0 9.6 0" }));
const DOTS_V = () => svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  svg("circle", { cx: 12, cy: 6, r: 1.6 }), svg("circle", { cx: 12, cy: 12, r: 1.6 }), svg("circle", { cx: 12, cy: 18, r: 1.6 }));

function renderParams() {
  const host = document.getElementById("edParams");
  if (!ED.view) return;
  const b = findBlock(ED.sel);
  if (!b) {
    const msg = selIsEmptyCell()
      ? `行${ED.sel.row + 1} 列${ED.sel.col + 1} は空きです。左の一覧(スマホは ☰)からデバイスをダブルクリックすると置けます。`
      : "グリッドのブロックを選ぶと、ここにパラメーターが出ます。矢印キーで選択を動かせます。";
    host.replaceChildren(el("div", { class: "cc", "data-c": "util" },
      el("div", { class: "cc-head" }, el("span", { class: "cc-dots", "aria-hidden": "true" }, DOTS_V()),
        el("span", { class: "cc-title" }, el("span", { class: "cc-cat" }, selIsEmptyCell() ? "空きセル" : "ブロック未選択"))),
      el("div", { class: "cc-none" }, msg)));
    return;
  }
  const key = blockKey(b);
  const params = b.params || [];
  const pages = Math.ceil(params.length / PER_PAGE);
  let page = Math.min(ED.page[key] || 0, Math.max(0, pages - 1));
  ED.page[key] = page;

  const unknown = b.bypassed === null || b.bypassed === undefined;
  const power = el("button", {
    type: "button", class: "cc-power", "aria-pressed": unknown ? null : String(!b.bypassed), disabled: unknown,
    "aria-label": unknown ? "バイパスの状態が分かりません" : b.bypassed ? "バイパス中(押すとオン)" : "オン(押すとバイパス)",
    title: unknown ? "バイパスの状態が分かりません" : `${b.bypassed ? "バイパス中" : "オン"}。クリックで切り替え(1 回で取り消しの 1 単位)`,
  }, POWER_ICON());
  power.addEventListener("click", () => toggleBypass(b));

  const tabs = pages > 1 ? el("div", { class: "cc-pages", role: "group", "aria-label": "ページ" },
    Array.from({ length: pages }, (_, i) => {
      const t = el("button", { type: "button", "aria-pressed": String(i === page), "aria-label": `ページ ${i + 1}` }, String(i + 1));
      t.addEventListener("click", () => { ED.page[key] = i; renderParams(); });
      return t;
    })) : null;

  const dots = el("button", { type: "button", class: "cc-dots", "aria-label": "ブロックの情報", "aria-haspopup": "true" }, DOTS_V());
  dots.addEventListener("click", () => openMenu(dots, [
    ...EdLayout.blockMenuItems(b),
    ED.view.repo && ED.view.repo.note ? { info: `表示属性: ${ED.view.repo.note}` } : null,
  ]));

  const banners = [];
  if (b.responds === false) {
    banners.push(el("div", { class: "cc-banner" },
      "このブロックはパソコンから変えても音が変わりません(読み戻せるが音は変わらない)"));
  }
  for (const f of b.flags || []) banners.push(el("div", { class: "cc-banner" }, f));

  const list = params.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  const cells = list.map(p => paramCell(b, p));
  const pad = (5 - (list.length % 5)) % 5;
  for (let k = 0; k < pad; k++) cells.push(el("div", { class: "cc-cell blank", "aria-hidden": "true" }));

  host.replaceChildren(EdPhotos.wrap(el("div", { class: `cc ${b.bypassed ? "byp" : ""}`, "data-c": b.color || "util" },
    el("div", { class: "cc-head" },
      dots,
      el("span", { class: "cc-title", title: b.based_on || "" },
        el("span", { class: "cc-cat" }, b.category_label || b.category || ""),
        el("b", {}, b.title || b.model)),
      tabs, power),
    banners,
    params.length ? el("div", { class: "cc-grid" }, cells)
      : el("div", { class: "cc-none" }, "表示できるパラメーターはありません。")), b));
}

function paramCell(b, p) {
  if (p.kind === "knob") return knobCell(b, p);
  if (p.kind === "select") return selectCellParam(b, p);
  return el("div", { class: "cc-cell text off", title: p.why_not || "" },
    el("span", { class: "cc-name" }, p.name),
    el("div", { class: "cc-body" }, el("span", { class: "cc-text", "aria-disabled": "true" }, p.text ?? "—")));
}

// ---- knob

function knobSvg(p, v) {
  const a = EC.angle(p, v);
  const t = (a - 90) * Math.PI / 180;
  const x = 50 + 31 * Math.cos(t), y = 50 + 31 * Math.sin(t);
  return svg("svg", { class: "cc-knob", viewBox: "0 0 100 100", "aria-hidden": "true" },
    svg("path", { d: EC.arcPath(45, -135, 135), fill: "none", stroke: "#0a0a0a", "stroke-width": "7" }),
    a > -134.5 ? svg("path", { d: EC.arcPath(45, -135, a), fill: "none", stroke: "var(--c)", "stroke-width": "7" }) : null,
    svg("circle", { cx: 50, cy: 50, r: 37, fill: "#1a1a1a", stroke: "#0a0a0a", "stroke-width": "3" }),
    svg("line", { x1: 50, y1: 50, x2: x.toFixed(2), y2: y.toFixed(2), stroke: "#fff", "stroke-width": "4", "stroke-linecap": "round" }));
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
    e.stopPropagation();   // a focused knob keeps its arrow keys (no block navigation)
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
    cell, b, p, startValue: p.value, final: p.value, id: null, failed: false, aborted: false,
    ending: null, idleTimer: null,
  };
  // The previous gesture's /end must have finished before this /begin goes out.
  const before = ED.lastEnd;
  g.begin = before.catch(() => {})
    .then(() => edFetch("/api/editor/begin", { ...writeTarget(b, p), expect: p.value }))
    .then(res => { g.id = res.gesture; return g.id; })
    .catch(err => { g.failed = true; throw err; });
  g.begin.catch(() => {});   // handled where awaited
  g.throttle = new EC.Throttle(async (value, seq) => {
    const id = await g.begin;
    if (g.aborted) return;
    try {
      await edFetch("/api/editor/move", { gesture: id, seq, [valueField(p)]: value }, { quiet: true });
    } catch (e) {
      if (e.status === 409 && e.data && e.data.conflict) abortGesture(g, e);
      else if (!g.moveErrorShown) {
        // Job running, device busy, ...: the server still holds the gesture, so it must
        // still get an /end (sent when the knob is let go).
        g.moveErrorShown = true;
        toast((e && e.message) || "書き込めませんでした", true);
      }
    }
  }, 100);
  ED.gesture = g;
  cell.classList.add("active");
  return g;
}

// /move answered 409: the block changed mid-drag and the server already stopped the
// gesture. Stop sending, do not send /end, read the grid again.
function abortGesture(g, err) {
  if (g.aborted || g.ending) return;
  g.aborted = true;
  if (g.idleTimer) { clearTimeout(g.idleTimer); g.idleTimer = null; }
  g.throttle.cancel().catch(() => {});
  toast(conflictText(409, (err && err.data) || { conflict: true }, (err && err.message) || "ブロックが変わりました"), true);
  g.ending = Promise.resolve();
  if (ED.gesture === g) ED.gesture = null;
  g.cell.classList.remove("active");
  ED.reloadWanted = false;
  loadGrid();
  loadHistory();
}

function gestureMove(cell, b, p, v, idleEnds) {
  if (!cell.isConnected) return;
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
      if (g.aborted) return;
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
  ED.lastEnd = g.ending;
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

function selectCellParam(b, p) {
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

// ---------------------------------------------------------------- menus / popovers

let openPop = null;
function closeMenu() {
  if (!openPop) return;
  const { node, anchor } = openPop;
  node.remove();
  if (anchor) anchor.setAttribute("aria-expanded", "false");
  openPop = null;
}

// items: {label, onClick|href, disabled, on, cur} | {sep} | {head} | {info} | null
function openMenu(anchor, items) {
  const again = openPop && openPop.anchor === anchor;
  closeMenu();
  if (again) return;
  const node = el("div", { class: "pop", role: "menu" });
  for (const it of items) {
    if (!it) continue;
    if (it.sep) node.append(el("hr"));
    else if (it.head !== undefined) { if (it.head) node.append(el("div", { class: "pop-h" }, it.head)); }
    else if (it.info !== undefined) node.append(el("div", { class: "pop-info" }, it.info));
    else if (it.href) node.append(el("a", { href: it.href, role: "menuitem", class: it.cur ? "cur" : "" }, it.label));
    else {
      const b = el("button", { type: "button", role: "menuitem", disabled: !!it.disabled, class: it.on ? "on" : "",
        title: it.title || null }, it.label);
      b.addEventListener("click", () => { closeMenu(); it.onClick && it.onClick(); });
      node.append(b);
    }
  }
  document.body.append(node);
  const r = anchor.getBoundingClientRect();
  const w = node.offsetWidth, h = node.offsetHeight;
  let left = Math.min(window.innerWidth - w - 8, Math.max(8, r.right - w));
  if (r.left + w < window.innerWidth - 8 && r.left < window.innerWidth / 2) left = Math.max(8, r.left);
  let top = r.bottom + 6;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
  node.style.left = `${left}px`;
  node.style.top = `${top}px`;
  anchor.setAttribute("aria-expanded", "true");
  openPop = { node, anchor };
  const first = node.querySelector("button:not(:disabled), a");
  if (first) first.focus({ preventScroll: true });
}

document.addEventListener("pointerdown", e => {
  if (openPop && !openPop.node.contains(e.target) && !openPop.anchor.contains(e.target)) closeMenu();
}, true);
window.addEventListener("resize", closeMenu);

function mainMenuItems() {
  return [
    { head: "qcsuite のページ" },
    ...PAGES.map(([id, label]) => ({ label, href: `/static/${id}.html`, cur: id === "editor" })),
    { sep: true },
    { label: "読み直す", onClick: () => { loadGrid(); loadHistory(); loadAB(); } },
    { label: "履歴", onClick: () => openHistory() },
    { label: "sandbox に保存…", onClick: () => saveFlow() },
  ];
}

function abItems() {
  const ab = ED.ab || {};
  const slot = s => ab.slots && ab.slots[s];
  const t = s => slot(s) ? `${slot(s).label}(${slot(s).at})` : "まだ記録していません";
  return [
    { head: "A/B 比較(本体の音が切り替わります)" },
    { label: `A を聴く${ab.active === "A" ? "(いま)" : ""}`, on: ab.active === "A", disabled: !slot("A"), title: t("A"), onClick: () => abSwitch("A") },
    { label: `B を聴く${ab.active === "B" ? "(いま)" : ""}`, on: ab.active === "B", disabled: !slot("B"), title: t("B"), onClick: () => abSwitch("B") },
    { sep: true },
    { label: "いまを A に記録", onClick: () => abCapture("A") },
    { label: "いまを B に記録", onClick: () => abCapture("B") },
  ];
}

// ---------------------------------------------------------------- history, undo, A/B, save

async function loadHistory() {
  let rows;
  try { rows = await api("/api/panel/history"); } catch { return; }
  const engines = { rules: "ルール", claude: "Claude", editor: "エディター" };
  document.getElementById("history").replaceChildren(rows.length ? table(rows.slice().reverse(), [
    ["at", "時刻", v => String(v || "").split(" ").pop()], ["instruction", "指示"], ["engine", "変換", v => engines[v] || v || ""],
    ["summary", "内容", v => (v || []).join(" / ")],
    ["undone", "状態", v => v ? "取り消し済み" : "適用中"],
  ]) : el("p", { class: "muted small" }, "(まだありません)"));
}

function openHistory() {
  const d = document.getElementById("historyDrawer");
  d.hidden = false;
  document.getElementById("btnHistory").classList.add("on");
  loadHistory();
  document.getElementById("btnHistoryClose").focus({ preventScroll: true });
}
function closeHistory() {
  document.getElementById("historyDrawer").hidden = true;
  document.getElementById("btnHistory").classList.remove("on");
}

async function loadAB() {
  try { ED.ab = await api("/api/panel/ab"); } catch { return; }
  const on = !!(ED.ab && ED.ab.active);
  for (const id of ["btnAB", "btnAB2"]) {
    const b = document.getElementById(id);
    b.classList.toggle("on", on);
    b.title = on ? `A/B 比較(いま ${ED.ab.active})` : "A/B 比較";
  }
}

async function abCapture(slot) {
  try {
    await api("/api/panel/ab/capture", { method: "POST", body: { slot } });
    toast(`いまの状態を ${slot} に記録しました`);
  } catch { /* shown */ }
  loadAB();
}

async function abSwitch(slot) {
  if (ED.gesture) await endGesture(ED.gesture);
  try {
    const r = await api("/api/panel/ab/switch", { method: "POST", body: { slot } });
    toast(`${slot} にしました(${r.applied} 件の操作)`);
    (r.notes || []).forEach(n => toast(n));
  } catch { /* shown */ }
  loadGrid(); loadHistory(); loadAB();
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

// sandbox save: ask for a name, build the plan (dry run), showPlan asks to confirm.
function saveFlow() {
  const input = el("input", { placeholder: "保存する名前", "aria-label": "保存する名前",
    value: (ED.view && ED.view.preset && ED.view.preset.name) || "" });
  const ok = el("button", { type: "button", class: "primary" }, "計画を見る");
  const cancel = el("button", { type: "button" }, "やめる");
  const dlg = el("dialog", { class: "ed-dlg" }, el("h3", {}, "sandbox に保存"),
    el("p", { class: "small" }, "いまのグリッドを sandbox の空きスロットに保存する計画を作ります。まず計画(dry run)だけを作り、内容を確認してから実行します。エディターの操作だけでは保存されません。"),
    input, el("div", { class: "row" }, cancel, ok));
  const close = () => { dlg.close(); dlg.remove(); };
  const go = async () => {
    const name = input.value.trim();
    if (!name) { toast("名前を入れてください", true); input.focus(); return; }
    ok.disabled = true;
    try {
      const plan = await api("/api/panel/save", { method: "POST", body: { name } });
      close();
      showPlan(plan, () => toast("保存しました"));
    } catch { ok.disabled = false; /* shown */ }
  };
  ok.addEventListener("click", go);
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  cancel.addEventListener("click", close);
  dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
  document.body.append(dlg);
  dlg.showModal();
  input.select();
}

// ---------------------------------------------------------------- sidebar drawer (phones)

function openDrawer() {
  document.getElementById("sidebar").classList.add("open");
  document.getElementById("sbBackdrop").hidden = false;
}
function closeDrawer() {
  document.getElementById("sidebar").classList.remove("open");
  document.getElementById("sbBackdrop").hidden = true;
}

// ---------------------------------------------------------------- wiring

document.getElementById("btnUndo").addEventListener("click", () => undoRedo("undo"));
document.getElementById("btnRedo").addEventListener("click", () => undoRedo("redo"));
document.getElementById("btnAB").addEventListener("click", e => openMenu(e.currentTarget, abItems()));
document.getElementById("btnAB2").addEventListener("click", e => openMenu(e.currentTarget, abItems()));
document.getElementById("btnMore").addEventListener("click", e => openMenu(e.currentTarget, mainMenuItems()));
document.getElementById("btnPresetMenu").addEventListener("click", e => openMenu(e.currentTarget, [
  { label: "プリセットを選ぶ…", onClick: () => EdPresets.openList() },
  { label: "読み直す", onClick: () => { loadGrid(); loadHistory(); loadAB(); } },
  { label: "sandbox に保存…", onClick: () => saveFlow() },
  { label: "履歴", onClick: () => openHistory() },
  { sep: true },
  { info: "プリセットは < > か名前のクリックで切り替えます(音が変わります。保存前の編集があるときは確認します)。シーンは A〜H。本体で変えたものは自動で画面に反映されます。" },
]));
document.getElementById("btnSave").addEventListener("click", () => saveFlow());
document.getElementById("btnHistory").addEventListener("click", () => {
  if (document.getElementById("historyDrawer").hidden) openHistory(); else closeHistory();
});
document.getElementById("btnHistoryClose").addEventListener("click", closeHistory);
document.getElementById("btnDevices").addEventListener("click", openDrawer);
document.getElementById("sbBackdrop").addEventListener("click", closeDrawer);

document.addEventListener("keydown", e => {
  const t = e.target;
  if (e.key === "Escape") {
    if (openPop) { closeMenu(); e.preventDefault(); return; }
    if (document.querySelector("dialog[open]")) return;
    if (!document.getElementById("historyDrawer").hidden) { closeHistory(); return; }
    if (document.getElementById("sidebar").classList.contains("open")) { closeDrawer(); return; }
    if (!typingTarget(t) && ED.sel) { clearSelection(); return; }
    return;
  }
  if (typingTarget(t)) return;
  if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === "z" || e.key === "Z")) {
    e.preventDefault();
    undoRedo(e.shiftKey ? "redo" : "undo");
    return;
  }
  // Arrow keys move the block selection unless a knob, field, menu or dialog has focus.
  const arrows = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] };
  if (arrows[e.key] && !e.metaKey && !e.ctrlKey && !e.altKey) {
    if (openPop || document.querySelector("dialog[open]")) return;
    if (t && t.closest && (t.closest('[role="slider"]') || t.closest(".sb") || t.closest(".drawer") || t.closest(".cc-tog"))) return;
    e.preventDefault();
    moveSelection(...arrows[e.key]);
  }
});

// Changes made in another tab (言葉で操作, the unit's screen) show up when we come back.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { loadGrid(); loadHistory(); loadAB(); }
});

// Leaving mid-gesture: close it so the server records one undo unit.
window.addEventListener("pagehide", () => {
  const g = ED.gesture;
  if (!g || !g.id || g.aborted || !navigator.sendBeacon) return;
  const body = { gesture: g.id, [valueField(g.p)]: g.final };
  navigator.sendBeacon("/api/editor/end", new Blob([JSON.stringify(body)], { type: "application/json" }));
  // The gesture is closed now; a page restored from the back/forward cache must not end it twice.
  if (g.idleTimer) { clearTimeout(g.idleTimer); g.idleTimer = null; }
  g.aborted = true;
  g.ending = Promise.resolve();
  ED.gesture = null;
});
window.addEventListener("pageshow", e => { if (e.persisted) loadGrid(); });

// common.js header: still fetched for QC.status (mode, version), but hidden by CSS.
function showModeBadge(st) {
  const badge = document.getElementById("edMode");
  if (st) {
    if (st.mode === "mock") { badge.textContent = "モック(実機なし)"; badge.className = "tb-badge mock"; }
    else { badge.textContent = st.connected ? "実機に接続中" : "実機(未接続)"; badge.className = "tb-badge hw"; }
  } else badge.textContent = "状態不明";
}

const headerReady = renderHeader("editor").then(st => {
  showModeBadge(st);
  renderStatus();
  return st;
});
renderSidebar();
headerReady.then(() => { loadGrid(); loadHistory(); loadAB(); loadCatalog(); });
