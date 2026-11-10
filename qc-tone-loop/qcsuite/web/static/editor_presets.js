// エディター: switching presets and following changes made on the unit.
//
// Presets: < > step to the previous / next stored preset in the loaded setlist, the
// preset name and the DIRECTORY tab open a list. Loading changes the sound and drops
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

  async function openList() {
    const status = el("p", { class: "ed-dlg-note" }, "読み込み中…(セットリストの一覧は、初回は 20 秒ほどかかります)");
    const folderSel = el("select", { "aria-label": "セットリスト", disabled: true });
    const list = el("div", { class: "ed-plist", role: "listbox", "aria-label": "プリセット" });
    const close = el("button", { type: "button" }, "閉じる");
    const manage = el("a", { href: "/static/library.html", class: "ed-dlg-link" }, "プリセット管理を開く");
    const dlg = el("dialog", { class: "ed-dlg ed-dlg-wide" },
      el("h3", {}, "プリセット"),
      el("p", { class: "ed-dlg-note" }, "クリックで呼び出します(本体の音が変わります。保存していない編集があるときは確認します)。保存はエディターの保存アイコンから sandbox に。"),
      el("div", { class: "ed-dlg-fields" }, folderSel), status, list,
      el("div", { class: "row" }, manage, close));
    const done = () => { dlg.close(); dlg.remove(); };
    close.addEventListener("click", done);
    dlg.addEventListener("cancel", e => { e.preventDefault(); done(); });
    document.body.append(dlg);
    dlg.showModal();

    const fill = async folder => {
      status.textContent = "読み込み中…";
      status.hidden = false;
      list.replaceChildren();
      let data;
      try {
        data = await edFetch(`/api/editor/presets${folder ? `?folder=${encodeURIComponent(folder)}` : ""}`,
          undefined, { method: "GET" });
      } catch (e) { status.textContent = "読み込めませんでした。"; return; }
      if (!folderSel.options.length) {
        folderSel.replaceChildren(...data.setlists.map(f => el("option", { value: f.key }, f.name)));
      }
      folderSel.value = data.folder || "";
      folderSel.disabled = false;
      const cur = data.current;
      status.textContent = cur ? `いま読み込まれているのは ${cur.folder_name} ${cur.slot}` : "";
      status.hidden = !cur;
      list.replaceChildren(...(data.presets.length ? data.presets.map(p => {
        const here = cur && cur.folder === data.folder && cur.position === p.position;
        const item = el("button", { type: "button", role: "option", class: `ed-pitem ${here ? "cur" : ""}`,
          "aria-selected": String(!!here) }, el("span", { class: "slot" }, p.slot), el("span", { class: "nm" }, p.name || "(名前なし)"));
        item.addEventListener("click", () => { done(); recall({ folder: data.folder, position: p.position }); });
        return item;
      }) : [el("p", { class: "ed-dlg-note" }, "プリセットがありません。")]));
      const curItem = list.querySelector(".cur");
      if (curItem) curItem.scrollIntoView({ block: "center" });
    };
    folderSel.addEventListener("change", () => fill(folderSel.value));
    fill(null);
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
