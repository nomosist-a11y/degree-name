function source() {
  return document.querySelector("input[name=src]:checked").value;
}

function selection() {
  const s = source();
  const body = { title: document.getElementById("title").value.trim() };
  if (s === "backup") {
    body.from_backup = true;
    body.refs = [...document.querySelectorAll(".bk:checked")].map(cb => cb.value);
    if (!body.refs.length) throw new Error("バックアップからプリセットを選んでください");
  } else if (s === "refs") {
    body.refs = document.getElementById("refs").value.split(",").map(x => x.trim()).filter(Boolean);
    if (!body.refs.length) throw new Error("フォルダ:スロットを入れてください(例 kami:1E)");
  } else if (s === "folder") {
    body.folder = document.getElementById("folder").value;
  } else {
    body.current = true;
  }
  return body;
}

async function confirmRecalls(body) {
  if (body.from_backup || body.current) return true;
  const { recalls } = await api("/api/sheets/recalls", { method: "POST", body });
  if (!recalls) return true;
  return confirm(`${recalls} 個のプリセットを本体に読み込みます。音が切り替わり、本体で保存していない編集は消えます。続けますか?`);
}

function showResult(text, file) {
  document.getElementById("result").replaceChildren(el("p", {},
    text, " ", el("a", { href: fileUrl(file), target: "_blank" }, "開く / ダウンロード")));
}

async function run(kind) {
  let body;
  try { body = selection(); } catch (e) { toast(e.message, true); return; }
  if (!(await confirmRecalls(body))) return;
  if (kind === "pack") {
    body.pdf = document.getElementById("packPdf").checked;
    if (document.getElementById("packRender").checked) {
      body.render_di = document.getElementById("di").value;
      if (!confirmMonitor()) return;
    }
  }
  toast("作成中…");
  const r = await api(`/api/sheets/${kind}`, { method: "POST", body });
  if (kind === "html") {
    window.open(fileUrl(r.file), "_blank");
    showResult(`HTML 資料(${r.presets} 件)を作りました。`, r.file);
  } else if (kind === "pdf") {
    showResult(`PDF(${r.pages} ページ、フォント ${r.font})を作りました。${r.note || ""}`, r.file);
  } else {
    showResult(`共有パック(プリセット ${r.presets}、音声 ${r.audio})を作りました。`, r.file);
  }
}

async function init() {
  await renderHeader("sheets");
  document.querySelectorAll("input[name=src]").forEach(r => r.addEventListener("change", () => {
    const s = source();
    document.getElementById("pickBackup").hidden = s !== "backup";
    document.getElementById("pickRefs").hidden = s !== "refs";
    document.getElementById("pickFolder").hidden = s !== "folder";
  }));
  const data = await api("/api/sheets/sources");
  document.getElementById("folder").replaceChildren(...data.folders.map(f => el("option", { value: f }, f)));
  const box = document.getElementById("backupList");
  if (!data.backup.length) {
    box.replaceChildren(el("p", {}, "バックアップがまだありません。「バックアップ」のページで取るか、ほかの方法を選んでください。"));
  } else {
    box.replaceChildren(el("div", { class: "row" },
      el("input", { id: "bkFilter", placeholder: "絞り込み" }),
      el("button", { onclick: () => document.querySelectorAll(".bk").forEach(cb => { if (cb.closest("label").style.display !== "none") cb.checked = true; }) }, "表示中をすべて選ぶ")),
      el("div", { class: "scroll", style: "max-height: 18em" }, data.backup.map(b =>
        el("label", { style: "display: flex" }, el("input", { type: "checkbox", class: "bk", value: b.ref }), `${b.ref} ${b.name}`))));
    document.getElementById("bkFilter").addEventListener("input", e => {
      const q = e.target.value.toLowerCase();
      document.querySelectorAll(".bk").forEach(cb => {
        cb.closest("label").style.display = cb.closest("label").textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });
  }
  const dis = await api("/api/di");
  document.getElementById("di").replaceChildren(...dis.map(d => el("option", { value: d.name }, d.name)));
  document.getElementById("btnHtml").addEventListener("click", () => run("html"));
  document.getElementById("btnPdf").addEventListener("click", () => run("pdf"));
  document.getElementById("btnPack").addEventListener("click", () => run("pack"));
}
init();
