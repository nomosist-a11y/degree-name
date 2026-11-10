const BK = { presets: [], picked: [], log: [], current: null, warning: "", lastJob: null };
const $ = id => document.getElementById(id);

async function loadStatus() {
  const s = await api("/api/backup/status");
  BK.warning = s.recall_warning;
  BK.lastJob = s.last_job;
  $("recallWarn").textContent = s.recall_warning;
  const last = s.last_commit;
  $("status").replaceChildren(
    el("div", {}, `保存先: ${s.repo}`),
    el("div", {}, `プリセット: ${s.presets} 件 ` + Object.entries(s.folders).map(([k, v]) => `${k} ${v}`).join(" / ")),
    el("div", {}, last ? `最新: ${last.date} ${last.short} ${last.message}` : "まだバックアップがありません。"),
    s.last_job ? el("div", {}, `直近のジョブ: ${s.last_job.id} ${s.last_job.status} ${s.last_job.message || ""}`) : null);
  const resumable = s.last_job && ["interrupted", "cancelled", "failed"].includes(s.last_job.status);
  $("btnResume").hidden = !resumable;
}

async function loadFolders() {
  const folders = await api("/api/folders");
  $("folders").replaceChildren(...folders.filter(f => f.kind === "user").map(f =>
    el("label", {}, el("input", { type: "checkbox", value: f.key, checked: true }), `${f.name}(${f.occupied})`)));
  const policy = (await api("/api/status")).policy || { allowed: [] };
  $("dest").replaceChildren(el("option", { value: "" }, "sandbox(_qcsuite_sandbox)"),
    ...policy.allowed.map(k => el("option", { value: k }, k.split("/").pop())));
}

function startJob(body) {
  if (!confirm(`${BK.warning}\n\n始めますか?`)) return;
  api("/api/backup/run", { method: "POST", body }).then(({ job }) => {
    watchJob(job, () => { loadStatus(); loadLog(); loadPresets(); });
  });
}

$("btnRun").addEventListener("click", () => {
  const folders = [...document.querySelectorAll("#folders input:checked")].map(i => i.value);
  if (!folders.length && !$("factory").checked) { toast("フォルダを選んでください", true); return; }
  if ($("factory").checked) folders.push("/opt/neuraldsp/Factory Library");
  startJob({ folders });
});
$("btnResume").addEventListener("click", () => startJob({ resume: BK.lastJob.id }));

async function loadLog() {
  const rows = await api("/api/backup/log");
  BK.log = rows;
  BK.picked = [];
  if (!rows.length) { $("log").replaceChildren(el("p", { class: "muted" }, "(まだありません)")); return; }
  $("log").replaceChildren(table(rows, [
    ["commit", "比較", (v) => {
      const cb = el("input", { type: "checkbox" });
      cb.addEventListener("change", () => {
        BK.picked = cb.checked ? [...BK.picked, v].slice(-2) : BK.picked.filter(x => x !== v);
      });
      return cb;
    }],
    ["date", "日時"], ["short", "コミット"], ["message", "内容"],
  ]));
}

async function showDiff(a, b) {
  const params = new URLSearchParams();
  if (a) params.set("a", a);
  if (b) params.set("b", b);
  const d = await api(`/api/backup/diff?${params}`);
  $("diff").textContent = d.text + (d.stat ? `\n\n${d.stat}` : "");
}
$("btnDiffLast").addEventListener("click", () => showDiff());
$("btnDiffSel").addEventListener("click", () => {
  if (BK.picked.length !== 2) { toast("比べるコミットを 2 つ選んでください", true); return; }
  // The log is newest first: the commit further down is the older one.
  const [a, b] = BK.picked.slice().sort((x, y) =>
    BK.log.findIndex(r => r.commit === y) - BK.log.findIndex(r => r.commit === x));
  showDiff(a, b);
});

async function loadPresets() {
  BK.presets = await api("/api/backup/presets");
  drawPresets();
}

function drawPresets() {
  const q = $("filter").value.trim().toLowerCase();
  const rows = BK.presets.filter(p => !q || p.name.toLowerCase().includes(q))
    .map(p => ({ ...p, folderName: (p.folder || "").split("/").pop() }));
  if (!rows.length) { $("presets").replaceChildren(el("p", { class: "muted" }, "(該当なし)")); return; }
  $("presets").replaceChildren(table(rows, [
    ["folderName", "フォルダ"], ["slot", "スロット"],
    ["name", "名前", (v, r) => el("a", { href: "#detailPanel", onclick: () => pick(r) }, v)],
    ["blocks", "ブロック数"],
  ]), el("p", { class: "small muted" }, `${rows.length} 件`));
}
$("filter").addEventListener("input", drawPresets);

async function pick(r) {
  BK.current = r;
  const [view, history] = await Promise.all([
    api(`/api/backup/show?ref=${encodeURIComponent(r.file)}`),
    api(`/api/backup/log?preset=${encodeURIComponent(r.file)}`),
  ]);
  $("detail").replaceChildren(
    el("strong", {}, `${r.folderName} ${r.slot}「${r.name}」`),
    chainView(view.preset),
    el("div", {}, `このプリセットの履歴: ${history.length} 回`),
    table(history, [["date", "日時"], ["short", "コミット"], ["commit", "この時点と今を比べる", v =>
      el("button", { class: "small", onclick: async () => {
        const d = await api(`/api/backup/diff?a=${v}&preset=${encodeURIComponent(r.file)}`);
        $("diff").textContent = d.text; $("diff").scrollIntoView();
      } }, "比べる")]]));
  $("btnRestore").disabled = false;
}

$("btnRestore").addEventListener("click", async () => {
  if (!BK.current) return;
  const plan = await api("/api/backup/restore", { method: "POST", body: {
    ref: BK.current.file, to: $("dest").value, slot: $("slot").value.trim(), name: $("name").value.trim() } });
  showPlan(plan, () => toast("復元しました(sandbox を本体で確認してください)"));
});

renderHeader("backup").then(async () => {
  await loadStatus();
  await loadFolders();
  loadLog();
  loadPresets();
});
