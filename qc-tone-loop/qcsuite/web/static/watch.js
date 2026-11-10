// Edit watcher: start/stop, live changes, notes, linked takes, session exports.

let timer = null;

function entryText(e) {
  switch (e.type) {
    case "change": {
      const merged = e.polls > 1 ? `(${e.polls} 回の操作をまとめた)` : "";
      return `${e.where}: ${e.before ?? "—"} → ${e.after ?? "—"}${merged}`;
    }
    case "scene": return `シーン ${e.before} → ${e.after}`;
    case "segment": return `区間 ${e.segment}: 「${e.preset}」${e.note ? " — " + e.note : ""}`;
    case "note": return `メモ: ${e.text}`;
    case "take": return `録音「${e.di}」${e.seconds} 秒(ずれ ${fmt(e.offset_ms)} ms、「${e.preset}」)`;
    case "start": return `記録開始: 「${e.preset}」シーン ${e.scene}`;
    case "stop": return "記録終了";
    case "pause": return `一時停止: ${e.reason || ""}`;
    case "error": return `読み取りに失敗: ${e.error}`;
    default: return JSON.stringify(e);
  }
}

function entryList(entries, pending) {
  const rows = [...(entries || []), ...(pending || []).map(p => ({ ...p, _pending: true }))];
  if (!rows.length) return el("p", { class: "muted" }, "(まだありません)");
  return el("ul", {}, rows.slice().reverse().map(e => el("li", {},
    el("span", { class: "muted" }, `${(e.last_t || e.t || "").slice(11)} `),
    entryText(e),
    e._pending ? el("span", { class: "muted" }, "(動かし中)") : null,
    e.manual ? el("span", { class: "danger" }, " ※本体で行った変更(パソコンからは再現できない可能性)") : null)));
}

async function refresh() {
  const s = await api("/api/watch/status");
  const status = document.getElementById("status");
  if (s.running) {
    const paused = s.paused === "job" ? "(ジョブの実行中は記録を止めています)" :
      s.paused === "busy" ? "(本体が使用中のため 1 回飛ばしました)" : "";
    status.textContent = `記録中: ${s.session} / 区間 ${s.segment} / 最後の読み取り ${s.last_poll || "—"} ${paused}` +
      (s.error ? ` / エラー: ${s.error}` : "");
    status.className = "small";
  } else {
    status.textContent = s.session ? `記録していません(最後のセッション ${s.session})` : "記録していません。";
    status.className = "small muted";
  }
  document.getElementById("entries").replaceChildren(entryList(s.entries, s.pending));
  if (s.running && !timer) timer = setInterval(refresh, 1500);
  if (!s.running && timer) { clearInterval(timer); timer = null; }
}

async function drawTakes() {
  const takes = await api("/api/watch/takes");
  document.getElementById("takes").replaceChildren(takes.length ? table(takes, [
    ["di", "名前"], ["preset", "プリセット"], ["scene", "シーン"], ["seconds", "秒"],
    ["offset_ms", "ずれ ms", v => fmt(v)], ["t", "日時"],
    ["di_file", "DI", v => el("audio", { controls: true, preload: "none", src: fileUrl(v) })],
    ["wet_file", "ウェット", v => el("audio", { controls: true, preload: "none", src: fileUrl(v) })],
    ["knobs", "ノブ", v => el("details", {}, el("summary", {}, "表示"),
      el("pre", {}, Object.entries(v || {}).map(([b, k]) =>
        `${b}\n` + Object.entries(k).map(([n, x]) => `  ${n}: ${x}`).join("\n")).join("\n")))],
  ]) : el("p", { class: "muted small" }, "(録音はまだありません)"));
}

// Like showPlan, but executed through the watcher's endpoint so the source
// preset is recalled and the changes replayed (instead of a JSON rebuild).
function showWatchPlan(plan) {
  const dialog = el("dialog");
  const refused = plan.refused || [];
  const input = el("input", { placeholder: `「${plan.confirm_word || "実行"}」と入力` });
  const run = el("button", { class: plan.destructive ? "danger" : "primary", disabled: refused.length > 0 }, "実行");
  run.addEventListener("click", async () => {
    const { job } = await api(`/api/watch/plans/${plan.id}/execute`, {
      method: "POST", body: { confirm: input.value.trim() } });
    dialog.close(); dialog.remove();
    watchJob(job, () => toast("sandbox に保存しました"));
  });
  dialog.append(el("h3", {}, plan.title), el("pre", {}, plan.text),
    refused.length ? el("div", { class: "warn" }, el("pre", {}, refused.join("\n"))) : null,
    el("div", { class: "row" }, input, run,
      el("button", { onclick: () => { dialog.close(); dialog.remove(); } }, "やめる")),
    el("p", { class: "small muted" }, `計画 ID: ${plan.id}(実行するまで何も変わりません)`));
  document.body.append(dialog);
  dialog.showModal();
}

async function openSession(id) {
  const d = await api(`/api/watch/sessions/${id}`);
  const md = await (await fetch(`/api/watch/sessions/${id}/markdown`)).text();
  const segments = d.summary.segments.map(s => s.n);
  const segSel = el("select", {}, segments.map(n => el("option", { value: n, selected: n === segments[segments.length - 1] }, `区間 ${n}`)));
  const source = el("input", { placeholder: "元のプリセット(例 kami:1E、省略可)" });
  const out = el("pre", { class: "small" });
  document.getElementById("detail").replaceChildren(
    el("h3", {}, `セッション ${id}`),
    el("div", { class: "row" }, segSel,
      el("button", { onclick: async () => {
        const r = await api(`/api/watch/sessions/${id}/patch?segment=${segSel.value}`);
        out.textContent = JSON.stringify(r, null, 1);
      } }, "再現用パッチを見る"),
      el("button", { onclick: async () => {
        if (!confirm("区間の数値の変更を、いまのグリッドに適用します(保存はしません)。")) return;
        const r = await api(`/api/watch/sessions/${id}/apply`, { method: "POST", body: { segment: Number(segSel.value) } });
        toast(`${r.applied} 件を適用しました`);
        if (r.manual && r.manual.length) out.textContent = "本体で手作業が必要:\n" + r.manual.join("\n");
      } }, "グリッドに適用"),
      source,
      el("button", { onclick: async () => {
        const plan = await api(`/api/watch/sessions/${id}/to-sandbox`, { method: "POST",
          body: { segment: Number(segSel.value), source: source.value.trim() || null } });
        showWatchPlan(plan);
      } }, "sandbox に保存(計画)"),
      el("a", { class: "btn", href: `/api/watch/sessions/${id}/markdown`, download: `watch-${id}.md` }, "Markdown を保存")),
    out,
    el("pre", { class: "small" }, md));
}

async function drawSessions() {
  const rows = await api("/api/watch/sessions");
  document.getElementById("sessions").replaceChildren(rows.length ? table(rows.map(r => ({
    ...r, preset: r.segments.length ? r.segments[0].preset : "", changes: r.counts.change,
    notes: r.counts.note, takes: r.counts.take, state: r.running ? "記録中" : "終了" })), [
    ["id", "ID"], ["started", "開始"], ["state", "状態"], ["preset", "プリセット"],
    ["changes", "変更"], ["notes", "メモ"], ["takes", "録音"],
  ], { onRow: r => openSession(r.id) }) : el("p", { class: "muted" }, "(記録はまだありません)"));
}

document.getElementById("btnStart").addEventListener("click", async () => {
  await api("/api/watch/start", { method: "POST", body: { interval: Number(document.getElementById("interval").value) } });
  toast("記録を始めました。本体を操作してください。");
  refresh(); drawSessions();
});
document.getElementById("btnStop").addEventListener("click", async () => {
  const s = await api("/api/watch/stop", { method: "POST" });
  toast(`記録を終えました(変更 ${s.counts.change} 件)`);
  refresh(); drawSessions();
});
document.getElementById("btnNote").addEventListener("click", async () => {
  const box = document.getElementById("noteText");
  if (!box.value.trim()) return;
  await api("/api/watch/note", { method: "POST", body: { text: box.value.trim() } });
  box.value = ""; toast("メモを残しました"); refresh();
});
document.getElementById("btnTake").addEventListener("click", async () => {
  const name = document.getElementById("takeName").value.trim();
  if (!name) { toast("録音の名前を入れてください", true); return; }
  const seconds = Number(document.getElementById("takeSec").value);
  toast(`${seconds} 秒録音します。弾いてください。`);
  const t = await api("/api/watch/take", { method: "POST", body: {
    name, seconds, note: document.getElementById("takeNote").value } });
  toast(`録音しました: ${t.di}`);
  drawTakes(); refresh();
});

renderHeader("watch").then(() => { refresh(); drawTakes(); drawSessions(); });
