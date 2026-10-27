"""``qcsuite selftest``: the hardware test plan as a program.

``qcsuite selftest --hardware`` walks ``docs/hardware-test-plan.md`` on the
player's Mac, step by step and in the same order: the two checks that decide
whether anything else may run (room for another setlist; whether a row output
VOLUME written from the computer is audible), then read-only steps, then
writes into the sandbox, then reamps, then cleanup. Each step prints what it
is about to do, asks before anything that changes the sound or writes, records
PASS / FAIL / SKIP / STOP with numbers, and the report is written as it goes,
so a crash still leaves a record. ``--start-at N`` resumes at step N.

At the end the sandbox is deleted (through a plan) and the user folders are
compared with the counts at the start - My Presets 51, Favorite 21, kami 20,
Cory Wong 15, Live/Artist/Song/Workshop 0 on the player's unit - and must match
exactly.

``qcsuite --mock selftest`` runs every step against the fake unit with all
answers "yes", which is how the runner itself is tested. A mock run proves the
runner and the features work together; it says nothing about the unit.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from qcsuite import jobs, paths, presets
from qcsuite import plan as plan_mod
from qcsuite.audio import di as di_mod
from qcsuite.audio import lufs, passthrough
from qcsuite.audio.render import MEASURED_LATENCY_MS, MONITOR_WARNING
from qcsuite.device import guard
from qcsuite.device.session import open_rig
from qctone import features

#: The player's folders as reported on 2026-10 (the baseline the cleanup restores).
EXPECTED_COUNTS = {"My Presets": 51, "Favorite": 21, "kami": 20, "Cory Wong": 15,
                   "Live": 0, "Artist": 0, "Song": 0, "Workshop": 0}
DI_SECONDS = 6.0
LANE_DELTA_DB = -6.0
LANE_TOLERANCE_DB = 1.5

PASS, FAIL, SKIP, STOP, INFO = "PASS", "FAIL", "SKIP", "STOP", "INFO"


class Stopped(Exception):
    """A step decided the rest must not run (e.g. no room for the sandbox)."""


@dataclass
class Step:
    number: int
    group: str
    title: str
    expect: str
    on_fail: str
    fn: object
    changes_sound: bool = False
    writes: bool = False
    reamps: bool = False


@dataclass
class Run:
    ctx: object
    rig: object
    report: Path
    mock: bool
    state: dict = field(default_factory=dict)
    results: list = field(default_factory=list)

    @property
    def device(self):
        return self.rig.device

    def ask(self, question: str, default_yes: bool = True) -> bool:
        if self.mock or self.ctx.yes:
            return default_yes
        answer = input(f"{question} [y/n]: ").strip().lower()
        return answer in ("y", "yes", "はい")

    def ask_text(self, question: str, default: str) -> str:
        if self.mock or self.ctx.yes:
            return default
        answer = input(f"{question} [{default}]: ").strip()
        return answer or default

    def di(self):
        if "di" not in self.state:
            audio, sr = di_mod.load(self.state.get("di_name", di_mod.SYNTH_NAME), self.mock)
            audio = audio[: int(DI_SECONDS * sr)]
            self.state["di"] = (audio, sr, features.analyze_array(audio[:, None], sr))
        return self.state["di"]


# -- the steps ------------------------------------------------------------------------

STEPS: list[Step] = []


def step(group, title, expect, on_fail, **flags):
    def register(fn):
        STEPS.append(Step(len(STEPS) + 1, group, title, expect, on_fail, fn, **flags))
        return fn
    return register


def _user_folders(device) -> dict:
    return {f["name"]: f["occupied"] for f in device.setlists() if f["kind"] in ("user", "sandbox")}


@step("0 準備", "接続とファームウェア",
      "接続でき、ファームウェアが d14e(CorOS 4.0.1)と表示される",
      "Cortex Control を終了、USB を確認。d14e 以外なら以降の結果は参考扱い(pyquadcortex は 4.0.1 でのみ確認済み)")
def s_connect(run: Run):
    info = run.device.version()
    ok = info["firmware"] == "d14e"
    return (PASS if ok else FAIL), f"firmware={info['firmware']}"


@step("0 準備", "フォルダ数の基準を記録",
      "My Presets 51 / Favorite 21 / kami 20 / Cory Wong 15 / Live・Artist・Song・Workshop 0",
      "数が違っていたら、本体で整理した結果かを確認する。最後の照合は開始時の実際の数を基準にする")
def s_baseline(run: Run):
    counts = _user_folders(run.device)
    run.state["baseline"] = counts
    sandbox_left = [n for n in counts if n.startswith(guard.SANDBOX_PREFIX)]
    differ = {n: (counts.get(n), want) for n, want in EXPECTED_COUNTS.items()
              if counts.get(n) != want}
    detail = json.dumps(counts, ensure_ascii=False)
    if sandbox_left:
        return FAIL, f"前回の sandbox が残っています: {sandbox_left}。{detail}"
    return (PASS if not differ else INFO), detail + (f" 想定との違い: {differ}" if differ else "")


@step("1 最初の確認", "カスタムセットリストの上限",
      "ユーザーのフォルダ数 + sandbox 1 個が、本体の上限以内",
      "上限に余裕がなければ sandbox を作らずに止まる(以降の書き込み系・後片付けは実行しない)")
def s_setlist_limit(run: Run):
    room = run.device.setlist_room()
    print(f"  ユーザーのフォルダは {room['user_folders']} 個です: {', '.join(room['names'])}")
    print(f"  qcsuite は上限を {room['limit']} 個(My Presets を含む)と想定しています"
          "(Neural DSP のサポート情報: カスタムのセットリストは最大 12 個)。")
    answer = run.ask_text("本体のマニュアルか画面で確認した上限(My Presets を含む総数)を入力",
                          str(room["limit"]))
    try:
        limit = int(answer)
    except ValueError:
        return FAIL, f"数として読めません: {answer!r}"
    run.state["setlist_limit"] = limit
    import os
    os.environ["QCSUITE_SETLIST_LIMIT"] = str(limit)
    if room["user_folders"] + 1 > limit:
        raise Stopped(f"フォルダ {room['user_folders']} 個 + sandbox 1 個が上限 {limit} を超えるため止めます。")
    return PASS, f"フォルダ {room['user_folders']} 個、上限 {limit}、残り {limit - room['user_folders']}"


FACTORY = "/opt/neuraldsp/Factory Library"


def _load_factory_for_reamp(device) -> int:
    """Recall the first factory preset whose guitar rows can be pointed at USB 5.

    Factory presets are read-only, so nothing stored can be harmed; the routing
    change is unsaved and undone by the next recall.
    """
    for entry in device.presets(FACTORY)[:16]:
        stored = device.read_stored(FACTORY, entry["index"])
        if device.prepare_reamp(stored):
            return entry["index"]
    raise RuntimeError("Input 1 から始まる工場出荷プリセットが最初の 16 個に見つかりません")


@step("1 最初の確認", "Lane Output VOLUME をパソコンから書いて音量が変わるか",
      f"出力の行の VOLUME を {LANE_DELTA_DB:+.0f} dB 動かすと、レンダリングの LUFS も約 {LANE_DELTA_DB:+.0f} dB 動く"
      f"(±{LANE_TOLERANCE_DB} dB)",
      "変わらなければ「書けるのに効かない」。音量そろえは別の方法(--method gain_block など)に切り替える",
      changes_sound=True, reamps=True)
def s_lane_volume(run: Run):
    device = run.device
    audio, sr, _ = run.di()
    print("  工場出荷のプリセットを読み込み、行の入力を USB 5 にしてリアンプします(保存はしません)。")
    if not run.mock:
        print(f"  注意: {MONITOR_WARNING}")
        if not run.ask("準備はできましたか?"):
            return SKIP, "ユーザーが中止"
    position = _load_factory_for_reamp(device)
    rows = device.output_rows() or [0]
    before_db = {r: device.lane_volume_db(r) for r in rows}
    loud_before = lufs.integrated_loudness(run.rig.render(audio, sr).audio, sr)
    for r in rows:
        device.set_lane_volume_db(r, max(-39.5, before_db[r] + LANE_DELTA_DB))
    loud_after = lufs.integrated_loudness(run.rig.render(audio, sr).audio, sr)
    for r in rows:
        device.set_lane_volume_db(r, before_db[r])
    device.recall(FACTORY, position)
    change = loud_after - loud_before
    effective = abs(change - LANE_DELTA_DB) <= LANE_TOLERANCE_DB
    run.state["lane_volume_effective"] = effective
    detail = f"前 {loud_before:.2f} LUFS → 後 {loud_after:.2f} LUFS(変化 {change:+.2f} dB、行 {rows})"
    return (PASS if effective else FAIL), detail


@step("2 読み取り", "カタログの取得(再試行込み)",
      "モデル数が表示される。25 秒のタイムアウトが出ても再試行で通る",
      "4 回続けて失敗したら本体を再起動してやり直す")
def s_catalog(run: Run):
    t0 = time.monotonic()
    n = len(run.device.catalog)
    retries = sum(1 for x in run.device.notices if x.kind == "retry" and "カタログ" in x.message)
    return PASS, f"{n} モデル、{time.monotonic() - t0:.1f} 秒、再試行 {retries} 回"


@step("2 読み取り", "全フォルダの一覧",
      "各フォルダの件数が基準どおり。Factory Library は 256",
      "一覧が来ないときは再試行(遅れて届くことがある)。件数違いは本体の表示と見比べる")
def s_listing(run: Run):
    out = {}
    for f in run.device.setlists():
        out[f["name"]] = len(run.device.presets(f["key"]))
    factory_ok = out.get("Factory Library") == 256
    same = all(out.get(n) == c for n, c in run.state.get("baseline", {}).items())
    return (PASS if factory_ok and same else FAIL), json.dumps(out, ensure_ascii=False)


@step("2 読み取り", "キャプチャー・IR・お気に入りの一覧",
      "キャプチャー約 2,078 件、IR とお気に入りが読める",
      "タイムアウトは再試行。件数が大きく違えば本体のライブラリ表示と見比べる")
def s_libraries(run: Run):
    caps = run.device.captures()
    irs = run.device.irs()
    favs = run.device.favorites()
    return PASS, f"キャプチャー {len(caps)} 件、IR {len(irs)} 件、お気に入り {len(favs)} 件"


@step("2 読み取り", "現在のグリッドの読み取りに副作用がない",
      "2 回読んで同じ。本体の音は途切れない(耳で確認)",
      "音が途切れたら記録して止める(read_current_preset は副作用がないはず)")
def s_grid_read(run: Run):
    a = presets.normalize(run.device.current_preset(), run.device.catalog)
    b = presets.normalize(run.device.current_preset(), run.device.catalog)
    same = a["fingerprint"] == b["fingerprint"]
    heard = run.ask("読み取りの間、本体の音は途切れませんでしたか?")
    return (PASS if same and heard else FAIL), f"fingerprint {a['fingerprint']} / 途切れなし={heard}"


@step("4 リアンプ", "往復遅延と素通し検出",
      f"往復遅延が約 {MEASURED_LATENCY_MS:.0f} ms。未所有プラグインだけの行は素通しと判定される",
      "遅延が大きく違えば QCSUITE_LATENCY_MS で固定。素通し判定が外れたら研究メモの実験 E2 へ",
      changes_sound=True, reamps=True)
def s_latency_passthrough(run: Run):
    device = run.device
    audio, sr, dry = run.di()
    if not run.mock:
        print(f"  注意: {MONITOR_WARNING}")
    position = _load_factory_for_reamp(device)
    result = run.rig.render(audio, sr)
    latency_ok = abs(result.latency_ms - MEASURED_LATENCY_MS) < 30
    plugin = next((m for m in device.catalog
                   if getattr(m, "plugin_id", None) and "plini" in
                   f"{m.name} {m.based_on} {m.plugin_id}".lower()), None)
    if plugin is None:
        device.recall(FACTORY, position)
        return (PASS if latency_ok else FAIL), (f"遅延 {result.latency_ms:.1f} ms。"
                                                "Plini のモデルがカタログに無いので素通し検出は省略")
    grid = device.grid()
    clear = [{"op": "remove_block", "row": 0, "col": b["col"]} for b in grid["blocks"] if b["row"] == 0]
    device.apply_patch({"ops": clear + [{"op": "set_block", "row": 0, "col": 0, "model": plugin.id}]})
    feat = features.analyze_array(run.rig.render(audio, sr).audio, sr)
    check = passthrough.check(feat, dry)
    device.recall(FACTORY, position)
    ok = latency_ok and check["passthrough"]
    return (PASS if ok else FAIL), (f"遅延 {result.latency_ms:.1f} ms、{plugin.name}: "
                                    f"flatness {check['flatness_1k_5k']}、形の差 {check['shape_diff_db']} dB、"
                                    f"素通し={check['passthrough']}")


@step("3 sandbox", "sandbox の作成",
      "_qcsuite_sandbox ができ、0/256 で一覧に出る",
      "上限に余裕がないと止まる(手順 3 の結果を確認)", writes=True)
def s_sandbox(run: Run):
    key = run.device.ensure_sandbox()
    run.state["sandbox"] = key
    room = run.device.setlist_room()
    listed = next(f for f in run.device.setlists() if f["key"] == key)
    return PASS, f"{key}: {listed['occupied']}/{listed['slots']}、フォルダ {room['user_folders']} 個"


@step("3 sandbox", "保護フォルダへの書き込みが拒否される",
      "My Presets / Favorite / kami / Cory Wong / Factory への保存・コピーは、何も送らずに拒否される",
      "拒否されなかったら直ちに止めて報告(安全ルールの不具合)", writes=True)
def s_refusals(run: Run):
    from qcsuite.errors import WriteRefused
    refused = []
    for name in ("My Presets", "Favorite", "kami", "Cory Wong"):
        try:
            run.device.save_current(guard.folder_key(name), 255, "qcsuite should refuse")
        except WriteRefused:
            refused.append(name)
    try:
        run.device.save_current(FACTORY, 255, "x")
    except WriteRefused:
        refused.append("Factory Library")
    ok = len(refused) == 5
    return (PASS if ok else FAIL), f"拒否: {refused}"


@step("3 sandbox", "コピーと名前の重複回避",
      "kami の 1 件を sandbox に 2 回コピーすると、2 回目は 20 文字以内 + _1 に改名される。タグは消える",
      "改名規則が違えば記録する(モックの規則を実機に合わせて直す)", writes=True, changes_sound=True)
def s_copy_dedup(run: Run):
    key = run.state.get("sandbox") or run.device.ensure_sandbox()
    entry = run.device.presets(guard.folder_key("kami"))[0]
    plan = plan_mod.Plan("selftest copy", steps=[
        plan_mod.Step("copy", key, position=0, name=entry["name"],
                      source={"folder": guard.folder_key("kami"), "position": entry["index"]}),
        plan_mod.Step("copy", key, position=1, name=entry["name"],
                      source={"folder": guard.folder_key("kami"), "position": entry["index"]})])
    result = plan_mod.execute(plan, run.device, confirm=True)
    names = [r["stored_name"] for r in result["results"]]
    expected_second = (entry["name"][:18] + "_1") if len(entry["name"]) > 18 else entry["name"] + "_1"
    ok = names[0] == entry["name"] and names[1] == expected_second
    return (PASS if ok else FAIL), f"保存名: {names}(2 回目の想定 {expected_second!r})"


@step("3 sandbox", "JSON からの復元(sandbox へ)",
      "数値パラメーターと構造が一致する(名前以外の差分なし)。文字列・キャプチャーは警告に出る",
      "差分が出たら記録。キャプチャーやマイクは本体で読み込み直す必要がある(既知)", writes=True,
      changes_sound=True)
def s_restore(run: Run):
    key = run.state.get("sandbox") or run.device.ensure_sandbox()
    device = run.device
    source_slot = run.device.presets(key)[0]
    source = presets.normalize(device.read_stored(key, source_slot["index"]), device.catalog)
    position = device.free_position(key)
    plan = plan_mod.Plan("selftest restore", steps=[plan_mod.Step(
        "rebuild", key, position=position, name="selftest restore", payload=source)])
    plan_mod.execute(plan, device, confirm=True)
    restored = presets.normalize(device.read_stored(key, position), device.catalog)
    changes = [c for c in presets.diff(source, restored) if c["where"] != "名前"]
    return (PASS if not changes else FAIL), presets.diff_text(changes)[:600]


@step("5 後片付け", "sandbox の削除とフォルダ数の照合",
      "sandbox が消え、ユーザーのフォルダと件数が開始時と完全に一致する",
      "一致しなければ差分を報告し、本体の画面で確認する(保護フォルダは qcsuite から書けないはず)",
      writes=True)
def s_cleanup(run: Run):
    device = run.device
    folders = {f["name"]: f for f in device.setlists()}
    steps = [plan_mod.Step("delete_folder", f["key"]) for name, f in folders.items()
             if name.startswith(guard.SANDBOX_PREFIX)]
    if steps:
        plan = plan_mod.Plan("selftest cleanup", steps=steps)
        print(plan.render())
        if not run.ask("sandbox を削除しますか?"):
            return SKIP, "ユーザーが中止(sandbox は残っています)"
        plan_mod.execute(plan, device, confirm=True)
    deadline = time.monotonic() + (60 if not run.mock else 1)
    while True:
        after = _user_folders(device)
        if not any(n.startswith(guard.SANDBOX_PREFIX) for n in after) or time.monotonic() > deadline:
            break
        time.sleep(2)
    baseline = run.state.get("baseline") or EXPECTED_COUNTS
    ok = after == baseline
    return (PASS if ok else FAIL), f"開始時 {json.dumps(baseline, ensure_ascii=False)} / 終了時 {json.dumps(after, ensure_ascii=False)}"


# -- feature steps ----------------------------------------------------------------------
# One short pass through each feature, sized to finish in minutes on the unit.
# Reamp steps use Factory Library presets (read-only on the unit) as sources.

FACTORY_REFS = ["Factory Library:1A", "Factory Library:1B", "Factory Library:1C"]


def _ref(ref: str) -> dict:
    folder, slot = ref.rsplit(":", 1)
    import pyquadcortex as pq
    return {"folder": guard.folder_key(folder), "position": pq.slot_to_position(slot)}


@step("2 読み取り", "プリセット管理: 一覧・重複・お気に入り比較(読み取りのみ)",
      "フォルダごとの件数が基準と一致。名前の重複とお気に入り比較が表示される。音は変わらない",
      "件数が違えば本体の画面と見比べる。お気に入り比較は本体のハートの画面と見比べて、違いを記録")
def s_library(run: Run):
    from qcsuite.features import library
    rows = library.index(run.device)
    counts: dict = {}
    for r in rows:
        counts[r["folder_name"] if "folder_name" in r else guard.folder_name(r["folder"])] = \
            counts.get(r.get("folder_name") or guard.folder_name(r["folder"]), 0) + 1
    dupes = library.name_dupes(rows)
    fav = library.favorites_compare(run.device)
    baseline = run.state.get("baseline", {})
    same = all(counts.get(n, 0) == c for n, c in baseline.items() if not n.startswith("_qcsuite"))
    groups = dupes.get("groups", dupes) if isinstance(dupes, dict) else dupes
    detail = (f"件数 {json.dumps(counts, ensure_ascii=False)}、名前の重複 "
              f"{len(groups) if hasattr(groups, '__len__') else groups} 組、お気に入り比較 "
              f"{json.dumps({k: len(v) for k, v in fav.items() if isinstance(v, list)}, ensure_ascii=False)}")
    return (PASS if same else FAIL), detail


@step("2 読み取り", "バックアップ(1 フォルダ)",
      "選んだフォルダの全プリセットが JSON になり、git に 1 コミットできる。1 件 10〜25 秒ほど",
      "途中で止まったら `qcsuite backup run --resume ジョブID`。失敗した件は `qcsuite jobs ジョブID` で確認",
      changes_sound=True)
def s_backup(run: Run):
    from qcsuite.features import backup
    folder = run.ask_text("バックアップするフォルダ(件数の少ないものを推奨)", "Cory Wong")
    print(f"  「{folder}」のプリセットを 1 つずつ本体に読み込みます。音が切り替わり、"
          "本体で保存していない編集は消えます。")
    if not run.ask("続けますか?"):
        return SKIP, "ユーザーが中止"
    job = backup.create_job(run.device, [folder], False, run.mock)
    result = backup.run_job(run.device, job)
    latest = backup.load_latest(run.mock)
    run.state["backup_items"] = latest
    expected = run.state.get("baseline", {}).get(folder)
    ok = result["commit"] is not None and result["failed"] == 0 and \
        (expected is None or result["files"] == expected)
    return (PASS if ok else FAIL), (f"{result['files']} 件、失敗 {result['failed']} 件、"
                                    f"コミット {str(result['commit'])[:8]}、ジョブ {job.id}")


@step("2 読み取り", "健康診断と資料(バックアップから、読み込みなし)",
      "要対応・注意・情報の件数が出る。HTML と PDF の資料ができ、PDF の日本語が読める",
      "PDF の日本語が豆腐(□)になるなら、日本語フォントが見つかっていない。結果を記録する")
def s_health_sheets(run: Run):
    from qcsuite.features import backup, health, sheets
    items = run.state.get("backup_items") or backup.load_latest(run.mock)
    if not items:
        return SKIP, "バックアップがないため省略"
    report = health.scan(items, "backup")
    out = paths.sub("selftest", run.mock)
    html = out / "sheets.html"
    html.write_text(sheets.sheet_html(items[:2]))
    pdf = sheets.sheet_pdf(items[:1], out / "sheets.pdf")
    font = sheets.japanese_font()
    seen = run.ask(f"{html} と {out / 'sheets.pdf'} を開いて、日本語とチェーン図が読めますか?")
    ok = html.stat().st_size > 1000 and (out / "sheets.pdf").exists() and seen
    return (PASS if ok else FAIL), (f"診断 {json.dumps(report['counts'], ensure_ascii=False)}、"
                                    f"PDF {pdf.get('pages', '?')} ページ、フォント {font or 'なし'}")


@step("3 sandbox", "言葉で操作: プレビュー・適用・取り消し(保存しない)",
      "プレビューでは何も変わらない。「少し暗く」を適用すると高域が少し下がり、取り消すと戻る",
      "画面の値は変わるのに音が変わらなければ、そのブロック名を記録する(文字列パラメーターと同じ現象の可能性)",
      changes_sound=True)
def s_panel(run: Run):
    from qcsuite.features import panel
    device = run.device
    device.read_stored(FACTORY, _ref(FACTORY_REFS[0])["position"])
    before = device.grid()
    preview = panel.preview(device, "少し暗く")
    if device.grid() != before:
        return FAIL, "プレビューでグリッドが変わりました"
    if preview.get("not_understood") or not preview["patch"]["ops"]:
        return FAIL, f"指示を解釈できませんでした: {preview.get('not_understood')}"
    panel.apply(device, preview["patch"], instruction="少し暗く")
    changed = device.grid() != before
    darker = run.ask("音が少し暗くなりましたか?")
    panel.undo(device)
    restored = presets.diff(presets.normalize(device.current_preset(), device.catalog),
                            presets.normalize(device.current_preset(), device.catalog)) == [] and \
        _numeric_equal(before, device.grid())
    back = run.ask("元の音に戻りましたか?")
    ok = changed and restored and darker and back
    return (PASS if ok else FAIL), (f"変更 {len(preview['patch']['ops'])} 操作、適用で変化={changed}、"
                                    f"取り消しで復帰={restored}、耳: 暗く={darker} 戻った={back}")


def _numeric_equal(a: dict, b: dict) -> bool:
    def values(g):
        return {(x["row"], x["col"], p["name"]): p.get("value") for x in g["blocks"]
                for p in x["params"] if "value" in p}
    va, vb = values(a), values(b)
    return va.keys() == vb.keys() and all(
        va[k] is None or vb[k] is None or abs(va[k] - vb[k]) < 1e-3 for k in va)


@step("3 sandbox", "編集ウォッチャー: 本体で回したノブを記録",
      "本体でノブを 1 つ回すと、その値の変化が 1 件として記録される。本体の音は途切れない",
      "何も記録されなければ読み取りの応答が落ちている可能性。間隔を広げて再試行し、だめなら記録して止める")
def s_watch(run: Run):
    from qcsuite.features import watch
    device = run.device
    watcher = watch.Watcher(device, run.mock, interval=0.5)
    watcher.start()
    if run.mock:
        grid = device.grid()
        block = next(b for b in grid["blocks"] if any("real" in p for p in b["params"]))
        knob = next(p for p in block["params"] if "real" in p)
        device.apply_patch({"ops": [{"op": "set_param", "row": block["row"], "col": block["col"],
                                     "param": knob["name"], "value": 0.123}]})
    else:
        input("  本体の画面でノブを 1 つ回して、終わったら Enter を押してください...")
    found = watcher.poll()
    watcher.stop()
    changes = [e for e in found if e.get("type") not in ("scene", "segment")]
    return (PASS if changes else FAIL), f"記録 {len(found)} 件、セッション {watcher.session.id}"


@step("4 リアンプ", "音量そろえ: 測定 → sandbox のコピーに補正 → 確認(±1 LU)",
      "補正後の各コピーが目標から ±1 LU 以内。元のプリセットは変わらない",
      "外れたら `qcsuite level verify` の数値を記録。行の出力レベルが効かない(手順 4 が FAIL)なら "
      "`--method gain_block` で再試行", changes_sound=True, reamps=True, writes=True)
def s_leveler(run: Run):
    from qcsuite.features import leveler
    device = run.device
    method = "lane" if run.state.get("lane_volume_effective", True) else "gain_block"
    refs = [_ref(r) for r in FACTORY_REFS[:2]]
    job = leveler.start_measure(device, presets=refs, di=run.state["di_name"], mock=run.mock)
    jobs.run(job, leveler.measure_work(run.rig, job))
    proposal = leveler.propose(leveler.results(job.id, run.mock), method=method)
    plan = leveler.build_plan(device, proposal, di=run.state["di_name"], measure_job=job.id)
    plan_mod.execute(plan, device, confirm=True)
    vjob = leveler.start_verify(leveler.load_session(plan.id, run.mock), mock=run.mock)
    jobs.run(vjob, leveler.measure_work(run.rig, vjob))
    summary = leveler.verify_summary(vjob.id, run.mock)
    ok = summary["count"] > 0 and summary["within_goal"] == summary["count"]
    return (PASS if ok else FAIL), (f"方法 {method}、目標 {summary['target']:.1f} LUFS、"
                                    f"{summary['within_goal']}/{summary['count']} 件が ±1 LU 以内、"
                                    f"最大のずれ {summary['worst_abs_lu']} LU")


@step("4 リアンプ", "ブラインド比較: 名前を伏せ、音量をそろえた候補ができる",
      "候補の音量差が 0.1 LU 以内で、名前が表示されない",
      "音量差が大きければ候補ごとの LUFS を記録する", changes_sound=True, reamps=True)
def s_tournament(run: Run):
    from qcsuite.features import tournament
    t = tournament.create_from_presets(run.rig, [_ref(r) for r in FACTORY_REFS],
                                       di=run.state["di_name"], title="selftest")
    levels = []
    for c in t["candidates"]:
        path = Path(c["audio"])
        path = path if path.is_absolute() else paths.home(run.mock) / path
        audio, sr = features.load(path)
        levels.append(lufs.integrated_loudness(audio, sr))
    spread = max(levels) - min(levels) if levels else None
    hidden = all(set(c) <= {"code", "audio"} for c in t["candidates"])
    ok = len(levels) >= 2 and spread is not None and spread <= 0.1 and hidden
    return (PASS if ok else FAIL), (f"候補 {len(levels)} 件、音量差 {spread:.2f} LU、名前は伏せた={hidden}、"
                                    f"除外 {len(t['excluded'])} 件、ID {t['id']}")


@step("4 リアンプ", "スイープ: ゲインを 2 段階で鳴らす",
      "2 構成ともレンダリングでき、ゲインを上げると 1〜5 kHz の flatness が上がり、クレストが下がる",
      "無音なら `qctone doctor --reamp-test`。きれいな音が素通しと判定されたら、その数値を記録",
      changes_sound=True, reamps=True)
def s_sweep(run: Run):
    from qcsuite.features import sweep
    device = run.device
    device.read_stored(FACTORY, _ref(FACTORY_REFS[0])["position"])
    device.prepare_reamp()
    grid = device.grid()
    amp = next((b for b in grid["blocks"] if b["category"] and "Amplifier" in b["category"]), None)
    if amp is None:
        return SKIP, "最初の工場出荷プリセットにアンプがありません"
    spec = {"title": "selftest", "di": run.state["di_name"], "seconds": DI_SECONDS,
            "base": "current", "mode": "grid",
            "cells": [{"row": amp["row"], "col": amp["col"], "params": {"gain": [0.15, 0.9]}}]}
    job, info = sweep.create(run.rig, spec)
    sweep.run(run.rig, job)
    rows = sorted(sweep.rows(job), key=lambda r: r.get("key", ""))
    flat = [r.get("flatness") for r in rows]
    crest = [r.get("crest") for r in rows]
    ok = len(rows) == 2 and None not in flat and flat[1] > flat[0] and crest[1] < crest[0]
    return (PASS if ok else FAIL), f"flatness {flat}、crest {crest}、ジョブ {job.id}"


@step("4 リアンプ", "一括リアンプ: DI × 2 プリセットのステム",
      "ステムがすべて同じ長さで DI の先頭にそろい、往復遅延が約 312 ms",
      "Logic で DI と並べてずれていないか耳と目で確かめる。ずれていれば stems.csv の遅延を記録",
      changes_sound=True, reamps=True)
def s_batch(run: Run):
    from qcsuite.features import batch
    job = batch.create(run.rig, [run.state["di_name"]], presets=FACTORY_REFS[:2])
    batch.run(run.rig, job)
    info = batch.finalize(job, run.mock)
    stems = batch.stems(job)
    lengths = set()
    for st in stems:
        path = Path(st["file"])
        path = path if path.is_absolute() else paths.home(run.mock) / path
        lengths.add(features.load(path)[0].shape[0])
    lat = [st.get("latency_ms") for st in stems if st.get("latency_ms") is not None]
    ok = len(stems) >= 2 and len(lengths) == 1 and lat and \
        all(abs(x - MEASURED_LATENCY_MS) < 30 for x in lat)
    return (PASS if ok else FAIL), (f"ステム {len(stems)} 本、長さ {sorted(lengths)}、遅延 {lat}、"
                                    f"フォルダ {info.get('folder', '')}")


@step("4 リアンプ", "弾いた音の同時録り(DI + ウェット)",
      "DI にギターが入り、ウェットは本体の音のまま、ずれは数 ms",
      "ウェットが無音なら行の入力が INPUT_1 か確認。DI が無音なら QCTONE_DRY_IN を確認",
      changes_sound=True)
def s_take(run: Run):
    from qcsuite.features import watch
    device = run.device
    device.read_stored(FACTORY, _ref(FACTORY_REFS[0])["position"])     # rows back on INPUT_1
    if not run.mock:
        input("  Enter を押したら 5 秒間ギターを弾いてください...")
    info = watch.take(run.rig, "selftest-take", 5.0, note="selftest")
    levels = info.get("levels", {})
    offset = info.get("offset_ms")
    ok = offset is not None and 0 <= offset < 20 and (levels.get("dry_peak_dbfs") or -99) > -40 \
        and (levels.get("wet_peak_dbfs") or -99) > -60
    return (PASS if ok else FAIL), (f"ずれ {offset} ms、DI ピーク {levels.get('dry_peak_dbfs')} dBFS、"
                                    f"ウェット ピーク {levels.get('wet_peak_dbfs')} dBFS、DI 名 {info.get('di')}")


# -- running ----------------------------------------------------------------------------

def _ordered() -> list[Step]:
    order = ["0 準備", "1 最初の確認", "2 読み取り", "3 sandbox", "4 リアンプ", "5 後片付け"]
    ordered = sorted(STEPS, key=lambda s: (order.index(s.group), STEPS.index(s)))
    for i, s in enumerate(ordered, 1):
        s.number = i
    return ordered


def _write_report(run: Run, finished: bool):
    lines = [f"# qcsuite 実機テスト結果({'モック' if run.mock else '実機'})", "",
             f"- 日時: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 状態: {'完了' if finished else '途中'}", "",
             "| # | 区分 | 手順 | 結果 | 詳細 |", "|---:|---|---|---|---|"]
    for r in run.results:
        detail = r["detail"].replace("|", "/").replace("\n", " ")
        lines.append(f"| {r['number']} | {r['group']} | {r['title']} | {r['status']} | {detail} |")
    failed = [r for r in run.results if r["status"] in (FAIL, STOP)]
    if failed:
        lines += ["", "## 失敗・中止した手順の対処", ""]
        for r in failed:
            lines.append(f"- {r['number']}. {r['title']}: {r['on_fail']}")
            if r.get("traceback"):
                lines += ["", "```", r["traceback"].rstrip(), "```"]
    run.report.parent.mkdir(parents=True, exist_ok=True)
    run.report.write_text("\n".join(lines) + "\n")


def _run(ctx, mock: bool, report_path, start_at: int, interactive: bool) -> int:
    """Run the steps; the confirmed setlist limit applies to this run only."""
    import os
    saved = os.environ.get("QCSUITE_SETLIST_LIMIT")
    try:
        return _run_steps(ctx, mock, report_path, start_at, interactive)
    finally:
        if saved is None:
            os.environ.pop("QCSUITE_SETLIST_LIMIT", None)
        else:
            os.environ["QCSUITE_SETLIST_LIMIT"] = saved


def _run_steps(ctx, mock: bool, report_path, start_at: int, interactive: bool) -> int:
    report = Path(report_path) if report_path else \
        paths.sub("selftest", mock) / f"report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    steps = _ordered()
    with open_rig(mock, allow_folders=ctx.allow_folders, what="qcsuite selftest") as rig:
        run = Run(ctx=ctx, rig=rig, report=report, mock=mock)
        di_mod.ensure_synthetic(mock)
        run.state["di_name"] = di_mod.SYNTH_NAME
        print(f"実機テスト: {len(steps)} 手順。レポート: {report}", file=sys.stderr)
        if interactive and not mock:
            run.state["di_name"] = run.ask_text("使う DI の名前(qcsuite di list)", di_mod.SYNTH_NAME)
        stopped = False
        for s in steps:
            if s.number < start_at:
                continue
            if stopped and s.group != "5 後片付け":
                run.results.append({"number": s.number, "group": s.group, "title": s.title,
                                    "status": SKIP, "detail": "前の手順で中止", "on_fail": s.on_fail})
                continue
            flags = "".join([" [音が変わる]" if s.changes_sound else "",
                             " [書き込み: sandbox のみ]" if s.writes else "",
                             " [リアンプ: 音が出る]" if s.reamps else ""])
            print(f"\n[{s.number}/{len(steps)}] {s.group}: {s.title}{flags}\n  期待: {s.expect}",
                  file=sys.stderr)
            if interactive and not mock and not run.ask("この手順を実行しますか?"):
                status, detail = SKIP, "ユーザーが飛ばした"
                tb = ""
            else:
                tb = ""
                try:
                    status, detail = s.fn(run)
                except Stopped as exc:
                    status, detail, stopped = STOP, str(exc), True
                except Exception as exc:
                    status, detail = FAIL, f"{type(exc).__name__}: {exc}"
                    tb = traceback.format_exc(limit=8)
            ctx.notices(rig.device)
            print(f"  → {status}: {detail}", file=sys.stderr)
            if status in (FAIL, STOP):
                print(f"  対処: {s.on_fail}", file=sys.stderr)
            run.results.append({"number": s.number, "group": s.group, "title": s.title,
                                "status": status, "detail": str(detail), "on_fail": s.on_fail,
                                "traceback": tb})
            _write_report(run, finished=False)
        _write_report(run, finished=True)
    print(f"\nレポート: {report}", file=sys.stderr)
    bad = [r for r in run.results if r["status"] in (FAIL, STOP)]
    return 1 if bad else 0


def run_mock(ctx, report_path=None) -> int:
    return _run(ctx, True, report_path, 1, interactive=False)


def run_hardware(ctx, report_path=None, start_at: int = 1) -> int:
    return _run(ctx, ctx.mock, report_path, start_at, interactive=True)


def steps_table() -> list[dict]:
    """The steps in order, for the hardware test plan and the tests."""
    return [{"number": s.number, "group": s.group, "title": s.title, "expect": s.expect,
             "on_fail": s.on_fail, "changes_sound": s.changes_sound, "writes": s.writes,
             "reamps": s.reamps} for s in _ordered()]
