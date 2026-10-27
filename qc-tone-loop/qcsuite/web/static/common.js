// Shared helpers for every qcsuite page: header and nav, API calls, toasts,
// job progress, plan confirmation and the monitor-volume warning.

const PAGES = [
  ["index", "ホーム"], ["library", "プリセット管理"], ["backup", "バックアップ"],
  ["leveler", "音量そろえ"], ["tournament", "ブラインド比較"], ["panel", "言葉で操作"],
  ["sweep", "スイープ"], ["sheets", "資料"], ["setlist", "セットリスト"],
  ["batch", "一括リアンプ"], ["health", "健康診断"], ["watch", "編集ウォッチャー"],
  ["remote", "リモコン"], ["jobs", "ジョブ"],
];

const QC = { status: null };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  const res = await fetch(path, opts);
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) {
    const message = (data && (data.error || data.detail)) || `${res.status} ${res.statusText}`;
    toast(message, true);
    throw new Error(message);
  }
  return data;
}

function toast(message, error = false) {
  let box = document.getElementById("toast");
  if (!box) { box = el("div", { id: "toast" }); document.body.append(box); }
  const item = el("div", { class: error ? "error" : "" }, message);
  box.append(item);
  setTimeout(() => item.remove(), error ? 9000 : 4500);
}

function fileUrl(path) {
  return `/api/files?path=${encodeURIComponent(path)}`;
}

function fmt(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return typeof v === "number" ? v.toFixed(digits) : String(v);
}

async function renderHeader(active) {
  const header = el("header", { class: "top" },
    el("h1", {}, el("a", { href: "/static/index.html" }, "qcsuite")),
    el("span", { id: "mode", class: "badge" }, "…"),
    el("nav", { class: "main" }, PAGES.map(([id, label]) =>
      el("a", { href: `/static/${id}.html`, class: id === active ? "active" : "" }, label))));
  document.body.prepend(header);
  try {
    QC.status = await api("/api/status");
    const mode = document.getElementById("mode");
    if (QC.status.mode === "mock") {
      mode.textContent = "モック(実機なし)"; mode.className = "badge mock";
    } else {
      mode.textContent = QC.status.connected ? "実機に接続中" : "実機(未接続)";
      mode.className = "badge hw";
    }
  } catch (e) { /* toast already shown */ }
  return QC.status;
}

// Reamping makes the unit produce sound: ask before every run on hardware.
function confirmMonitor() {
  if (QC.status && QC.status.mode === "mock") return true;
  const text = (QC.status && QC.status.monitor_warning) ||
    "リアンプ中は Quad Cortex から音が出ます。モニターの音量を下げてください。";
  return window.confirm(`${text}\n\n準備ができたら OK を押してください。`);
}

// Shows a plan (from any feature's "plan" endpoint) and runs it on confirmation.
function showPlan(plan, onDone) {
  const dialog = el("dialog");
  const refused = plan.refused || [];
  const input = el("input", { placeholder: plan.confirm_word ? `「${plan.confirm_word}」と入力` : "" });
  const run = el("button", { class: plan.destructive ? "danger" : "primary", disabled: refused.length > 0 },
    plan.destructive ? "バックアップしてから実行" : "実行");
  run.addEventListener("click", async () => {
    try {
      const { job } = await api(`/api/plans/${plan.id}/execute`, {
        method: "POST", body: { confirm: plan.confirm_word ? input.value.trim() : "実行" } });
      dialog.close(); dialog.remove();
      toast("実行を始めました。");
      watchJob(job, onDone);
    } catch (e) { /* shown */ }
  });
  dialog.append(
    el("h3", {}, plan.title),
    el("pre", {}, plan.text),
    refused.length ? el("div", { class: "warn" }, "拒否される手順があるため実行できません:", el("pre", {}, refused.join("\n"))) : null,
    plan.changes_sound ? el("div", { class: "warn" }, "実行中は本体のプリセットが切り替わり、音が変わります。") : null,
    el("div", { class: "row" },
      plan.confirm_word ? input : null, run,
      el("button", { onclick: () => { dialog.close(); dialog.remove(); } }, "やめる")),
    el("p", { class: "small muted" }, `計画 ID: ${plan.id}(dry-run は保存済み。実行するまで何も変わりません)`));
  document.body.append(dialog);
  dialog.showModal();
}

// Polls a job and keeps a progress bar up to date; calls onDone(detail).
function watchJob(jobId, onDone, container) {
  const host = container || document.getElementById("jobbox") || document.querySelector("main");
  const bar = el("div", { class: "progress" }, el("div"));
  const label = el("div", { class: "small muted" }, `ジョブ ${jobId}`);
  const cancel = el("button", { class: "small" }, "中断");
  const box = el("div", { class: "panel" }, label, bar, el("div", { class: "row" }, cancel));
  host.prepend(box);
  cancel.addEventListener("click", () => api(`/api/jobs/${jobId}/cancel`, { method: "POST" }));
  const tick = async () => {
    let detail;
    try { detail = await api(`/api/jobs/${jobId}`); } catch { return; }
    const s = detail.summary;
    const pct = s.total ? Math.round(100 * (s.done || 0) / s.total) : 0;
    bar.firstChild.style.width = `${pct}%`;
    const eta = s.eta_s ? ` 残り約 ${Math.ceil(s.eta_s / 60)} 分` : "";
    label.textContent = `${s.title || s.kind}: ${s.status} ${s.done || 0}/${s.total || 0}` +
      `${s.failed ? `(失敗 ${s.failed})` : ""}${eta} ${s.current || ""} ${s.message || ""}`;
    if (["running", "new"].includes(s.status)) { setTimeout(tick, 800); return; }
    cancel.remove();
    if (s.errors && s.errors.length) toast(`失敗: ${s.errors[s.errors.length - 1].error}`, true);
    if (onDone) onDone(detail);
  };
  tick();
}

function notices(list) {
  return el("ul", { class: "small" }, (list || []).slice().reverse().map(n =>
    el("li", {}, `${n.at} `, el("span", { class: n.kind === "inert_write" || n.kind === "sound_change" ? "danger" : "muted" },
      { sound_change: "[音が変わる] ", inert_write: "[鳴らない書き込み] ", retry: "[再試行] " }[n.kind] || ""), n.message)));
}

// A simple sortable table: columns = [[key, label, formatter?]].
function table(rows, columns, { onRow, selected } = {}) {
  let sortKey = null, asc = true;
  const tbody = el("tbody");
  const draw = () => {
    tbody.replaceChildren();
    const data = rows.slice();
    if (sortKey) data.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      const r = (typeof x === "number" && typeof y === "number") ? x - y : String(x ?? "").localeCompare(String(y ?? ""), "ja");
      return asc ? r : -r;
    });
    for (const r of data) {
      const tr = el("tr", { class: selected && selected(r) ? "sel" : "" },
        columns.map(([k, , f]) => el("td", {}, f ? f(r[k], r) : (r[k] ?? ""))));
      if (onRow) tr.addEventListener("click", () => { onRow(r); draw(); });
      tbody.append(tr);
    }
  };
  const thead = el("thead", {}, el("tr", {}, columns.map(([k, label]) => el("th", {
    class: "sortable", onclick: () => { asc = sortKey === k ? !asc : true; sortKey = k; draw(); } }, label))));
  draw();
  return el("div", { class: "scroll" }, el("table", {}, thead, tbody));
}

function chainView(n) {
  // n: a normalized preset (qcsuite.preset/1)
  const out = el("div");
  for (const r of n.rows) {
    const blocks = n.blocks.filter(b => b.row === r.row);
    if (!blocks.length && r.in_port === "EMPTY") continue;
    const line = el("div", { class: "chain" }, el("span", { class: "badge" }, `行${r.row + 1} ${r.in_port}`));
    for (const b of blocks) {
      const by = b.bypass && b.bypass.scenes && b.bypass.scenes.every(Boolean);
      line.append(el("span", { class: "arrow" }, "→"), el("span", {
        class: `blk ${by ? "bypassed" : ""} ${b.inert_plugin || b.capture ? "inert" : ""}`,
        title: Object.entries(b.params).filter(([, v]) => !("text" in v)).map(([k, v]) =>
          `${k}: ${v.option ?? v.real ?? v.value}`).join("\n") }, b.model + (b.capture ? ` «${b.capture}»` : "")));
    }
    const lane = r.lane && r.lane.VOLUME ? ` ${fmt(r.lane.VOLUME.db)} dB` : "";
    line.append(el("span", { class: "arrow" }, "→"), el("span", { class: "badge" }, `${r.out_port}${lane}`));
    out.append(line);
  }
  return out;
}
