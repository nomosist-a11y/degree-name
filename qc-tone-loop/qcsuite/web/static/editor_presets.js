// エディター: switching presets and following changes made on the unit.
//
// Presets: < > step to the previous / next stored preset in the loaded setlist, the
// preset name and the DIRECTORY tab open the DIRECTORY dialog (setlists and presets with
// a ⋮ menu - rename / move / delete as plans, only in the sandbox or allowed folders -,
// the capture library and the live preset's MIDI Out, both read-only). Loading changes the sound and drops
// unsaved edits, so - like Cortex Control - it asks first only when the unit reports
// unsaved edits (POST /api/editor/recall answers needs_confirm). Undo history stops
// at the switch.
//
// Following the unit: GET /api/editor/changes is a counter of the pushes the unit sends
// (grid, scene, preset, dirty flag); it costs no unit I/O. When it moves, the grid is
// read again - never during a knob drag, a block drag or a write, and never while a
// dialog is open. Without pushes ("poll") the grid is re-read every few seconds. What
// the unit shows wins: the page never writes to catch up.
"use strict";

const EdPresets = (() => {
  const TICK_MS = 1500;          // how often the page asks the counter (in-memory on the server)
  const POLL_MS = 5000;          // re-read interval when the unit's pushes are not available
  let lastSeq = null;
  let lastPoll = 0;
  let ticking = false;

  // ------------------------------------------------------------ loading a preset

  async function recall(body) {
    if (ED.busy) { toast("前の操作の途中です", true); return; }
    if (ED.gesture) await endGesture(ED.gesture);
    ED.busy = true;
    let res = null;
    try {
      res = await edFetch("/api/editor/recall", body);
      if (res.needs_confirm) {
        ED.busy = false;
        const ok = await confirmDialog("プリセットの呼び出し",
          `${res.slot} ${res.name} を呼び出します。${res.message}`, "呼び出す");
        if (!ok) return;
        ED.busy = true;
        res = await edFetch("/api/editor/recall", { folder: res.folder, position: res.position, confirm: true });
      }
      if (res.loaded) {
        toast(`${res.slot} ${res.name} を呼び出しました(音が切り替わります)`);
        ED.sel = null;
        ED.everSelected = false;
        ED.page = {};
      }
    } catch (e) {
      /* edFetch showed why */
    } finally {
      ED.busy = false;
    }
    await loadGrid();
    loadHistory();
    loadAB();
  }

  // ------------------------------------------------------------ the list (name click, DIRECTORY)
  //
  // Like Cortex Control's Directory: setlists, presets, a ⋮ menu per preset, captures and
  // the live preset's MIDI Out. Rename / move / delete and setlist create / delete are
  // offered only where the server's write policy allows (the sandbox, or an opt-in folder
  // started with --allow-folder). Each one builds a plan first (nothing is sent), shows
  // its steps and backup folder, and runs only after the confirm word is typed.

  let lastFolder = null;

  function smallDialog(title, ...body) {
    const dlg = el("dialog", { class: "ed-dlg" }, el("h3", {}, title), ...body);
    const close = () => { dlg.close(); dlg.remove(); };
    dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
    document.body.append(dlg);
    dlg.showModal();
    return { dlg, close };
  }

  // Ask for one text value (a name, a slot). Resolves to the trimmed text or null.
  function ask(title, note, value, okLabel, extra) {
    return new Promise(resolve => {
      const input = el("input", { value: value || "", "aria-label": title, autocomplete: "off" });
      const ok = el("button", { type: "button", class: "primary" }, okLabel || "計画を見る");
      const cancel = el("button", { type: "button" }, "やめる");
      const { dlg, close } = smallDialog(title, el("p", { class: "ed-dlg-note" }, note), input,
        extra || null, el("div", { class: "row" }, cancel, ok));
      const done = v => { close(); resolve(v); };
      ok.addEventListener("click", () => { const v = input.value.trim(); if (v) done(v); else input.focus(); });
      input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); ok.click(); } });
      cancel.addEventListener("click", () => done(null));
      dlg.addEventListener("cancel", () => resolve(null));
      // A prefix to type after (the sandbox's "_qcsuite_") keeps the caret at its end.
      if ((value || "").endsWith("_")) { input.focus(); input.setSelectionRange(value.length, value.length); }
      else input.select();
    });
  }

  // A plan from the server: steps, refusals, backup folder; runs after the word is typed.
  function planDialog(plan, onFinished) {
    const word = plan.confirm_word || "実行";
    const refused = plan.refused || [];
    const input = el("input", { placeholder: `「${word}」と入力`, "aria-label": "確認の言葉", autocomplete: "off" });
    const run = el("button", { type: "button", class: plan.destructive ? "danger" : "primary", disabled: true },
      plan.destructive ? "バックアップしてから実行" : "実行");
    const cancel = el("button", { type: "button" }, "やめる");
    const progress = el("p", { class: "ed-dlg-note", hidden: true });
    const pending = el("p", { class: "ed-dlg-note" }, `まだ何も変わっていません(計画だけ)。実行するには「${word}」と入力してください。`);
    const { dlg, close } = smallDialog(plan.title,
      el("pre", { class: "eddir-plan" }, plan.text),
      refused.length ? el("div", { class: "eddir-warn" }, "拒否される手順があるため実行できません:",
        el("pre", {}, refused.join("\n"))) : null,
      plan.backup_dir ? el("p", { class: "ed-dlg-note" }, "バックアップの保存先: ", el("code", {}, plan.backup_dir))
        : el("p", { class: "ed-dlg-note" }, "消える手順が無いので、バックアップは作りません。"),
      pending,
      refused.length ? null : input, progress,
      el("div", { class: "row" }, cancel, refused.length ? null : run));
    dlg.classList.add("ed-dlg-wide");
    input.addEventListener("input", () => { run.disabled = input.value.trim() !== word; });
    input.addEventListener("keydown", e => { if (e.key === "Enter" && !run.disabled) { e.preventDefault(); run.click(); } });
    cancel.addEventListener("click", close);
    run.addEventListener("click", async () => {
      run.disabled = true; input.disabled = true; cancel.disabled = true;
      pending.remove();
      progress.hidden = false;
      progress.textContent = "実行中…(終わるまで、ほかの書き込みはできません)";
      let job;
      try {
        job = (await edFetch(`/api/plans/${plan.id}/execute`, { confirm: input.value.trim() })).job;
      } catch (e) { cancel.disabled = false; input.disabled = false; progress.textContent = "始められませんでした。"; return; }
      for (;;) {
        await new Promise(r => setTimeout(r, 800));
        let detail;
        try { detail = await edFetch(`/api/jobs/${job}`, undefined, { method: "GET", quiet: true }); } catch { continue; }
        const s = detail.summary || {};
        if (["new", "running"].includes(s.status)) { progress.textContent = `実行中… ${s.current || ""}`; continue; }
        const results = detail.results || [];
        const last = results[results.length - 1] || {};
        const backups = (last.result && last.result.backups) || [];
        const failed = !last.ok;
        progress.replaceChildren(
          failed ? `止まりました: ${last.error || s.message || s.status}`
            : "終わりました。本体の一覧で確かめてください。",
          backups.length ? el("span", {}, el("br"), `バックアップ ${backups.length} 件: `, el("code", {}, plan.backup_dir || "")) : "");
        cancel.textContent = "閉じる";
        cancel.disabled = false;
        run.remove();
        input.remove();
        toast(failed ? "計画が途中で止まりました(記録が残っています)" : "計画を実行しました", !!failed);
        // Presets were loaded on the unit (copies, backups): undo must not reach past this.
        if (plan.changes_sound) edFetch("/api/editor/directory/finished", { plan: plan.id }, { quiet: true }).catch(() => {});
        if (onFinished) onFinished();
        return;
      }
    });
    input.focus();
  }

  async function planAndShow(body, onFinished) {
    let plan;
    try { plan = await edFetch("/api/editor/directory/plan", body); } catch { return; /* shown */ }
    planDialog(plan, onFinished);
  }

  function presetMenu(anchor, p, data, refresh, recallIt) {
    const old = document.querySelector(".eddir-menu");
    if (old) { old.remove(); if (old.dataset.for === String(p.position)) return; }
    const item = (label, fn, attrs = {}) => {
      const b = el("button", { type: "button", role: "menuitem", ...attrs }, label);
      if (fn) b.addEventListener("click", () => { menu.remove(); fn(); });
      return b;
    };
    const folder = data.folder;
    const menu = el("div", { class: "eddir-menu", role: "menu", "data-for": String(p.position) },
      item("呼び出す", recallIt),
      data.writable ? [
        item("名前を変更…", async () => {
          const name = await ask("名前を変更", "本体に名前を変える命令が無いため、コピー → 元を削除 → 元のスロットへ移動、の計画を作ります(20 文字まで)。", p.name);
          if (name) planAndShow({ action: "rename", folder, position: p.position, name }, refresh);
        }),
        item("スロットを移動…", async () => {
          const list = el("datalist", { id: "eddirFree" }, ...(data.free_slots || []).map(s => el("option", { value: s })));
          const to = await ask("スロットを移動", `${p.slot} から、同じセットリストの空きスロットへ(例: 3C)。上書きはしません。`,
            (data.free_slots || [])[0] || "", "計画を見る", list);
          if (to) planAndShow({ action: "move", folder, position: p.position, to }, refresh);
        }),
        item("削除…", () => planAndShow({ action: "delete", folder, position: p.position }, refresh), { class: "eddir-danger" }),
      ] : el("p", { class: "eddir-ro" }, "読むだけのフォルダです"));
    anchor.after(menu);
    menu.querySelector("button").focus();
  }

  function presetsTab(state) {
    const folderSel = el("select", { "aria-label": "セットリスト", disabled: true });
    const btnNew = el("button", { type: "button", class: "eddir-small", disabled: true }, "新しいセットリスト");
    const btnDel = el("button", { type: "button", class: "eddir-small", disabled: true }, "このセットリストを削除");
    const status = el("p", { class: "ed-dlg-note" }, "読み込み中…(セットリストの一覧は、初回は 20 秒ほどかかります)");
    const ro = el("p", { class: "eddir-ro", hidden: true });
    const list = el("div", { class: "ed-plist", role: "listbox", "aria-label": "プリセット" });
    let data = null;

    const fill = async folder => {
      status.textContent = "読み込み中…";
      status.hidden = false;
      list.replaceChildren();
      try {
        data = await edFetch(`/api/editor/directory${folder ? `?folder=${encodeURIComponent(folder)}` : ""}`,
          undefined, { method: "GET" });
      } catch (e) { status.textContent = "読み込めませんでした。"; return; }
      lastFolder = data.folder;
      folderSel.replaceChildren(...data.setlists.map(f =>
        el("option", { value: f.key }, f.writable ? `${f.name}(書き込める)` : f.name)));
      folderSel.value = data.folder || "";
      folderSel.disabled = false;
      const here = data.setlists.find(f => f.key === data.folder);
      btnDel.disabled = !(here && here.deletable);
      btnNew.disabled = data.create.readonly || data.room.free < 1;
      btnNew.title = data.room.free < 1 ? `セットリストが上限(${data.room.limit} 個と想定)です` : "";
      ro.hidden = data.writable;
      ro.textContent = data.writable ? "" : "読むだけのフォルダです(名前の変更・移動・削除はできません)。書き込めるのは sandbox と、起動時に許可したフォルダだけです。";
      const cur = data.current;
      status.textContent = cur ? `いま読み込まれているのは ${cur.folder_name} ${cur.slot}` : "";
      status.hidden = !cur;
      list.replaceChildren(...(data.presets.length ? data.presets.map(p => {
        const isCur = cur && cur.folder === data.folder && cur.position === p.position;
        const recallIt = () => { state.done(); recall({ folder: data.folder, position: p.position }); };
        const item = el("button", { type: "button", role: "option", class: `ed-pitem ${isCur ? "cur" : ""}`,
          "aria-selected": String(!!isCur) }, el("span", { class: "slot" }, p.slot), el("span", { class: "nm" }, p.name || "(名前なし)"));
        item.addEventListener("click", recallIt);
        const more = el("button", { type: "button", class: "eddir-more", "aria-label": `${p.slot} ${p.name} のメニュー`,
          "aria-haspopup": "menu", title: "メニュー" }, "⋮");
        const row = el("div", { class: "eddir-row" }, item, more);
        more.addEventListener("click", e => { e.stopPropagation(); presetMenu(row, p, data, () => fill(data.folder), recallIt); });
        return row;
      }) : [el("p", { class: "ed-dlg-note" }, "プリセットがありません。")]));
      const curItem = list.querySelector(".cur");
      if (curItem) curItem.scrollIntoView({ block: "center" });
    };

    folderSel.addEventListener("change", () => fill(folderSel.value));
    btnNew.addEventListener("click", async () => {
      const allowed = (data && data.create.allowed) || [];
      const name = await ask("新しいセットリスト",
        `名前は「${data.create.prefix}」で始めてください(例: ${data.create.prefix}_gig)。` +
        (allowed.length ? `許可したフォルダ(${allowed.join("、")})も作れます。` : "") +
        `いまユーザーのセットリストは ${data.room.user_folders} / ${data.room.limit} 個(想定の上限)です。`,
        `${data.create.prefix}_`);
      if (name) planAndShow({ action: "create_setlist", name }, () => fill(data.folder));
    });
    btnDel.addEventListener("click", () => {
      if (data && data.folder) planAndShow({ action: "delete_setlist", folder: data.folder }, () => fill(null));
    });
    fill(lastFolder);
    return el("div", { class: "eddir-pane" },
      el("p", { class: "ed-dlg-note" }, "クリックで呼び出します(本体の音が変わります。保存していない編集があるときは確認します)。⋮ で名前の変更・移動・削除(sandbox と許可したフォルダだけ)。"),
      el("div", { class: "ed-dlg-fields eddir-bar" }, folderSel, btnNew, btnDel), ro, status, list);
  }

  function capturesTab() {
    const filter = el("input", { type: "search", placeholder: "名前で絞り込む", "aria-label": "キャプチャーを名前で絞り込む" });
    const status = el("p", { class: "ed-dlg-note" }, "読み込み中…");
    const list = el("div", { class: "ed-plist eddir-caps" });
    let caps = [];
    const draw = () => {
      const words = filter.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const shown = caps.filter(c => words.every(w => (c.name || "").toLowerCase().includes(w)));
      status.textContent = `${shown.length} / ${caps.length} 件`;
      list.replaceChildren(...shown.slice(0, 500).map(c => el("div", { class: "eddir-cap" }, c.name || "(名前なし)")));
    };
    filter.addEventListener("input", draw);
    edFetch("/api/editor/directory/captures", undefined, { method: "GET" })
      .then(r => { caps = r.captures || []; note.textContent = r.note; draw(); })
      .catch(() => { status.textContent = "読み込めませんでした。"; });
    const note = el("p", { class: "eddir-ro" }, "パソコンから選んだキャプチャーは鳴りません。本体で読み込んでください。");
    return el("div", { class: "eddir-pane" }, note, el("div", { class: "ed-dlg-fields" }, filter), status, list);
  }

  function midiTab() {
    const box = el("div", {}, el("p", { class: "ed-dlg-note" }, "読み込み中…"));
    edFetch("/api/editor/directory/midi", undefined, { method: "GET" }).then(r => {
      box.replaceChildren(el("p", { class: "ed-dlg-note" }, r.note),
        r.rows.length ? el("table", { class: "eddir-midi" },
          el("thead", {}, el("tr", {}, el("th", {}, "送るもの"), el("th", {}, "種類"), el("th", {}, "ch"), el("th", {}, "内容"))),
          el("tbody", {}, ...r.rows.map(m => el("tr", {}, el("td", {}, m.source), el("td", {}, m.type),
            el("td", {}, String(m.channel)), el("td", {}, m.detail)))))
          : el("p", { class: "ed-dlg-note" }, "このプリセットには MIDI Out の設定がありません。"));
    }).catch(() => { box.replaceChildren(el("p", { class: "ed-dlg-note" }, "読み込めませんでした。")); });
    return el("div", { class: "eddir-pane" }, box);
  }

  async function openList() {
    const close = el("button", { type: "button" }, "閉じる");
    const manage = el("a", { href: "/static/library.html", class: "ed-dlg-link" }, "プリセット管理を開く");
    const tabs = [["presets", "プリセット", presetsTab], ["captures", "キャプチャー", capturesTab], ["midi", "MIDI", midiTab]];
    const body = el("div", {});
    const state = {};
    const tabBar = el("div", { class: "eddir-tabs", role: "tablist" });
    const show = id => {
      for (const b of tabBar.children) b.setAttribute("aria-selected", String(b.dataset.tab === id));
      const t = tabs.find(x => x[0] === id);
      body.replaceChildren(t[2](state));
    };
    for (const [id, label] of tabs) {
      const b = el("button", { type: "button", role: "tab", "data-tab": id }, label);
      b.addEventListener("click", () => show(id));
      tabBar.append(b);
    }
    const dlg = el("dialog", { class: "ed-dlg ed-dlg-wide eddir" },
      el("h3", {}, "DIRECTORY"), tabBar, body, el("div", { class: "row" }, manage, close));
    const done = () => { dlg.close(); dlg.remove(); loadGrid(); };
    state.done = () => { dlg.close(); dlg.remove(); };
    close.addEventListener("click", done);
    dlg.addEventListener("cancel", e => { e.preventDefault(); done(); });
    dlg.addEventListener("click", e => {
      if (!e.target.closest(".eddir-menu, .eddir-more")) { const m = dlg.querySelector(".eddir-menu"); if (m) m.remove(); }
    });
    document.body.append(dlg);
    dlg.showModal();
    show("presets");
  }

  // ------------------------------------------------------------ following the unit

  function quiet() {
    return document.visibilityState === "visible" && !ED.gesture && !ED.busy && !ED.loading &&
      !(window.EdLayout && EdLayout.dragging()) && !document.querySelector("dialog[open]") && !openPop;
  }

  async function tick() {
    if (ticking || !quiet()) return;
    ticking = true;
    try {
      const res = await fetch("/api/editor/changes");
      if (!res.ok) return;
      const c = await res.json();
      if (c.mode === "push") {
        if (lastSeq !== null && c.seq !== lastSeq && quiet()) {
          lastSeq = c.seq;
          await loadGrid();
          loadHistory();
          return;
        }
        lastSeq = c.seq;
      } else if (c.mode === "poll" && Date.now() - lastPoll > POLL_MS && quiet()) {
        lastPoll = Date.now();
        await loadGrid();
      }
    } catch (e) {
      /* the next tick tries again */
    } finally {
      ticking = false;
    }
  }

  setInterval(tick, TICK_MS);

  // ------------------------------------------------------------ wiring

  document.getElementById("btnPrevPreset").addEventListener("click", () => recall({ step: -1 }));
  document.getElementById("btnNextPreset").addEventListener("click", () => recall({ step: 1 }));
  const name = document.getElementById("presetName");
  name.addEventListener("click", openList);
  name.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openList(); } });
  document.getElementById("tabDirectory").addEventListener("click", e => { e.preventDefault(); openList(); });

  return { openList, recall, tick };
})();
window.EdPresets = EdPresets;
