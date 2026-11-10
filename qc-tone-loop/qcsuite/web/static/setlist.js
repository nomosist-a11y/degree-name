let current = null;
let folders = [];

async function loadSaved() {
  const rows = await api("/api/setlist/list");
  document.getElementById("saved").replaceChildren(rows.length ? table(rows, [
    ["name", "名前"], ["songs", "曲数"], ["resolved", "決定済み"], ["updated", "更新"],
  ], { onRow: r => openSetlist(r.name) }) : el("p", {}, "(まだありません)"));
}

async function openSetlist(name) {
  current = await api(`/api/setlist/${encodeURIComponent(name)}`);
  draw();
}

function where(p) { return p ? `${p.folder_name}:${p.slot}「${p.name ?? ""}」` : ""; }

function draw() {
  const s = current;
  document.getElementById("songPanel").hidden = false;
  document.getElementById("songTitle").textContent = `曲とプリセット: ${s.name}`;
  document.getElementById("problems").replaceChildren(s.problems.length
    ? el("div", { class: "warn small" }, "未解決があります。候補から選んでください:", el("pre", {}, s.problems.join("\n"))) : "");
  const rows = s.songs.map(x => ({ ...x }));
  document.getElementById("songTable").replaceChildren(table(rows, [
    ["n", "#"], ["song", "曲名"], ["query", "指定"],
    ["status", "状態", v => ({ resolved: "決定", ambiguous: "曖昧", missing: "見つからない" }[v] || v)],
    ["source", "プリセット", (v, r) => r.status === "resolved" ? where(v) : candidatePicker(r)],
    ["target", "配置先", v => v ? (v.placed ? where(v) : `${v.folder_name}:${v.slot}(未配置)`) : ""],
  ]));
  const plc = s.placement;
  document.getElementById("btnPlan").disabled = s.problems.length > 0;
  if (plc && plc.mode === "map") toast("今ある場所で使う設定です(コピーしません)");
}

function candidatePicker(r) {
  const sel = el("select", {}, el("option", { value: "" }, "候補を選ぶ…"),
    ...(r.candidates || []).map(c => el("option", { value: `${c.folder_name}:${c.slot}` }, `${c.folder_name}:${c.slot}「${c.name}」`)));
  sel.addEventListener("change", async () => {
    if (!sel.value) return;
    current = await api(`/api/setlist/${encodeURIComponent(current.name)}/choose`, { method: "POST", body: { n: r.n, preset: sel.value } });
    draw();
  });
  const manual = el("input", { placeholder: "フォルダ:スロット", size: 12 });
  manual.addEventListener("change", async () => {
    current = await api(`/api/setlist/${encodeURIComponent(current.name)}/choose`, { method: "POST", body: { n: r.n, preset: manual.value.trim() } });
    draw();
  });
  return el("span", {}, sel, " ", manual);
}

async function midi() {
  const ch = Number(document.getElementById("channel").value) || 1;
  const base = `/api/setlist/${encodeURIComponent(current.name)}/midi`;
  const r = await api(`${base}?format=json&channel=${ch}`);
  document.getElementById("midiNotes").replaceChildren(el("ul", {}, r.notes.map(n => el("li", {}, n))));
  document.getElementById("midiTable").replaceChildren(table(r.rows, [
    ["n", "#"], ["song", "曲名"], ["preset", "プリセット"], ["folder", "フォルダ"], ["slot", "スロット"],
    ["cc0", "CC#0"], ["cc32", "CC#32"], ["pc", "PC(0始まり)"], ["pc_display", "PC(1始まり)"],
    ["channel", "ch"], ["note", "注意"],
  ]));
  document.getElementById("csv").href = `${base}?format=csv&channel=${ch}`;
  document.getElementById("md").href = `${base}?format=md&channel=${ch}`;
}

async function loadMapping() {
  const r = await api("/api/setlist/midi-config");
  const box = document.getElementById("mapping");
  box.replaceChildren(...folders.map(f => el("label", {}, f,
    el("input", { type: "number", min: 0, max: 12, style: "width: 4.5em", "data-folder": f, value: r.mapping[f] ?? "" }))));
}

async function init() {
  await renderHeader("setlist");
  const list = await api("/api/folders");
  folders = list.filter(f => f.kind !== "sandbox").map(f => f.name);
  document.getElementById("prefer").append(...folders.map(f => el("option", { value: f }, f)));
  const status = await api("/api/status");
  const allowed = new Set((status.policy && status.policy.allowed) || []);
  const dest = document.getElementById("dest");
  dest.append(el("option", { value: "" }, "sandbox(_qcsuite_sandbox)"));
  for (const name of ["Live", "Artist", "Song", "Workshop"]) {
    const key = `/media/p4/Presets/${name}`;
    dest.append(el("option", { value: name, disabled: !allowed.has(key) }, allowed.has(key) ? name : `${name}(未許可)`));
  }
  document.getElementById("btnBuild").addEventListener("click", async () => {
    const name = document.getElementById("name").value.trim();
    const text = document.getElementById("songs").value;
    if (!name || !text.trim()) { toast("名前と曲のリストを入れてください", true); return; }
    current = await api("/api/setlist/build", { method: "POST", body: { name, text, prefer_folder: document.getElementById("prefer").value } });
    draw(); loadSaved();
  });
  document.getElementById("btnPlan").addEventListener("click", async () => {
    const plan = await api(`/api/setlist/${encodeURIComponent(current.name)}/plan`, { method: "POST", body: {
      folder: dest.value, start: document.getElementById("start").value.trim(),
      overwrite: document.getElementById("overwrite").checked, naming: document.getElementById("naming").value } });
    showPlan(plan, async () => {
      current = await api(`/api/setlist/${encodeURIComponent(current.name)}/refresh`, { method: "POST" });
      draw();
    });
  });
  document.getElementById("btnMap").addEventListener("click", async () => {
    current = await api(`/api/setlist/${encodeURIComponent(current.name)}/map`, { method: "POST" });
    draw();
  });
  document.getElementById("btnRefresh").addEventListener("click", async () => {
    current = await api(`/api/setlist/${encodeURIComponent(current.name)}/refresh`, { method: "POST" });
    draw();
  });
  document.getElementById("btnMidi").addEventListener("click", midi);
  document.getElementById("btnMapping").addEventListener("click", async () => {
    const mapping = {};
    document.querySelectorAll("#mapping input").forEach(i => { if (i.value !== "") mapping[i.dataset.folder] = Number(i.value); });
    await api("/api/setlist/midi-config", { method: "POST", body: { mapping } });
    toast("保存しました"); loadMapping();
  });
  loadSaved(); loadMapping();
}
init();
