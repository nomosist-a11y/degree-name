// 言葉で操作するパネル: grid view, words -> patch preview, apply, undo, A/B.

let lastPreview = null;

// replaceChildren with nested arrays flattened and empty entries dropped.
function fill(node, ...items) {
  node.replaceChildren(...items.flat(2).filter(x => x !== null && x !== undefined && x !== false));
}

async function loadGrid() {
  const view = await api("/api/panel/grid");
  document.getElementById("scene").textContent = `シーン ${view.active_scene}`;
  const box = document.getElementById("grid");
  box.replaceChildren();
  for (const r of view.rows) {
    const used = r.cells.some(Boolean);
    if (!used && !r.is_input) continue;
    const lane = r.lane_db === null ? "" : ` / 出力 ${r.lane_db > 0 ? "+" : ""}${fmt(r.lane_db)} dB`;
    box.append(el("div", { class: "rowhead" },
      `行${r.row + 1}: 入力 ${r.in_port} → 出力 ${r.out_port}${r.reaches_output ? "" : "(Out 1/2 に届かない)"}${lane}`));
    box.append(el("div", { class: "cells" }, r.cells.map((c, col) => {
      if (!c) return el("div", { class: "cell" }, el("span", { class: "muted" }, `列${col + 1}`));
      const knobs = c.knobs.filter(k => !k.text).map(k =>
        el("div", { class: "kn" }, `${k.name}: ${k.display}${k.switch ? " (スイッチ)" : ""}`));
      return el("div", {
        class: `cell on ${c.flags.length && !c.responds ? "flagged" : ""} ${c.bypassed ? "off" : ""}`,
        title: c.flags.join("\n"),
      }, el("div", { class: "name" }, `列${col + 1} ${c.model}`),
        c.bypassed ? el("div", { class: "kn" }, "(バイパス)") : null,
        knobs,
        c.flags.map(f => el("div", { class: "kn danger" }, `! ${f}`)));
    })));
  }
}

async function loadEngines() {
  const e = await api("/api/panel/engines");
  const note = document.getElementById("engineNote");
  const opt = document.querySelector('#engine option[value="claude"]');
  if (!e.claude.available) {
    opt.textContent = "Claude(使えません)";
    note.textContent = e.claude.reason;
  } else {
    note.textContent = `Claude: ${e.claude.model}`;
  }
}

function showPreview(p) {
  lastPreview = p;
  const list = document.getElementById("summary");
  fill(list,
    p.fallback ? el("li", { class: "warn" }, p.fallback) : null,
    p.summary.map(s => el("li", {}, s)),
    p.warnings.map(w => el("li", { class: "warn" }, `注意: ${w}`)),
    p.problems.map(x => el("li", { class: "problem" }, `× ${x}`)),
    !p.patch.ops.length && !p.problems.length ? el("li", { class: "muted" }, "(変更はありません)") : null,
    el("li", { class: p.can_apply ? "okc" : "muted" },
      p.can_apply ? "内容を確認して「本体に送る」を押してください。" : "何も送りません。"));
  document.getElementById("patch").textContent = JSON.stringify(p.patch, null, 1);
  document.getElementById("btnApply").disabled = !p.can_apply;
}

async function loadHistory() {
  const rows = await api("/api/panel/history");
  document.getElementById("history").replaceChildren(rows.length ? table(rows.slice().reverse(), [
    ["at", "時刻"], ["instruction", "指示"], ["engine", "変換"],
    ["summary", "内容", v => (v || []).join(" / ")],
    ["undone", "状態", v => v ? "取り消し済み" : "適用中"],
  ]) : el("p", { class: "muted small" }, "(まだありません)"));
}

async function loadAB() {
  const ab = await api("/api/panel/ab");
  for (const slot of ["A", "B"]) {
    const btn = document.getElementById(`ab${slot}`);
    btn.className = ab.active === slot ? "on" : "";
    btn.disabled = !ab.slots[slot];
  }
  const info = document.getElementById("abInfo");
  fill(info,
    ["A", "B"].map(slot => el("div", {}, `${slot}: `,
      ab.slots[slot] ? `${ab.slots[slot].label}(${ab.slots[slot].at})` : "(未記録)")),
    ab.diff ? el("details", { open: true }, el("summary", {}, `A と B の違い(${ab.diff.length} 件)`),
      el("ul", {}, ab.diff.map(d => el("li", {}, d)))) : null);
}

async function refreshAll() {
  await Promise.all([loadGrid(), loadHistory(), loadAB()]);
}

document.getElementById("btnPreview").addEventListener("click", async () => {
  const instruction = document.getElementById("instruction").value.trim();
  if (!instruction) { toast("指示を入れてください", true); return; }
  showPreview(await api("/api/panel/preview", { method: "POST",
    body: { instruction, engine: document.getElementById("engine").value } }));
});

document.getElementById("btnApply").addEventListener("click", async () => {
  if (!lastPreview || !lastPreview.can_apply) return;
  const r = await api("/api/panel/apply", { method: "POST", body: {
    patch: lastPreview.patch, instruction: lastPreview.instruction,
    engine: lastPreview.engine, summary: lastPreview.summary } });
  toast(`送りました(${r.applied} 件)。${r.note}`);
  (r.warnings || []).forEach(w => toast(w, true));
  document.getElementById("btnApply").disabled = true;
  lastPreview = null;
  refreshAll();
});

document.getElementById("btnUndo").addEventListener("click", async () => {
  const r = await api("/api/panel/undo", { method: "POST" });
  toast(`取り消しました: ${r.instruction}`);
  (r.notes || []).forEach(n => toast(n));
  refreshAll();
});
document.getElementById("btnRedo").addEventListener("click", async () => {
  const r = await api("/api/panel/redo", { method: "POST" });
  toast(`やり直しました: ${r.instruction}`);
  refreshAll();
});

for (const slot of ["A", "B"]) {
  document.getElementById(`cap${slot}`).addEventListener("click", async () => {
    await api("/api/panel/ab/capture", { method: "POST", body: { slot } });
    toast(`いまの状態を ${slot} に記録しました`); loadAB();
  });
  document.getElementById(`ab${slot}`).addEventListener("click", async () => {
    const r = await api("/api/panel/ab/switch", { method: "POST", body: { slot } });
    toast(`${slot} にしました(${r.applied} 件の操作)`);
    (r.notes || []).forEach(n => toast(n));
    loadGrid(); loadAB();
  });
}
document.getElementById("abLast").addEventListener("click", async () => {
  await api("/api/panel/ab/last", { method: "POST" });
  toast("直前の変更の前を A、後を B にしました(本体はいま B)"); loadAB();
});

document.getElementById("btnRefresh").addEventListener("click", loadGrid);

document.getElementById("btnSave").addEventListener("click", async () => {
  const name = document.getElementById("saveName").value.trim();
  if (!name) { toast("名前を入れてください", true); return; }
  const plan = await api("/api/panel/save", { method: "POST", body: { name } });
  showPlan(plan, () => toast("保存しました"));
});

document.addEventListener("keydown", e => {
  if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (e.key === "a" || e.key === "A") document.getElementById("abA").click();
  if (e.key === "b" || e.key === "B") document.getElementById("abB").click();
});

renderHeader("panel").then(() => { loadEngines(); refreshAll(); });
