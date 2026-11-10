const OPT_IN = ["Live", "Artist", "Song", "Workshop"];

async function refresh() {
  const s = await api("/api/status");
  QC.status = s;
  const info = document.getElementById("connInfo");
  document.getElementById("btnReset").hidden = s.mode !== "mock";
  if (s.connected && s.device) {
    info.textContent = `${s.mode === "mock" ? "モック" : "実機"} / ファームウェア ${s.device.firmware}` +
      (s.busy_job ? ` / 実行中のジョブ ${s.busy_job}` : "");
    drawPolicy(s.policy);
    document.getElementById("notices").replaceChildren(notices(s.notices));
    const folders = await api("/api/folders");
    document.getElementById("folders").replaceChildren(table(folders, [
      ["name", "フォルダ"], ["occupied", "使用数"], ["slots", "スロット"],
      ["kind", "種類", (v, r) => r.protected ? "保護" : { user: "ユーザー", sandbox: "sandbox", factory: "工場出荷(読み取りのみ)" }[v] || v],
    ]), el("p", { class: "small muted" },
      "カスタムのセットリスト(フォルダ)は最大 12 個とされています(Neural DSP のサポート情報)。sandbox も 1 個分を使います。"));
  } else {
    info.textContent = s.mode === "mock" ? "モック(未接続)" : "実機(未接続)。Cortex Control を終了してから接続してください。";
  }
}

function drawPolicy(policy) {
  const box = document.getElementById("policy");
  const allowed = new Set(policy.allowed);
  box.replaceChildren(
    el("div", {}, "常に許可: ", el("code", {}, policy.sandbox)),
    el("div", { class: "row" }, "明示的に許可するフォルダ:", OPT_IN.map(name => {
      const key = `/media/p4/Presets/${name}`;
      const cb = el("input", { type: "checkbox", checked: allowed.has(key) });
      cb.addEventListener("change", async () => {
        const p = await api("/api/policy/allow", { method: "POST", body: { folder: name, allowed: cb.checked } });
        drawPolicy(p);
        toast(`${name}: ${cb.checked ? "書き込みを許可しました" : "許可を外しました"}`);
      });
      return el("label", {}, cb, name);
    })),
    policy.readonly ? el("div", { class: "warn" }, "QCSUITE_READONLY が設定されているため、すべての書き込みを拒否します。") : null,
  );
}

async function drawDis() {
  const dis = await api("/api/di");
  document.getElementById("dis").replaceChildren(table(dis, [
    ["name", "名前"], ["seconds", "秒", v => fmt(v)], ["peak_dbfs", "ピーク dBFS", v => fmt(v)],
    ["source", "出どころ"], ["note", "メモ"],
    ["file", "試聴", v => el("audio", { controls: true, preload: "none", src: fileUrl(`di/${v}`) })],
  ]));
}

document.getElementById("btnConnect").addEventListener("click", async () => {
  await api("/api/connect", { method: "POST" }); toast("接続しました"); refresh();
});
document.getElementById("btnDisconnect").addEventListener("click", async () => {
  await api("/api/disconnect", { method: "POST" }); toast("切断しました"); refresh();
});
document.getElementById("btnReset").addEventListener("click", async () => {
  if (!confirm("モックの状態(コピーや sandbox)を初期状態に戻します。")) return;
  await api("/api/mock/reset", { method: "POST" }); toast("初期化しました"); refresh();
});
document.getElementById("btnImport").addEventListener("click", async () => {
  const f = document.getElementById("diFile").files[0];
  if (!f) { toast("ファイルを選んでください", true); return; }
  const form = new FormData();
  form.append("file", f); form.append("name", document.getElementById("diName").value);
  await api("/api/di/import", { method: "POST", body: form }); toast("取り込みました"); drawDis();
});
document.getElementById("btnRecord").addEventListener("click", async () => {
  const name = document.getElementById("recName").value.trim();
  if (!name) { toast("名前を入れてください", true); return; }
  toast("録音中… Input 1 で弾いてください");
  await api("/api/di/record", { method: "POST", body: { name, seconds: Number(document.getElementById("recSec").value) } });
  toast("録音しました"); drawDis();
});

renderHeader("index").then(() => { refresh(); drawDis(); });
