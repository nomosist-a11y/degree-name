// Loudness leveler page: lane check, measure, propose, apply to copies, verify.

const OPT_IN = ["Live", "Artist", "Song", "Workshop"];
const selected = new Map();   // "key#position" -> {folder, position, name}
let folders = [];

function recallWarning(count) {
  return window.confirm(`${count} 件のプリセットを本体に順に読み込みます。音が切り替わり、` +
    "本体で保存していない編集は消えます。続けますか?");
}

async function loadFolders() {
  folders = (await api("/api/folders")).filter(f => f.kind !== "captures");
  for (const id of ["folder", "ckFolder"]) {
    const sel = document.getElementById(id);
    sel.replaceChildren(...folders.map(f => el("option", { value: f.key }, `${f.name}(${f.occupied})`)));
  }
  const dest = document.getElementById("dest");
  const allowed = new Set((QC.status && QC.status.policy && QC.status.policy.allowed) || []);
  for (const name of OPT_IN) {
    const key = `/media/p4/Presets/${name}`;
    if (allowed.has(key)) dest.append(el("option", { value: key }, name));
  }
  await loadPresets();
  await loadCheckPresets();
}

async function loadPresets() {
  const key = document.getElementById("folder").value;
  const presets = await api(`/api/presets?folder=${encodeURIComponent(key)}`);
  const box = document.getElementById("presetList");
  box.replaceChildren(...presets.map(p => {
    const id = `${key}#${p.index}`;
    const cb = el("input", { type: "checkbox", checked: selected.has(id) });
    cb.addEventListener("change", () => {
      if (cb.checked) selected.set(id, { folder: key, position: p.index, name: p.name });
      else selected.delete(id);
      drawCount();
    });
    return el("div", {}, el("label", {}, cb, `${p.slot} ${p.name}`));
  }));
  drawCount();
}

async function loadCheckPresets() {
  const key = document.getElementById("ckFolder").value;
  const presets = await api(`/api/presets?folder=${encodeURIComponent(key)}`);
  const sel = document.getElementById("ckPreset");
  sel.replaceChildren(el("option", { value: "" }, "今のグリッドのまま"),
    ...presets.map(p => el("option", { value: p.index }, `${p.slot} ${p.name}`)));
}

function drawCount() {
  const whole = document.getElementById("wholeFolder").checked;
  document.getElementById("selCount").textContent = whole
    ? "選んだフォルダのすべてを測ります。" : `選択中: ${selected.size} 件(フォルダをまたいで選べます)`;
}

async function loadDis() {
  const dis = await api("/api/di");
  document.getElementById("di").replaceChildren(...dis.map(d =>
    el("option", { value: d.name }, `${d.name}${d.source === "synthetic" ? "(合成)" : ""}`)));
}

async function loadJobs() {
  const rows = await api("/api/leveler/jobs");
  const measure = rows.filter(j => j.kind === "level-measure");
  document.getElementById("measureJob").replaceChildren(...measure.map(j =>
    el("option", { value: j.id }, `${j.id}(${j.ok}/${j.total})`)));
  const sessions = await api("/api/leveler/sessions");
  document.getElementById("session").replaceChildren(...sessions.map(s =>
    el("option", { value: s.plan_id }, `${s.plan_id}(目標 ${fmt(s.target)} LUFS、${s.method || "lane"}、${s.entries.length} 件)`)));
}

function measureTable(results) {
  return table(results.map(m => ({
    ...m, folderName: m.folder.split("/").pop(),
    sceneText: Object.entries(m.scenes || {}).map(([s, v]) => `${s}:${fmt(v.lufs)}`).join(" "),
  })), [
    ["folderName", "フォルダ"], ["slot", "スロット"], ["name", "名前"],
    ["lufs", "LUFS", v => fmt(v)], ["true_peak_dbtp", "TP dBTP", v => fmt(v)],
    ["sceneText", "シーン別"],
    ["excluded", "除外", (v, r) => v ? el("span", { class: "danger" }, r.reasons.join(" / ")) : ""],
    ["file", "試聴", v => v ? el("audio", { controls: true, preload: "none", src: fileUrl(v) }) : ""],
  ]);
}

async function showMeasure(jobId) {
  if (!jobId) return;
  const data = await api(`/api/leveler/results/${jobId}`);
  document.getElementById("measureTable").replaceChildren(
    el("h4", {}, "測定結果"), measureTable(data.results));
}

function changesText(r) {
  const parts = [];
  for (const [row, v] of Object.entries(r.lanes)) parts.push(`行${Number(row) + 1} 出力 ${fmt(v.current_db)}→${fmt(v.new_db)} dB`);
  for (const [row, v] of Object.entries(r.gain_blocks)) parts.push(`行${Number(row) + 1} 列${v.col + 1} に Gain ${fmt(v.db)} dB`);
  for (const k of r.knobs) parts.push(`${k.model} ${k.param} ${fmt(k.current * 10)}→${fmt(k.new * 10)}`);
  return parts.join("、");
}

async function propose() {
  const body = { job: document.getElementById("measureJob").value || null,
    target: document.getElementById("target").value, method: document.getElementById("method").value };
  const p = await api("/api/leveler/propose", { method: "POST", body });
  document.getElementById("proposal").replaceChildren(
    el("h4", {}, `補正案: 目標 ${fmt(p.target)} LUFS(${p.target_source})、方法: ${p.method_label}`),
    table(p.rows.map(r => ({ ...r, changes: changesText(r), noteText: r.notes.join(" / ") })), [
      ["slot", "スロット"], ["name", "名前"], ["lufs", "測定", v => fmt(v)],
      ["delta_db", "補正 dB", v => v === null ? "—" : (v > 0 ? "+" : "") + fmt(v)],
      ["changes", "変更"], ["noteText", "メモ"],
    ]));
}

// Leveler plans run through the leveler's own executor (Gain-block and amp-knob
// adjustments are applied there), so this mirrors common.js showPlan.
function showLevelPlan(plan) {
  const dialog = el("dialog");
  const refused = plan.refused || [];
  const input = el("input", { placeholder: `「${plan.confirm_word || "実行"}」と入力` });
  const run = el("button", { class: "primary", disabled: refused.length > 0 }, "実行");
  run.addEventListener("click", async () => {
    try {
      const { job } = await api(plan.execute_url, { method: "POST", body: { confirm: input.value.trim() } });
      dialog.close(); dialog.remove();
      toast("実行を始めました。");
      watchJob(job, () => { toast("コピーを作りました。「4. 確認」で測り直せます。"); loadJobs(); });
    } catch (e) { /* shown */ }
  });
  dialog.append(el("h3", {}, plan.title), el("pre", {}, plan.text),
    refused.length ? el("div", { class: "warn" }, "拒否される手順があるため実行できません:",
      el("pre", {}, refused.join("\n"))) : null,
    el("div", { class: "warn" }, "実行中は本体のプリセットが切り替わり、音が変わります。元のプリセットは変更しません。"),
    el("div", { class: "row" }, input, run,
      el("button", { onclick: () => { dialog.close(); dialog.remove(); } }, "やめる")),
    el("p", { class: "small muted" }, `計画 ID: ${plan.id}`));
  document.body.append(dialog);
  dialog.showModal();
}

document.getElementById("folder").addEventListener("change", loadPresets);
document.getElementById("ckFolder").addEventListener("change", loadCheckPresets);
document.getElementById("wholeFolder").addEventListener("change", drawCount);
document.getElementById("measureJob").addEventListener("change", e => showMeasure(e.target.value));

document.getElementById("btnCheck").addEventListener("click", async () => {
  const position = document.getElementById("ckPreset").value;
  if (position !== "" && !recallWarning(1)) return;
  if (!confirmMonitor()) return;
  const body = { folder: position === "" ? null : document.getElementById("ckFolder").value,
    position: position === "" ? null : Number(position), di: document.getElementById("di").value };
  const { job } = await api("/api/leveler/check-lane", { method: "POST", body });
  watchJob(job, detail => {
    const r = (detail.results.find(x => x.ok) || {}).result;
    if (!r) return;
    document.getElementById("checkResult").replaceChildren(
      el("p", { class: r.effective ? "okc" : "danger" },
        `${fmt(r.delta_db)} dB 書き込み → 音量の変化 ${fmt(r.measured_change_db, 2)} dB: ${r.verdict}` +
        `(元の値に戻しました)${r.mock ? "(モック)" : ""}`),
      el("p", { class: "small muted" }, `推奨する方法: ${r.recommended_method === "lane" ? "行の出力レベル" : "Gain ブロックを追加"}`));
    document.getElementById("method").value = r.recommended_method;
  });
});

document.getElementById("btnMeasure").addEventListener("click", async () => {
  const whole = document.getElementById("wholeFolder").checked;
  const folder = document.getElementById("folder").value;
  const body = { presets: whole ? [] : [...selected.values()].map(p => ({ folder: p.folder, position: p.position })),
    folders: whole ? [folder] : [], di: document.getElementById("di").value,
    scenes: document.getElementById("scenes").value || null };
  const count = whole ? (folders.find(f => f.key === folder) || {}).occupied : body.presets.length;
  if (!count) { toast("測るプリセットを選んでください", true); return; }
  if (!recallWarning(count) || !confirmMonitor()) return;
  const { job } = await api("/api/leveler/measure", { method: "POST", body });
  watchJob(job, async () => { await loadJobs(); document.getElementById("measureJob").value = job; showMeasure(job); });
});

document.getElementById("btnPropose").addEventListener("click", propose);

document.getElementById("btnPlan").addEventListener("click", async () => {
  const body = { job: document.getElementById("measureJob").value || null,
    target: document.getElementById("target").value, method: document.getElementById("method").value,
    to: document.getElementById("dest").value || null };
  const plan = await api("/api/leveler/plan", { method: "POST", body });
  await loadJobs();
  showLevelPlan(plan);
});

document.getElementById("btnVerify").addEventListener("click", async () => {
  const session = document.getElementById("session").value;
  if (!session) { toast("適用の記録がありません", true); return; }
  if (!window.confirm("コピーしたプリセットを本体に順に読み込みます。音が切り替わります。続けますか?") ||
      !confirmMonitor()) return;
  const { job } = await api("/api/leveler/verify", { method: "POST",
    body: { session, di: document.getElementById("di").value } });
  watchJob(job, async () => {
    const v = await api(`/api/leveler/verify/${job}`);
    document.getElementById("verify").replaceChildren(
      el("p", {}, `目標 ${fmt(v.target)} LUFS、±${v.goal_lu} LU 以内: ${v.within_goal}/${v.count}` +
        (v.mock ? "(モックでの結果。実機は未確認)" : "")),
      table(v.rows, [["slot", "スロット"], ["name", "名前"], ["lufs", "LUFS", x => fmt(x)],
        ["residual_lu", "目標との差 LU", x => fmt(x, 2)],
        ["within_goal", "判定", x => x ? el("span", { class: "okc" }, "OK") : el("span", { class: "danger" }, "要確認")]]));
  });
});

renderHeader("leveler").then(async () => {
  await api("/api/connect", { method: "POST" });
  QC.status = await api("/api/status");
  await Promise.all([loadFolders(), loadDis(), loadJobs()]);
  showMeasure(document.getElementById("measureJob").value);
});
