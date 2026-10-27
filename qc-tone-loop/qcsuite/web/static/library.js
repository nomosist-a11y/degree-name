const LIB = { rows: [], selected: new Set(), current: null, dupesMd: "", favMd: "", found: [], chosen: new Set() };
const $ = id => document.getElementById(id);
const keyOf = r => `${r.folder_key}#${r.position}`;

function download(name, text, type = "text/markdown") {
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const a = el("a", { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function loadFolders() {
  const folders = await api("/api/folders");
  const sel = $("folder");
  for (const f of folders) sel.append(el("option", { value: f.key }, f.name));
  const policy = (QC.status && QC.status.policy) || (await api("/api/status")).policy;
  const dest = $("dest");
  dest.replaceChildren(el("option", { value: "" }, "sandbox(_qcsuite_sandbox)"));
  for (const key of (policy && policy.allowed) || []) {
    dest.append(el("option", { value: key }, key.split("/").pop()));
  }
}

async function search() {
  const params = new URLSearchParams({ q: $("q").value, folder: $("folder").value,
    sort: $("sort").value, include_factory: $("factory").checked });
  LIB.rows = await api(`/api/library/index?${params}`);
  drawList();
}

function drawList() {
  const box = $("list");
  if (!LIB.rows.length) { box.replaceChildren(el("p", { class: "muted" }, "(該当なし)")); return; }
  box.replaceChildren(table(LIB.rows, [
    ["position", "選択", (v, r) => {
      const cb = el("input", { type: "checkbox", checked: LIB.selected.has(keyOf(r)) });
      cb.addEventListener("click", ev => ev.stopPropagation());
      cb.addEventListener("change", () => {
        if (cb.checked) LIB.selected.add(keyOf(r)); else LIB.selected.delete(keyOf(r));
        $("selCount").textContent = `${LIB.selected.size} 件を選択`;
      });
      return cb;
    }],
    ["folder", "フォルダ", (v, r) => r.protected ? `${v}(保護)` : v],
    ["slot", "スロット"],
    ["name", "名前", (v, r) => el("a", { href: "#detailPanel", onclick: () => showDetail(r) }, v)],
  ]), el("p", { class: "small muted" }, `${LIB.rows.length} 件`));
}

function drawDetail(view) {
  const n = view.preset;
  const src = { backup: `バックアップ(${view.commit || ""})`, load: "本体に読み込み", current: "現在のグリッド" }[view.source];
  const knobRows = view.blocks.map(b => ({ block: b.block, knobs: b.knobs.map(([k, v]) => `${k} ${v}`).join("、"),
    issues: b.issues.join(" / ") }));
  $("detail").replaceChildren(
    el("div", {}, el("strong", {}, `${n.folder ? n.folder.split("/").pop() + " " + (n.slot || "") : ""}「${n.name || ""}」`),
      el("span", { class: "badge" }, src)),
    view.warning ? el("div", { class: "warn" }, view.warning) : null,
    chainView(n),
    table(knobRows, [["block", "ブロック"], ["knobs", "ノブ"], ["issues", "注意", v => v ? el("span", { class: "danger" }, v) : ""]]),
    el("p", { class: "small muted" }, "シーン名: " + (n.scene_labels || []).map((l, i) => l ? `${"ABCDEFGH"[i]}=${l}` : "").filter(Boolean).join(" ") || "(なし)"));
}

async function showDetail(r) {
  LIB.current = r;
  $("btnLoad").disabled = false;
  const view = await api(`/api/library/show?folder=${encodeURIComponent(r.folder_key)}&position=${r.position}&mode=backup`);
  if (view.missing) {
    $("detail").replaceChildren(el("p", { class: "muted" }, `${r.folder} ${r.slot}「${r.name}」: ${view.message}`));
    return;
  }
  drawDetail(view);
}

$("btnLoad").addEventListener("click", async () => {
  const r = LIB.current;
  if (!r) return;
  if (!confirm(`「${r.name}」を本体に読み込みます。本体の音が切り替わり、本体で保存していない編集は消えます。`)) return;
  drawDetail(await api("/api/library/load", { method: "POST", body: { folder: r.folder_key, position: r.position } }));
});
$("btnCurrent").addEventListener("click", async () => drawDetail(await api("/api/library/show?mode=current")));
$("btnSearch").addEventListener("click", search);
$("q").addEventListener("keydown", e => { if (e.key === "Enter") search(); });

$("btnCopy").addEventListener("click", async () => {
  const items = LIB.rows.filter(r => LIB.selected.has(keyOf(r))).map(r => ({ folder: r.folder_key, position: r.position }));
  if (!items.length) { toast("コピーするプリセットを選んでください", true); return; }
  const names = {};
  const rename = $("rename").value.trim();
  if (rename) {
    if (items.length !== 1) { toast("新しい名前は 1 件のときだけ使えます", true); return; }
    names[`${items[0].folder}#${items[0].position}`] = rename;
  }
  const plan = await api("/api/library/copy-plan", { method: "POST", body: { items, to: $("dest").value, names } });
  showPlan(plan, () => { LIB.selected.clear(); $("selCount").textContent = "0 件を選択"; search(); });
});

$("btnDupes").addEventListener("click", async () => {
  const d = await api("/api/library/dupes");
  LIB.dupesMd = d.markdown;
  $("btnDupesMd").disabled = false;
  const nameRows = d.names.groups.map(g => ({ name: g.name, where: g.entries.map(e => `${e.folder} ${e.slot}`).join("、") }));
  const parts = [el("h3", {}, `同じ名前が複数のフォルダにある(${nameRows.length} 件)`),
    table(nameRows, [["name", "名前"], ["where", "場所"]])];
  if (d.names.factory_overlap.length) {
    parts.push(el("p", {}, `工場出荷と同じ名前: ${d.names.factory_overlap.join("、")}`));
  }
  parts.push(el("h3", {}, "中身が同じ(バックアップの指紋で比較)"));
  if (!d.contents.available) parts.push(el("div", { class: "warn" }, d.contents.message));
  else if (!d.contents.groups.length) parts.push(el("p", { class: "muted" }, "(見つかりませんでした)"));
  else parts.push(table(d.contents.groups.map(g => ({ n: g.entries.length,
    where: g.entries.map(e => `${e.folder} ${e.slot}「${e.name}」`).join("、"), fp: g.fingerprint })),
    [["n", "件数"], ["where", "プリセット"], ["fp", "指紋"]]),
    el("p", { class: "small muted" }, `バックアップ ${d.contents.commit || ""}(${d.contents.presets} 件)から。` +
      "qcsuite は削除しません。整理は整理案を見て本体で行ってください。"));
  $("dupes").replaceChildren(...parts);
});
$("btnDupesMd").addEventListener("click", () => download("qcsuite-duplicates.md", LIB.dupesMd));

$("btnFav").addEventListener("click", async () => {
  const c = await api("/api/library/favorites");
  LIB.favMd = c.markdown;
  $("btnFavMd").disabled = false;
  const where = hs => hs.map(h => h.folder + (h.points_at_folder ? "(このフォルダ)" : "")).join("、");
  $("fav").replaceChildren(
    el("p", {}, `お気に入り ${c.hearts} 件 / 「${c.folder}」フォルダ ${c.folder_presets} 件` +
      (c.folder_exists ? "" : "(フォルダがありません)")),
    el("h3", {}, `両方にある(${c.both.length})`),
    table(c.both.map(e => ({ name: e.name, slots: e.slots.join("/"), where: where(e.hearts) })),
      [["name", "名前"], ["slots", "フォルダのスロット"], ["where", "ハートの指す先"]]),
    el("h3", {}, `ハートだけ(${c.hearts_only.length})`),
    table(c.hearts_only.map(e => ({ name: e.name, where: where(e.hearts) })), [["name", "名前"], ["where", "ハートの指す先"]]),
    el("h3", {}, `フォルダだけ(${c.folder_only.length})`),
    table(c.folder_only.map(e => ({ name: e.name, slots: e.slots.join("/") })), [["name", "名前"], ["slots", "スロット"]]));
});
$("btnFavMd").addEventListener("click", () => download("qcsuite-favorites.md", LIB.favMd));

$("btnFind").addEventListener("click", async () => {
  const kind = $("kind").value, q = encodeURIComponent($("q2").value);
  LIB.chosen.clear();
  if (kind === "catalog") {
    const models = await api(`/api/library/catalog?q=${q}`);
    LIB.found = [];
    $("found").replaceChildren(table(models, [
      ["id", "ID"], ["category", "種類"], ["name", "名前", (v, m) => m.inert_plugin ? `${v}(未所有プラグイン: 素通し)` : v],
      ["params", "パラメーター", ps => ps.map(p => p.name).join(", ")],
    ]), el("p", { class: "small muted" }, `${models.length} 件`));
    return;
  }
  const res = await api(`/api/library/${kind}?q=${q}`);
  LIB.found = res.items;
  $("found").replaceChildren(table(res.items, [
    ["name", "選択", (v, r) => {
      const cb = el("input", { type: "checkbox" });
      cb.addEventListener("change", () => cb.checked ? LIB.chosen.add(r) : LIB.chosen.delete(r));
      return cb;
    }],
    ["name", "名前"],
  ]), el("p", { class: "small muted" }, `${res.total} 件中 ${res.items.length} 件を表示`));
});

async function exportCandidates(format) {
  const items = [...LIB.chosen];
  if (!items.length) { toast("候補に入れるものを選んでください", true); return; }
  const res = await fetch("/api/library/candidates", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items, format }) });
  download(`qcsuite-candidates.${format}`, await res.text(), format === "csv" ? "text/csv" : "text/markdown");
}
$("btnCandMd").addEventListener("click", () => exportCandidates("md"));
$("btnCandCsv").addEventListener("click", () => exportCandidates("csv"));

async function loadUsage() {
  const u = await api("/api/library/usage");
  $("usage").replaceChildren(table(u.folders, [
    ["name", "フォルダ"], ["occupied", "使用"], ["free", "空き"],
    ["kind", "種類", (v, f) => f.protected ? "保護" : { user: "ユーザー", sandbox: "sandbox", factory: "工場出荷(読み取りのみ)" }[v] || v],
  ]), el("p", {}, `ユーザーのセットリスト: ${u.user_setlists} / ${u.setlist_limit}(想定) 残り ${u.setlist_free}`),
  el("p", { class: "small muted" }, u.note));
}

renderHeader("library").then(async () => {
  await loadFolders();
  await search();
  loadUsage();
});
