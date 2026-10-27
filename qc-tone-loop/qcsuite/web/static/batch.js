const $ = id => document.getElementById(id);
// replaceChildren() neither flattens arrays nor skips nulls; this does both.
const put = (node, ...kids) => node.replaceChildren(
  ...kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false));
const chosen = new Map();     // "folder:slot" -> name
let current = null;

async function drawDis() {
  const dis = await api("/api/di");
  put($("dis"), dis.map((d, i) => el("label", {},
    el("input", { type: "checkbox", value: d.name, checked: i === 0, class: "dicb" }),
    `${d.name}(${fmt(d.seconds)} 秒)`)));
}

async function drawFolders() {
  const folders = await api("/api/folders");
  put($("folderSel"), el("option", { value: "" }, "(フォルダを選ぶ)"),
    folders.map(f => el("option", { value: f.key }, `${f.name}(${f.occupied})`)));
}

async function drawPresets() {
  const folder = $("folderSel").value;
  if (!folder) return;
  const rows = await api(`/api/presets?folder=${encodeURIComponent(folder)}`);
  put($("presets"), table(rows, [
    ["slot", "選", (v, r) => {
      const ref = `${folder}:${r.slot}`;
      const cb = el("input", { type: "checkbox", checked: chosen.has(ref) });
      cb.addEventListener("change", () => {
        cb.checked ? chosen.set(ref, r.name) : chosen.delete(ref);
        $("picked").textContent = `${chosen.size} 件選択`;
      });
      return cb;
    }],
    ["slot", "スロット"], ["name", "名前"],
  ]));
}

async function run() {
  const dis = [...document.querySelectorAll(".dicb:checked")].map(c => c.value);
  const whole = $("wholeFolder").checked;
  const body = { dis, format: $("fmt").value,
    presets: whole ? [] : [...chosen.keys()], folder: whole ? $("folderSel").value : "" };
  if (!dis.length) { toast("DI を選んでください", true); return; }
  if (!body.folder && !body.presets.length) { toast("プリセットを選んでください", true); return; }
  if (!window.confirm("プリセットを順に本体に読み込みます。音が切り替わり、本体で保存していない編集は消えます。")) return;
  if (!confirmMonitor()) return;
  const r = await api("/api/batch/run", { method: "POST", body });
  toast(`ジョブ ${r.job}: ${r.items} 本のステムを作ります`);
  watchJob(r.job, () => loadJobs(r.job));
}

async function loadJobs(select) {
  const list = await api("/api/batch/jobs");
  put($("jobSel"), el("option", { value: "" }, "(ジョブを選ぶ)"),
    list.map(j => el("option", { value: j.id }, `${j.id} ${j.title || ""} ${j.status} ${j.done || 0}/${j.total || 0}`)));
  if (select) { $("jobSel").value = select; show(); }
}

async function show() {
  current = $("jobSel").value;
  if (!current) return;
  const d = await api(`/api/batch/${current}`);
  put($("mapDi"), d.dis.map(x => el("option", { value: x }, x)));
  const lat = d.latency;
  put($("stems"), 
    el("p", {}, `${d.title}: ${d.stems.length} 本。往復遅延 ${fmt(lat.min_ms)}〜${fmt(lat.max_ms)} ms。`,
      el("a", { href: fileUrl(`batch/${current}/stems.csv`) }, "stems.csv"), " / ",
      el("a", { href: fileUrl(`batch/${current}/README.md`) }, "README.md")),
    lat.warning ? el("div", { class: "warn" }, lat.warning) : null,
    el("p", { class: "muted" }, "DI: ", d.di_stems.map(p => el("audio", { controls: true, preload: "none", src: fileUrl(p) }))),
    table(d.stems, [
      ["n", "順"], ["di", "DI"], ["name", "プリセット"], ["folder_name", "フォルダ"], ["slot", "スロット"],
      ["latency_ms", "遅延 ms", v => fmt(v)], ["lufs", "LUFS", v => fmt(v)],
      ["excluded", "除外", (v, r) => v ? el("span", { class: "danger", title: r.reasons.join("\n") }, "除外の印") : ""],
      ["file", "試聴", v => el("audio", { controls: true, preload: "none", src: fileUrl(v) })],
    ]));
}

function drawMap(data) {
  const w = 640, h = 420, pad = 40, ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", "100%");
  svg.style.maxWidth = `${w}px`;
  const xs = data.points.map(p => p.x), ys = data.points.map(p => p.y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const sx = v => pad + (w - 2 * pad) * (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0));
  const sy = v => h - pad - (h - 2 * pad) * (y1 === y0 ? 0.5 : (v - y0) / (y1 - y0));
  for (const p of data.points) {
    const g = document.createElementNS(ns, "g");
    g.style.cursor = "pointer";
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", sx(p.x)); c.setAttribute("cy", sy(p.y)); c.setAttribute("r", 5);
    c.setAttribute("fill", "var(--accent)");
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", sx(p.x) + 7); t.setAttribute("y", sy(p.y) + 4);
    t.setAttribute("font-size", 11); t.setAttribute("fill", "currentColor");
    t.textContent = p.name;
    g.append(c, t);
    g.addEventListener("click", () => {
      const list = data.neighbours[p.key] || [];
      put($("near"), el("p", {}, `「${p.name}」(${p.folder} ${p.slot})に測定値が近いもの:`),
        el("ol", {}, list.map(n => el("li", {}, `${n.name}(${n.folder} ${n.slot}) 距離 ${fmt(n.distance, 2)}`))),
        el("audio", { controls: true, preload: "none", src: fileUrl(p.file) }));
    });
    svg.append(g);
  }
  put($("map"), 
    el("p", { class: "small muted" }, data.note + (data.excluded.length ? ` 除外: ${data.excluded.join(", ")}` : "")),
    svg);
}

$("folderSel").addEventListener("change", drawPresets);
$("btnRun").addEventListener("click", run);
$("jobSel").addEventListener("change", show);
$("btnResume").addEventListener("click", async () => {
  if (!current) { toast("ジョブを選んでください", true); return; }
  if (!confirmMonitor()) return;
  const r = await api(`/api/batch/${current}/resume`, { method: "POST" });
  watchJob(r.job, () => show());
});
$("btnMap").addEventListener("click", async () => {
  if (!current) { toast("ジョブを選んでください", true); return; }
  drawMap(await api(`/api/batch/${current}/map?di=${encodeURIComponent($("mapDi").value)}`));
});

renderHeader("batch").then(() => { drawDis(); drawFolders(); loadJobs(); });
