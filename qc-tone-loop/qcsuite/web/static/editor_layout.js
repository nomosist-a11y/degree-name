// エディター: grid layout the way Cortex Control does it.
//
//  * drag a block to an empty cell (move) or onto another block (swap); hold Option/Alt
//    to duplicate instead; drop it outside the grid to delete it
//  * drag a device from the list onto a cell to place it
//  * hover ×, Delete / Backspace, the block's ⋮ menu or a right-click to delete;
//    ⌘C / ⌘V (Ctrl on Windows) copy and paste a block
//  * click a port to change the row's input / output; rows 1 and 3 can branch (split)
//  * scene buttons switch scenes; double-click renames
//
// Every grid change goes to POST /api/editor/layout/<action> and is one undo step in the
// panel's history; nothing is saved. Deleting is not confirmed (it can be undone), except
// for capture / IR blocks, whose capture choice an undo cannot bring back.
"use strict";

const EdLayout = (() => {
  const CLIP_KEY = "qcsuite.editor.clip";
  const T_BLOCK = "application/x-qc-block";
  const T_MODEL = "application/x-qc-model";
  let drag = null;              // {kind: "block"|"model", ...} while a drag is in flight

  // ------------------------------------------------------------ clipboard (per viewer)

  function clip() {
    if (ED.clip) return ED.clip;
    try { ED.clip = JSON.parse(localStorage.getItem(CLIP_KEY) || "null"); } catch { ED.clip = null; }
    return ED.clip;
  }

  function setClip(data) {
    ED.clip = data;
    try { localStorage.setItem(CLIP_KEY, JSON.stringify(data)); } catch { /* private window */ }
  }

  // ------------------------------------------------------------ one call per change

  async function act(action, body, okText) {
    if (ED.busy) { toast("前の操作の途中です", true); return null; }
    if (ED.gesture) await endGesture(ED.gesture);
    ED.busy = true;
    let res = null;
    try {
      res = await edFetch(`/api/editor/layout/${action}`, body);
      (res.warnings || []).forEach(w => toast(w, true));
      if (okText && res.id !== null) toast(`${okText}(保存はしていません。取り消しで戻せます)`);
      await loadGrid();
    } catch (e) {
      /* edFetch showed why (409: changed on the unit, nothing written) */
      await loadGrid();
    } finally {
      ED.busy = false;
    }
    loadHistory();
    return res;
  }

  const where = (r, c) => `行${r + 1} 列${c + 1}`;
  const nameOf = b => b.title || b.model;

  // ------------------------------------------------------------ actions

  async function remove(b) {
    if (!b) return;
    if (b.lossy && !await confirmDialog("ブロックの削除",
      `${where(b.row, b.col)} の ${nameOf(b)} を削除しますか? 取り消しても、キャプチャー・IR の選択は戻りません。`, "削除する")) return;
    const res = await act("delete", { row: b.row, col: b.col, model_id: b.model_id }, `${nameOf(b)} を削除しました`);
    if (res && ED.sel && ED.sel.row === b.row && ED.sel.col === b.col) {
      ED.sel = { row: b.row, col: b.col, model_id: null };
      renderGrid(); renderParams(); renderSidebar();
    }
  }

  async function move(b, row, col) {
    const target = cellAt(row, col);
    if (target && target.row === b.row && target.col === b.col) return;
    const res = await act("move", { row: b.row, col: b.col, model_id: b.model_id, to_row: row, to_col: col,
      to_model_id: target ? target.model_id : null },
    target ? `${nameOf(b)} と ${nameOf(target)} を入れ替えました` : `${nameOf(b)} を ${where(row, col)} へ移動しました`);
    if (res) select(row, col);
  }

  async function duplicate(b, row, col) {
    if (cellAt(row, col)) { toast("複製は空いているマスに落としてください", true); return; }
    if (b.color === "cap") { toast("キャプチャーは本体で複製してください(パソコンから選んだキャプチャーは鳴りません)", true); return; }
    const res = await act("duplicate", { row: b.row, col: b.col, model_id: b.model_id, to_row: row, to_col: col },
      `${nameOf(b)} を ${where(row, col)} に複製しました`);
    if (res) select(row, col);
  }

  async function copy(b) {
    if (!b) return;
    if (b.color === "cap") { toast("キャプチャーはコピーできません(本体で複製してください)", true); return; }
    try {
      const data = await edFetch(`/api/editor/block?row=${b.row}&col=${b.col}`, undefined, { method: "GET" });
      setClip(data);
      toast(`${nameOf(b)} をコピーしました`);
    } catch (e) { /* shown */ }
  }

  async function paste(row, col) {
    const c = clip();
    if (!c) { toast("コピーしたブロックがありません", true); return; }
    const target = cellAt(row, col);
    if (target && target.lossy && !await confirmDialog("貼り付け",
      `${where(row, col)} の ${nameOf(target)} に貼り付けますか? 取り消しても、キャプチャー・IR の選択は戻りません。`, "貼り付ける")) return;
    const res = await act("paste", { row, col, model_id: target ? target.model_id : null, source: c },
      `${where(row, col)} に ${c.model} を貼り付けました`);
    if (res) select(row, col);
  }

  async function reset(b) {
    await act("reset", { row: b.row, col: b.col, model_id: b.model_id }, `${nameOf(b)} を初期値に戻しました`);
  }

  async function placeAt(modelId, row, col) {
    const hit = ED.catIndex.get(modelId);
    const m = hit ? hit.model : null;
    if (!m) return;
    if (m.usable === false) { toast(m.why_not || `${m.name} は置けません`, true); return; }
    const b = cellAt(row, col);
    if (b && b.model_id === m.id) return;
    if (b && b.lossy && !await confirmDialog("ブロックの差し替え",
      `${where(row, col)} の ${nameOf(b)} を ${m.name} に差し替えますか? 取り消しても、キャプチャー・IR の選択は戻りません。`, "差し替える")) return;
    if (ED.busy) return;
    ED.busy = true;
    try {
      const res = await edFetch("/api/editor/replace", { row, col, model_id: b ? b.model_id : null, new_model_id: m.id });
      (res.warnings || []).forEach(w => toast(w, true));
      toast(`${where(row, col)} に ${m.name} を置きました(保存はしていません。取り消しで戻せます)`);
    } catch (e) { /* shown */ } finally { ED.busy = false; }
    await loadGrid();
    select(row, col);
    loadHistory();
  }

  function select(row, col) {
    if (cellAt(row, col) || ED.view) selectCell(row, col, { focus: true });
  }

  function freeCellNear(b) {
    for (let c = b.col + 1; c < NCOLS; c++) if (!cellAt(b.row, c)) return [b.row, c];
    for (let c = b.col - 1; c >= 0; c--) if (!cellAt(b.row, c)) return [b.row, c];
    return null;
  }

  // ------------------------------------------------------------ the block menu (⋮, right-click)

  function blockMenuItems(b) {
    const c = clip();
    const spot = freeCellNear(b);
    return [
      { head: b.category_label || b.category || "" },
      { info: `${nameOf(b)}(${where(b.row, b.col)})` },
      b.based_on ? { info: b.based_on } : null,
      { sep: true },
      { label: "コピー(⌘C)", disabled: b.color === "cap", onClick: () => copy(b) },
      { label: c ? `ペースト: ${c.model}(⌘V)` : "ペースト(⌘V)", disabled: !c, onClick: () => paste(b.row, b.col) },
      { label: spot ? `複製(右の空き ${where(...spot)} へ)` : "複製(同じ行に空きがありません)",
        disabled: !spot || b.color === "cap", onClick: () => duplicate(b, ...spot) },
      { label: "パラメーターを初期値に戻す", disabled: !!b.lossy, onClick: () => reset(b) },
      { label: b.bypassed ? "バイパスを解除" : "バイパス", onClick: () => toggleBypass(b) },
      { sep: true },
      { label: "削除(Delete)", onClick: () => remove(b) },
      { sep: true },
      { label: "読み直す", onClick: () => loadGrid() },
    ];
  }

  function emptyMenuItems(row, col) {
    const c = clip();
    return [
      { head: `${where(row, col)}(空き)` },
      { label: c ? `ペースト: ${c.model}(⌘V)` : "ペースト(⌘V)", disabled: !c, onClick: () => paste(row, col) },
      { info: "左の一覧からドラッグ(またはダブルクリック)で置けます。" },
    ];
  }

  // ------------------------------------------------------------ ports and splits

  function portMenu(anchor, r, side) {
    const list = (ED.view.ports || {})[side] || [];
    const cur = side === "in" ? r.in_port : r.out_port;
    const items = [{ head: `行${r.row + 1} の${side === "in" ? "入力" : "出力"}` }];
    if (side === "in" && cur === "PREV_ROW") items.push({ info: "いまは前の行から信号が来ています(Prev. row)。" });
    for (const p of list) {
      items.push({ label: p.label, on: p.id === cur,
        disabled: p.id === cur,
        onClick: () => act("port", { row: r.row, which: side, port: p.id, expect: cur },
          `行${r.row + 1} の${side === "in" ? "入力" : "出力"}を ${p.label} にしました`) });
    }
    if (side === "out" && (r.row === 0 || r.row === 2)) {
      items.push({ sep: true }, { head: `分岐(並列の経路は行${r.row + 2})` });
      if (r.split) items.push({ info: `列${r.split[0] + 1} で分岐、${r.split[1] < 0 ? "合流しない" : `列${r.split[1] + 1} で合流`}` });
      items.push({ label: r.split ? "分岐の位置を変える…" : "分岐を作る…", onClick: () => splitDialog(r) });
      if (r.split) items.push({ label: "分岐を外す", onClick: () => act("split", { row: r.row, clear: true, expect: r.split }, `行${r.row + 1} の分岐を外しました`) });
    }
    items.push({ sep: true }, { info: "本体全体の入出力の設定(I/O、Global EQ など)はここでは変えません。" });
    openMenu(anchor, items);
  }

  function splitDialog(r) {
    const opts = (from, to, sel, extra) => [...(extra || []), ...Array.from({ length: to - from }, (_, i) => from + i)]
      .map(v => el("option", { value: String(v), selected: v === sel }, v < 0 ? "合流しない" : `列${v + 1}`));
    const split = el("select", { "aria-label": "分岐する列" }, opts(0, NCOLS - 1, r.split ? r.split[0] : 0));
    const mix = el("select", { "aria-label": "合流する列" }, opts(1, NCOLS, r.split ? r.split[1] : NCOLS - 1, [-1]));
    const ok = el("button", { type: "button", class: "primary" }, "決定");
    const cancel = el("button", { type: "button" }, "やめる");
    const dlg = el("dialog", { class: "ed-dlg" }, el("h3", {}, `行${r.row + 1} の分岐`),
      el("p", {}, `行${r.row + 1} の信号を途中で分け、行${r.row + 2} を並列の経路にします。保存はしません。取り消しで戻せます。`),
      el("div", { class: "ed-dlg-fields" }, el("label", {}, "分岐 ", split), el("label", {}, "合流 ", mix)),
      el("div", { class: "row" }, cancel, ok));
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener("click", close);
    dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
    ok.addEventListener("click", () => {
      const s = Number(split.value), m = Number(mix.value);
      if (m !== -1 && m <= s) { toast("合流は分岐より右の列にしてください", true); return; }
      close();
      act("split", { row: r.row, split: s, mix: m, expect: r.split || null }, `行${r.row + 1} に分岐を作りました`);
    });
    document.body.append(dlg);
    dlg.showModal();
  }

  // ------------------------------------------------------------ scenes

  async function switchScene(i) {
    try {
      const res = await edFetch("/api/editor/scene", { scene: i });
      toast(`シーン ${res.active} にしました(本体の音が変わります)`);
    } catch (e) { /* shown */ }
    loadGrid();
  }

  function renameScene(i) {
    const labels = (ED.view && ED.view.scene_labels) || [];
    const input = el("input", { type: "text", maxlength: "30", value: (labels[i] || "").trim(), "aria-label": "シーンの名前" });
    const ok = el("button", { type: "button", class: "primary" }, "変更");
    const cancel = el("button", { type: "button" }, "やめる");
    const dlg = el("dialog", { class: "ed-dlg" }, el("h3", {}, `シーン ${"ABCDEFGH"[i]} の名前`),
      el("p", {}, "保存はしません(本体でプリセットを呼び出し直すと元に戻ります)。シーン名の変更は取り消しの対象外です。"),
      el("div", { class: "ed-dlg-fields" }, input), el("div", { class: "row" }, cancel, ok));
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener("click", close);
    dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
    const submit = async () => {
      close();
      try {
        await edFetch("/api/editor/scene", { scene: i, label: input.value });
        toast(`シーン ${"ABCDEFGH"[i]} の名前を変えました(保存はしていません)`);
      } catch (e) { /* shown */ }
      loadGrid();
    };
    ok.addEventListener("click", submit);
    input.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); }
    });
    document.body.append(dlg);
    dlg.showModal();
    input.select();
  }

  function sceneButton(s, i, labels, active) {
    const lab = (labels[i] || "").trim();
    const btn = el("button", { type: "button", class: `pb-scene ${active === s ? "on" : ""}`,
      "aria-pressed": String(active === s),
      title: `${lab ? `${s}: ${lab}\n` : ""}クリックでシーン ${s} に切り替え(本体の音が変わります)。右クリック(長押し)で名前を変更` }, s);
    btn.addEventListener("click", () => { if (active !== s) switchScene(i); });
    btn.addEventListener("contextmenu", e => {
      e.preventDefault();
      openMenu(btn, [{ head: `シーン ${s}${lab ? `: ${lab}` : ""}` },
        { label: "名前を変える…", onClick: () => renameScene(i) },
        active !== s ? { label: `シーン ${s} に切り替える`, onClick: () => switchScene(i) } : null]);
    });
    return btn;
  }

  // ------------------------------------------------------------ drag & drop on the grid

  function payload(e) {
    const dt = e.dataTransfer;
    if (!dt) return null;
    if (drag) return drag;
    try {
      if ([...dt.types].includes(T_BLOCK)) return { kind: "block", ...JSON.parse(dt.getData(T_BLOCK) || "{}") };
      if ([...dt.types].includes(T_MODEL)) return { kind: "model", id: Number(dt.getData(T_MODEL)) };
    } catch { /* foreign drag */ }
    return null;
  }

  function endDrag() {
    const wanted = drag && ED.reloadWanted;
    drag = null;
    if (wanted && !ED.gesture) { ED.reloadWanted = false; setTimeout(loadGrid, 0); }
    document.body.classList.remove("ed-dragging", "ed-dragging-model");
    document.querySelectorAll(".ed-cell.drop-ok, .ed-cell.drop-swap, .ed-cell.drop-dup")
      .forEach(n => n.classList.remove("drop-ok", "drop-swap", "drop-dup"));
  }

  const cellNodeAt = (row, col) => document.querySelector(`.ed-cell[data-row="${row}"][data-col="${col}"]`);

  function wireCell(node) {
    const row = Number(node.dataset.row), col = Number(node.dataset.col);
    const b = cellAt(row, col);
    if (b) {
      node.draggable = true;
      node.addEventListener("dragstart", e => {
        if (ED.busy || ED.gesture) { e.preventDefault(); return; }
        drag = { kind: "block", row, col, model_id: b.model_id, at: Date.now() };
        e.dataTransfer.effectAllowed = "copyMove";
        e.dataTransfer.setData(T_BLOCK, JSON.stringify({ row, col, model_id: b.model_id }));
        e.dataTransfer.setData("text/plain", nameOf(b));
        requestAnimationFrame(() => document.body.classList.add("ed-dragging"));
      });
      node.addEventListener("dragend", endDrag);
      node.addEventListener("contextmenu", e => { e.preventDefault(); selectCell(row, col, { focus: false }); openMenu(cellNodeAt(row, col) || node, blockMenuItems(cellAt(row, col) || b)); });
      const x = el("button", { type: "button", class: "ed-x", tabindex: "-1", "aria-label": `${nameOf(b)} を削除`,
        title: "削除(取り消しで戻せます)" }, svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: "M7 7l10 10M17 7 7 17" })));
      x.addEventListener("click", e => { e.stopPropagation(); remove(b); });
      x.addEventListener("mousedown", e => e.stopPropagation());
      node.append(x);
    } else {
      node.addEventListener("contextmenu", e => { e.preventDefault(); selectCell(row, col, { focus: false }); openMenu(cellNodeAt(row, col) || node, emptyMenuItems(row, col)); });
    }
    node.addEventListener("dragover", e => {
      const p = payload(e);
      if (!p) return;
      e.preventDefault();
      e.stopPropagation();
      const occupied = !!cellAt(row, col);
      const dup = p.kind === "block" && e.altKey;
      e.dataTransfer.dropEffect = dup || p.kind === "model" ? "copy" : "move";
      node.classList.toggle("drop-dup", dup && !occupied);
      node.classList.toggle("drop-swap", !dup && occupied && !(p.kind === "block" && p.row === row && p.col === col));
      node.classList.toggle("drop-ok", !dup && !occupied);
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop-ok", "drop-swap", "drop-dup"));
    node.addEventListener("drop", e => {
      const p = payload(e);
      if (!p) return;
      e.preventDefault();
      e.stopPropagation();
      const alt = e.altKey;
      endDrag();
      if (p.kind === "model") { placeAt(p.id, row, col); return; }
      const src = cellAt(p.row, p.col);
      if (!src || src.model_id !== p.model_id) { toast("ブロックが画面と違います。読み直します", true); loadGrid(); return; }
      if (alt) duplicate(src, row, col); else move(src, row, col);
    });
  }

  // Outside the grid: a dropped block is deleted (like dragging it off the grid on the unit).
  document.addEventListener("dragover", e => {
    if (drag && drag.kind === "block" && !e.target.closest(".ed-grid")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    }
  });
  document.addEventListener("drop", e => {
    if (!drag || drag.kind !== "block" || e.target.closest(".ed-grid")) return;
    if (Date.now() - drag.at > 60000 || !e.dataTransfer || ![...e.dataTransfer.types].includes(T_BLOCK)) {
      endDrag();               // a stale drag (its source was redrawn) or someone else's drop
      return;
    }
    e.preventDefault();
    const p = drag;
    endDrag();
    const b = cellAt(p.row, p.col);
    if (b && b.model_id === p.model_id) remove(b);
  });

  function wireDevice(item, m) {
    if (m.usable === false) return;
    item.draggable = true;
    item.addEventListener("dragstart", e => {
      drag = { kind: "model", id: m.id, at: Date.now() };
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData(T_MODEL, String(m.id));
      e.dataTransfer.setData("text/plain", m.name);
      document.body.classList.add("ed-dragging-model");
      if (SB_DRAWER_MQ.matches) setTimeout(closeDrawer, 0);
    });
    item.addEventListener("dragend", endDrag);
  }

  function decorate(box) {
    box.querySelectorAll(".ed-cell").forEach(wireCell);
    box.querySelectorAll(".ed-port[data-row]").forEach(node => {
      const r = ED.view.rows.find(x => x.row === Number(node.dataset.row));
      if (!r) return;
      node.setAttribute("role", "button");
      node.tabIndex = 0;
      node.title = `${node.title}\nクリックで${node.dataset.side === "in" ? "入力" : "出力"}を変更`;
      const open = () => portMenu(node, r, node.dataset.side);
      node.addEventListener("click", open);
      node.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    });
    // split markers: the branch and rejoin columns on the row and its lane
    for (const r of ED.view.rows) {
      if (!r.split) continue;
      const [sc, mc] = r.split;
      for (const rowNo of [r.row, r.row + 1]) {
        const cells = box.querySelectorAll(`.ed-cell[data-row="${rowNo}"]`);
        if (cells[sc]) cells[sc].classList.add("split-from");
        if (mc >= 0 && cells[mc]) cells[mc].classList.add("split-to");
      }
      const rowEl = box.querySelectorAll(".ed-row")[r.row + 1];
      if (rowEl) rowEl.classList.add("lane");
    }
  }

  // ------------------------------------------------------------ keys

  document.addEventListener("keydown", e => {
    const t = e.target;
    if (typingTarget(t) || openPop || document.querySelector("dialog[open]")) return;
    if (t && t.closest && (t.closest('[role="slider"]') || t.closest(".drawer"))) return;
    if (e.repeat) return;
    const mod = e.metaKey || e.ctrlKey;
    const b = ED.sel ? findBlock(ED.sel) : null;
    if ((e.key === "Delete" || e.key === "Backspace") && !mod && b) {
      if (t && t.closest && t.closest(".sb")) return;
      e.preventDefault();
      remove(b);
    } else if (mod && !e.altKey && (e.key === "c" || e.key === "C") && b) {
      if (window.getSelection && String(window.getSelection())) return;   // copying page text
      e.preventDefault();
      copy(b);
    } else if (mod && !e.altKey && (e.key === "v" || e.key === "V") && ED.sel) {
      e.preventDefault();
      paste(ED.sel.row, ED.sel.col);
    }
  });

  return { decorate, wireDevice, blockMenuItems, sceneButton, copy, paste, remove,
    dragging: () => !!drag && Date.now() - drag.at < 60000 };
})();
window.EdLayout = EdLayout;
