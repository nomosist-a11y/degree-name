const $ = id => document.getElementById(id);
// replaceChildren() neither flattens arrays nor skips nulls; this does both.
const put = (node, ...kids) => node.replaceChildren(
  ...kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false));
let current = null;          // selected job id
let lastRows = [];
const picked = new Set();

function parseBase(text) {
  const t = (text || "").trim();
  if (!t || t === "current") return "current";
  const i = t.lastIndexOf(":");
  if (i < 0) { toast("ベースは current か フォルダ:スロット(例 kami:1E)", true); return "current"; }
  return { preset: { folder: t.slice(0, i), slot: t.slice(i + 1) } };
}

function numbers(text) {
  return text.split(/[,\s]+/).filter(Boolean).map(Number).filter(v => !Number.isNaN(v));
}

function buildSpec() {
  const params = {};
  const p1 = $("fParam").value.trim(), p2 = $("fParam2").value.trim();
  if (p1) params[p1] = numbers($("fValues").value);
  if (p2) params[p2] = numbers($("fValues2").value);
  const cell = { row: Number($("fRow").value) - 1, col: Number($("fCol").value) - 1, params };
  const models = $("fModels").value.split(",").map(s => s.trim()).filter(Boolean);
  if (models.length) cell.models = models;
  const spec = { title: `行${cell.row + 1}列${cell.col + 1} ${Object.keys(params).join("×")}`,
    di: $("fDi").value || "synthetic-riff", base: parseBase($("fBase").value),
    mode: $("fMode").value, cells: [cell] };
  if ($("fSeconds").value) spec.seconds = Number($("fSeconds").value);
  $("spec").value = JSON.stringify(spec, null, 2);
}

function specBody() {
  try { return JSON.parse($("spec").value); }
  catch (e) { toast(`JSON が読めません: ${e.message}`, true); throw e; }
}

async function preview() {
  const spec = specBody();
  // A preset base is recalled to resolve the cells: the unit's sound changes.
  const recalls = spec && spec.base && typeof spec.base === "object" && spec.base.preset;
  if (recalls && !window.confirm("ベースのプリセットを本体に読み込みます(音が切り替わり、保存していない編集は消えます)。")) return null;
  const r = await api("/api/sweep/preview", { method: "POST", body: { spec, allow_recall: !!recalls } });
  put($("preview"), 
    el("p", {}, `${r.count} 通り、見込み ${fmt(r.estimate_s / 60)} 分` +
      (QC.status && QC.status.mode === "mock" ? "(モック)" : "(実機で 1 通り約 16 秒の実測から)")),
    r.large ? el("div", { class: "warn" }, "500 通りを超えています。実行するときに確認します。") : null,
    r.notes.length ? el("ul", {}, r.notes.map(n => el("li", {}, n))) : null,
    el("ol", { start: 0 }, r.configs.slice(0, 30).map(c => el("li", {}, c.label))));
  return r;
}

async function run() {
  const spec = specBody();
  const pre = await preview();
  if (!pre) return;
  let confirm_large = false;
  if (pre.large) {
    if (!window.confirm(`${pre.count} 通りになります。本当に実行しますか?`)) return;
    confirm_large = true;
  }
  if (!confirmMonitor()) return;
  const r = await api("/api/sweep/run", { method: "POST", body: { spec, confirm_large } });
  toast(`ジョブ ${r.job} を始めました(${r.count} 通り)`);
  watchJob(r.job, () => { loadJobs(r.job); });
}

async function loadJobs(select) {
  const list = await api("/api/sweep/jobs");
  const sel = $("jobSel");
  put(sel, el("option", { value: "" }, "(ジョブを選ぶ)"),
    list.map(j => el("option", { value: j.id }, `${j.id} ${j.title || ""} ${j.status} ${j.done || 0}/${j.total || 0}`)));
  if (select) { sel.value = select; current = select; loadResults(); }
}

async function loadResults() {
  current = $("jobSel").value;
  if (!current) return;
  const data = await api(`/api/sweep/${current}/results?sort=${$("sortSel").value}` +
    `&include_excluded=${!$("hideEx").checked}`);
  lastRows = data.rows;
  picked.clear();
  const cols = [
    ["key", "選", (v, r) => {
      const cb = el("input", { type: "checkbox", disabled: r.excluded });
      cb.addEventListener("change", () => cb.checked ? picked.add(r.key) : picked.delete(r.key));
      return cb;
    }],
    ["label", "構成"],
  ];
  if (data.has_reference) cols.push(["score", "スコア", v => fmt(v)], ["distance", "距離", v => fmt(v, 2)]);
  cols.push(["lufs", "LUFS", v => fmt(v)], ["flatness", "flatness", v => fmt(v, 3)],
    ["crest", "クレスト", v => fmt(v)], ["tilt", "傾き", v => fmt(v, 2)],
    ["excluded", "除外", (v, r) => v ? el("span", { class: "danger", title: (r.reasons || []).join("\n") }, "除外") : ""],
    ["file", "試聴", v => v ? el("audio", { controls: true, preload: "none", src: fileUrl(v) }) : ""]);
  put($("results"), 
    el("p", { class: "muted" }, `${data.title}: ${data.rows.length} 件(除外 ${data.excluded})。${data.note}`),
    data.notes && data.notes.length ? el("ul", {}, data.notes.map(n => el("li", {}, n))) : null,
    table(data.rows, cols));
}

async function send(body) {
  if (!current) { toast("ジョブを選んでください", true); return; }
  const r = await api(`/api/sweep/${current}/tournament`, { method: "POST", body });
  toast(`ブラインド比較 ${r.tournament.id || ""} に ${r.sent.length} 件を送りました`);
}

function lineChart(points, key, label) {
  const w = 220, h = 140, pad = 28;
  const xs = points.map(p => p.value), ys = points.map(p => p[key]).filter(v => v !== null);
  if (!ys.length) return el("div");
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = v => pad + (w - 2 * pad) * (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0));
  const sy = v => h - pad - (h - 2 * pad) * (y1 === y0 ? 0.5 : (v - y0) / (y1 - y0));
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", w); svg.setAttribute("height", h);
  const add = (tag, attrs, text) => {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    svg.append(n); return n;
  };
  add("line", { x1: pad, y1: h - pad, x2: w - pad, y2: h - pad, stroke: "currentColor", "stroke-opacity": 0.3 });
  add("polyline", { fill: "none", stroke: "var(--accent)", "stroke-width": 2,
    points: points.filter(p => p[key] !== null).map(p => `${sx(p.value)},${sy(p[key])}`).join(" ") });
  for (const p of points) if (p[key] !== null)
    add("circle", { cx: sx(p.value), cy: sy(p[key]), r: 3, fill: p.excluded ? "var(--danger)" : "var(--accent)" });
  add("text", { x: pad, y: 14, "font-size": 11, fill: "currentColor" }, label);
  add("text", { x: pad, y: h - 8, "font-size": 9, fill: "currentColor" }, fmt(x0, 2));
  add("text", { x: w - pad, y: h - 8, "font-size": 9, fill: "currentColor", "text-anchor": "end" }, fmt(x1, 2));
  add("text", { x: 2, y: sy(y1) + 3, "font-size": 9, fill: "currentColor" }, fmt(y1, 2));
  add("text", { x: 2, y: sy(y0) + 3, "font-size": 9, fill: "currentColor" }, fmt(y0, 2));
  return el("div", { class: "panel", style: "display:inline-block;margin:4px;padding:6px" }, svg);
}

async function drawSensitivity(jobId) {
  const d = await api(`/api/sweep/${jobId}/sensitivity`);
  put($("sens"), 
    el("p", { class: "small muted" }, `${d.param} を動かしたときの測定値(赤い点は除外)。音の良し悪しではありません。`),
    el("div", {}, Object.entries(d.measures).map(([k, label]) => lineChart(d.points, k, label))));
}

async function sensitivity() {
  if (!confirmMonitor()) return;
  const r = await api("/api/sweep/sensitivity", { method: "POST", body: {
    row: Number($("sRow").value) - 1, col: Number($("sCol").value) - 1, param: $("sParam").value.trim(),
    steps: Number($("sSteps").value), base: parseBase($("sBase").value) } });
  watchJob(r.job, () => { drawSensitivity(r.job); loadJobs(); });
}

$("btnBuild").addEventListener("click", buildSpec);
$("btnPreview").addEventListener("click", preview);
$("btnRun").addEventListener("click", run);
$("jobSel").addEventListener("change", loadResults);
$("sortSel").addEventListener("change", loadResults);
$("hideEx").addEventListener("change", loadResults);
$("btnSendSel").addEventListener("click", () => {
  if (picked.size < 2) { toast("2 件以上選んでください", true); return; }
  send({ keys: [...picked] });
});
$("btnSendTop").addEventListener("click", () => send({ top: Number($("topN").value) }));
$("btnResume").addEventListener("click", async () => {
  if (!current) { toast("ジョブを選んでください", true); return; }
  if (!confirmMonitor()) return;
  const r = await api(`/api/sweep/${current}/resume`, { method: "POST" });
  watchJob(r.job, () => loadResults());
});
$("btnSens").addEventListener("click", sensitivity);

renderHeader("sweep").then(async () => {
  const dis = await api("/api/di");
  put($("fDi"), dis.map(d => el("option", { value: d.name }, d.name)));
  buildSpec();
  loadJobs();
});
