const SEV_CLASS = { "要対応": "danger", "注意": "", "情報": "muted" };

function drawReport(res) {
  const r = res.report;
  document.getElementById("summary").textContent =
    `${r.at} / ${r.source}: ${r.scanned} 件 — 要対応 ${r.counts["要対応"]}、注意 ${r.counts["注意"]}、情報 ${r.counts["情報"]}`;
  const box = document.getElementById("report");
  box.replaceChildren();
  for (const p of r.presets) {
    if (!p.issues.length) continue;
    box.append(el("div", { class: "panel" },
      el("strong", {}, `${p.name}`), el("span", { class: "muted small" }, ` ${p.where}`),
      el("ul", { class: "small" }, p.issues.map(i => el("li", {},
        el("span", { class: SEV_CLASS[i.severity] }, `[${i.severity}] `), i.message,
        el("div", { class: "muted" }, `→ ${i.fix}`))))));
  }
  const clean = r.presets.filter(p => !p.issues.length);
  if (clean.length) box.append(el("p", { class: "small okc" }, `問題なし: ${clean.map(p => p.name).join(", ")}`));
}

async function scan() {
  const source = document.querySelector("input[name=src]:checked").value;
  const body = { source };
  if (source === "folder") {
    body.folder = document.getElementById("folder").value;
    if (!confirm(`「${body.folder}」のうちバックアップに無いプリセットを本体に 1 つずつ読み込みます。音が切り替わり、本体で保存していない編集は消えます。続けますか?`)) return;
    const { job } = await api("/api/health/scan", { method: "POST", body });
    watchJob(job, async () => drawReport(await api(`/api/health/job/${job}`)));
    return;
  }
  if (source === "refs") {
    body.refs = document.getElementById("refs").value.split(",").map(x => x.trim()).filter(Boolean);
    if (!body.refs.length) { toast("フォルダ:スロットを入れてください", true); return; }
    if (!confirm("バックアップに無いものは本体に読み込みます(音が切り替わります)。続けますか?")) return;
  }
  drawReport(await api("/api/health/scan", { method: "POST", body }));
}

async function gig() {
  const name = document.getElementById("gigName").value;
  if (!name) { toast("セットリストがありません(「セットリスト」のページで作成)", true); return; }
  const noRead = document.getElementById("noRead").checked;
  if (!noRead && !confirm("バックアップに無いプリセットは本体に読み込みます(音が切り替わります)。続けますか?")) return;
  const g = await api(`/api/health/gig/${encodeURIComponent(name)}?no_read=${noRead}`);
  const mark = ok => ok === true ? "✔" : ok === false ? "✘" : "?";
  document.getElementById("gig").replaceChildren(
    el("p", { class: g.ok ? "okc" : "danger" }, g.ok ? "すべて OK" : "確認が必要な曲があります"),
    el("p", { class: "small muted" }, g.median_lufs !== null
      ? `音量の中央値 ${fmt(g.median_lufs)} LUFS(±1 LU 以内を OK)` : "音量はまだ測っていません(「音量そろえ」で測定)"),
    ...g.songs.map(s => el("div", { class: "panel" },
      el("strong", {}, `${s.n}. ${s.song}`), el("span", { class: "muted small" }, ` ${s.where || "未解決"} — ${s.status}`),
      el("ul", { class: "small" }, s.checks.map(c => el("li", { class: c.ok === false ? "danger" : c.ok === null ? "muted" : "" },
        `${mark(c.ok)} ${c.item}${c.detail ? ": " + c.detail : ""}`))))),
    el("p", { class: "small muted" }, "音の判断(この音で良いか)はチェックしていません。リハーサルで耳で確かめてください。"));
}

async function init() {
  await renderHeader("health");
  const folders = await api("/api/folders");
  document.getElementById("folder").replaceChildren(...folders.map(f => el("option", { value: f.name }, f.name)));
  const lists = await api("/api/setlist/list");
  document.getElementById("gigName").replaceChildren(...lists.map(s => el("option", { value: s.name }, s.name)));
  const last = await api("/api/health/last");
  if (last.report) drawReport(last);
  document.getElementById("btnScan").addEventListener("click", scan);
  document.getElementById("btnGig").addEventListener("click", gig);
}
init();
