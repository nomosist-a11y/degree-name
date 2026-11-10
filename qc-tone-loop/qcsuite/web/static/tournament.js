// Blind comparison page: build a comparison from presets, listen to A/B at the
// same position, vote, see the tally, reveal names when the player asks.

const selected = new Map();
let current = null;      // tournament id
let pair = null;         // the pair on screen
let side = "a";          // which side is audible

const audio = { a: document.getElementById("audioA"), b: document.getElementById("audioB") };

async function loadFolders() {
  const folders = (await api("/api/folders")).filter(f => f.kind !== "captures");
  const sel = document.getElementById("folder");
  sel.replaceChildren(...folders.map(f => el("option", { value: f.key }, `${f.name}(${f.occupied})`)));
  await loadPresets();
}

async function loadPresets() {
  const key = document.getElementById("folder").value;
  const presets = await api(`/api/presets?folder=${encodeURIComponent(key)}`);
  document.getElementById("presetList").replaceChildren(...presets.map(p => {
    const id = `${key}#${p.index}`;
    const cb = el("input", { type: "checkbox", checked: selected.has(id) });
    cb.addEventListener("change", () => {
      if (cb.checked) selected.set(id, { folder: key, position: p.index });
      else selected.delete(id);
      document.getElementById("selCount").textContent = `選択中: ${selected.size} 件(フォルダをまたいで選べます)`;
    });
    return el("div", {}, el("label", {}, cb, `${p.slot} ${p.name}`));
  }));
}

async function loadDis() {
  const dis = await api("/api/di");
  document.getElementById("di").replaceChildren(...dis.map(d =>
    el("option", { value: d.name }, `${d.name}${d.source === "synthetic" ? "(合成)" : ""}`)));
}

async function loadList() {
  const rows = await api("/api/tournament");
  document.getElementById("list").replaceChildren(rows.length ? table(rows.map(r => ({
    ...r, prog: `${r.progress.votes}/${r.progress.planned}`, state: r.revealed ? "名前を表示済み" : "伏せ中",
  })), [["created", "作成"], ["title", "タイトル"], ["format", "形式"], ["candidates", "候補"],
        ["prog", "投票"], ["state", "状態"]], { onRow: r => open(r.id) })
    : el("p", { class: "muted" }, "(まだありません)"));
}

function setSide(which) {
  const other = which === "a" ? "b" : "a";
  const playing = !audio[side].paused;
  const t = audio[side].currentTime;
  side = which;
  audio[other].pause();
  audio[which].currentTime = Math.min(t, audio[which].duration || t);
  if (playing || audio[other].currentTime > 0) audio[which].play();
  document.getElementById("sideA").classList.toggle("playing", which === "a");
  document.getElementById("sideB").classList.toggle("playing", which === "b");
}

function playPause() {
  if (audio[side].paused) audio[side].play(); else audio[side].pause();
}

async function open(tid) {
  current = tid;
  document.getElementById("listening").hidden = false;
  document.getElementById("standingsBox").hidden = false;
  document.getElementById("exportLink").href = `/api/tournament/${tid}/export?fmt=md`;
  await drawStandings();
  await nextPair();
  document.getElementById("listening").scrollIntoView({ behavior: "smooth" });
}

async function nextPair() {
  const t = await api(`/api/tournament/${current}`);
  document.getElementById("ttitle").textContent = t.title;
  pair = await api(`/api/tournament/${current}/next`);
  const prog = pair.progress;
  document.getElementById("tprogress").textContent =
    `投票 ${prog.votes}/${prog.planned}、第 ${prog.round}/${prog.rounds_planned} 回戦、音量 ${fmt(t.target_lufs)} LUFS にそろえています`;
  const done = document.getElementById("doneBox");
  for (const id of ["voteA", "voteB", "voteTie", "playA", "playB"]) document.getElementById(id).disabled = !!pair.done;
  if (pair.done) {
    done.replaceChildren(el("p", { class: "okc" }, "予定の組はすべて聴き終わりました。集計を見て、名前を表示してください。"));
    audio.a.pause(); audio.b.pause();
    return;
  }
  done.replaceChildren();
  document.getElementById("voteTie").disabled = !pair.allow_tie;
  audio.a.src = fileUrl(pair.a.audio);
  audio.b.src = fileUrl(pair.b.audio);
  audio.a.currentTime = 0; audio.b.currentTime = 0;
  setSide("a");
}

async function vote(choice) {
  if (!pair || pair.done) return;
  if (choice === "0" && !pair.allow_tie) { toast("勝ち抜き戦では、どちらかを選んでください", true); return; }
  await api(`/api/tournament/${current}/vote`, { method: "POST", body: { choice, pair_id: pair.pair_id } });
  audio.a.pause(); audio.b.pause();
  await drawStandings();
  await nextPair();
}

async function drawStandings() {
  const t = await api(`/api/tournament/${current}`);
  const rows = t.revealed ? t.reveal : t.standings;
  const cols = [["code", "候補"], ["rating", "レーティング", v => fmt(v)], ["wins", "勝"],
    ["losses", "負"], ["ties", "分"]];
  if (t.revealed) cols.push(["label", "正体"]);
  if (t.format === "knockout") cols.push(["alive", "勝ち残り", v => v ? "○" : ""]);
  const box = document.getElementById("standings");
  box.replaceChildren(table(rows, cols),
    el("p", { class: "small muted" }, "この表はあなたの投票の集計です。"));
  if (t.revealed && t.consistency && t.consistency.checks)
    box.append(el("p", { class: "small" }, `同じ音どうしの確認: ${t.consistency.checks} 回中 ` +
      `${t.consistency.answered_same} 回「同じくらい」と答えました。`));
  if (t.excluded && t.excluded.length)
    box.append(el("div", { class: "small" }, "比較から除外: ",
      el("ul", {}, t.excluded.map(e => el("li", {}, `${e.label}: ${e.reasons.join(" / ")}`)))));
  document.getElementById("btnReveal").disabled = t.revealed;
}

document.getElementById("folder").addEventListener("change", loadPresets);
document.getElementById("playA").addEventListener("click", () => setSide("a"));
document.getElementById("playB").addEventListener("click", () => setSide("b"));
document.getElementById("playPause").addEventListener("click", playPause);
document.getElementById("voteA").addEventListener("click", () => vote("1"));
document.getElementById("voteB").addEventListener("click", () => vote("2"));
document.getElementById("voteTie").addEventListener("click", () => vote("0"));
document.getElementById("btnReveal").addEventListener("click", async () => {
  if (!confirm("名前を表示すると、この比較はもう目隠しではなくなります。表示しますか?")) return;
  await api(`/api/tournament/${current}/reveal`, { method: "POST" });
  drawStandings(); loadList();
});

document.addEventListener("keydown", e => {
  if (!current || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if (e.key === "1") vote("1");
  else if (e.key === "2") vote("2");
  else if (e.key === "0") vote("0");
  else if (e.key === "a" || e.key === "A") setSide("a");
  else if (e.key === "b" || e.key === "B") setSide("b");
  else if (e.key === " ") { e.preventDefault(); playPause(); }
});

document.getElementById("btnCreate").addEventListener("click", async () => {
  if (selected.size < 2) { toast("2 つ以上選んでください", true); return; }
  if (!confirm(`${selected.size} 件のプリセットを本体に順に読み込んで鳴らします。音が切り替わり、` +
      "本体で保存していない編集は消えます。続けますか?") || !confirmMonitor()) return;
  const body = { presets: [...selected.values()], di: document.getElementById("di").value,
    format: document.getElementById("format").value, rounds: document.getElementById("rounds").value,
    checks: document.getElementById("checks").value, title: document.getElementById("title").value };
  const { job } = await api("/api/tournament/presets", { method: "POST", body });
  watchJob(job, async detail => {
    if (!["done", "done_with_errors"].includes(detail.summary.status)) return;
    // The comparison is assembled right after the last render: wait for it quietly.
    for (let i = 0; i < 40; i++) {
      const res = await fetch(`/api/tournament/by-job/${job}`);
      if (res.ok) { const t = await res.json(); await loadList(); open(t.id); return; }
      const state = await api(`/api/jobs/${job}`);
      if (state.summary.status === "failed") { toast(state.summary.message || "比較を作れませんでした", true); return; }
      await new Promise(r => setTimeout(r, 500));
    }
    toast("比較の準備に時間がかかっています。一覧を読み込み直してください。", true);
  });
});

renderHeader("tournament").then(async () => {
  await api("/api/connect", { method: "POST" });
  await Promise.all([loadFolders(), loadDis(), loadList()]);
  const want = new URLSearchParams(location.search).get("id");
  if (want) open(want);
});
