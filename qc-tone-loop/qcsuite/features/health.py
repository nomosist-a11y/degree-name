"""Preset health check (idea A) and the pre-gig check (idea B).

A health check reads presets - preferably from the backup, so nothing on the
unit is recalled - and lists what would bite at rehearsal or on stage, each
finding with a severity and what to do about it:

* **要対応** - the preset will not do what the player expects: an input left on
  USB 5-8 after a reamp (silent with a guitar plugged in), no row reaching
  Out 1/2, a muted or "Off" output row, a block from an unowned plugin (its
  audio passes straight through, measured on the player's unit).
* **注意** - likely trouble: output level far from unity, every block
  bypassed, scenes used but unnamed, a chain that is probably heavy on DSP,
  a placeholder name.
* **情報** - worth knowing: capture blocks (fine when built on the unit; a
  change made from the computer would not sound), a strong input gate,
  identical content or the same name in other folders.

The DSP and level thresholds are heuristics, labelled as such; the unit's real
DSP budget is not public.

The gig check takes a saved setlist (:mod:`.setlist`) and asks, per song: does
the preset exist where the setlist says, is it healthy, is its loudness within
±1 LU of the setlist's median (from the leveler's measurements, or "未測定"),
is its input Input 1, does it reach Out 1/2, are its scenes named.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from qcsuite import jobs, paths, presets
from qcsuite.device import guard
from qcsuite.features import setlist as setlist_mod
from qcsuite.features import sheets_sources as src

MUST, WARN, INFO = "要対応", "注意", "情報"
ORDER = {MUST: 0, WARN: 1, INFO: 2}
REACHES_OUT = {"XLR_1_2", "XLR_1", "XLR_2", "MULTIPLE"}
OTHER_OUTS = {"OUT_3_4", "OUT_3", "OUT_4", "SEND_1_2", "SEND_1", "SEND_2", "USB_5", "USB_6",
              "USB_7", "USB_8", "USB_5_6", "USB_7_8", "USB_3", "USB_4", "USB_3_4"}
FEEDS_NEXT = {"NEXT_ROW_3", "NEXT_ROW_4", "NEXT_ROW_3_4"}
GUITAR_INPUTS = {"INPUT_1", "INPUT_2", "INPUT_1_2"}
CAPTURE_MODELS = (14000, 14001)
#: Heuristics, not the unit's real budget (which is not public).
HEAVY_GAIN_STAGES = 3          # amps + captures
HEAVY_REVERBS = 3
HEAVY_BLOCKS = 14
LEVEL_LOW_DB, LEVEL_HIGH_DB = -20.0, 6.0
PLACEHOLDER = re.compile(r"^(new preset|untitled|test\d*|a+|x+|aaaa+|copy|無題)$", re.I)
LU_TOLERANCE = 1.0


def _issue(severity, code, message, fix, where=""):
    return {"severity": severity, "code": code, "message": message, "fix": fix, "where": where}


def _rows_with_content(n: dict) -> list[dict]:
    with_blocks = {b["row"] for b in n["blocks"]}
    return [r for r in n["rows"] if r["row"] in with_blocks or r["in_port"] != "EMPTY"]


def check_preset(n: dict, *, same_content: list[str] = (), same_name: list[str] = ()) -> list[dict]:
    """Findings for one normalized preset (see the module docstring)."""
    out = []
    rows = _rows_with_content(n)
    # Routing: inputs.
    for r in rows:
        if r["in_port"].startswith("USB_"):
            out.append(_issue(
                MUST, "usb_input", f"行{r['row'] + 1} の入力が {r['in_port']} のままです。ギターをつないでも鳴りません"
                "(リアンプ用の設定の残りの可能性)。",
                "本体でその行の入力を Input 1 に戻して保存する。qcsuite からは sandbox のコピーで直す。",
                f"行{r['row'] + 1}"))
    if rows and not any(r["in_port"] in GUITAR_INPUTS for r in rows) and \
            not any(r["in_port"].startswith("USB_") for r in rows):
        out.append(_issue(WARN, "no_guitar_input", "Input 1/2 から始まる行がありません。",
                          "意図した入力(Return など)か、本体で確認する。"))
    # Routing: outputs.
    outs = [r for r in rows if r["out_port"] in REACHES_OUT]
    if not outs:
        others = [r["out_port"] for r in rows if r["out_port"] in OTHER_OUTS]
        if others:
            out.append(_issue(WARN, "other_output", f"Out 1/2 ではなく {', '.join(sorted(set(others)))} に"
                              "出ています。", "接続している出力と合っているか確認する。"))
        elif n["blocks"]:
            out.append(_issue(MUST, "no_output", "Out 1/2 に届く行がありません(無音になります)。",
                              "本体で最後の行の出力を Output 1/2 にする。"))
    for r in rows:
        if r["out_port"] == "EMPTY" and r["row"] in {b["row"] for b in n["blocks"]} and \
                r["in_port"] != "EMPTY":
            out.append(_issue(WARN, "dead_end", f"行{r['row'] + 1} は入力があるのに出力先がありません。",
                              "出力か次の行への接続を設定する。", f"行{r['row'] + 1}"))
    # Output rows: mute and level.
    for r in outs:
        lane = r.get("lane", {})
        mute = (lane.get("MUTE") or {}).get("value") or 0
        vol = lane.get("VOLUME") or {}
        where = f"行{r['row'] + 1}"
        if mute >= 0.5:
            out.append(_issue(MUST, "muted", f"{where} の出力がミュートされています。",
                              "本体で行の出力の MUTE を外す。", where))
        if vol.get("value") is not None and vol["value"] <= 0:
            out.append(_issue(MUST, "volume_off", f"{where} の出力レベルが Off です。",
                              "本体で行の出力レベルを上げる。", where))
        elif vol.get("db") is not None and vol["db"] < LEVEL_LOW_DB:
            out.append(_issue(WARN, "volume_low", f"{where} の出力レベルが {vol['db']:+.1f} dB と低めです。",
                              "音量そろえ(qcsuite level)で測って確かめる。", where))
        elif vol.get("db") is not None and vol["db"] > LEVEL_HIGH_DB:
            out.append(_issue(WARN, "volume_high", f"{where} の出力レベルが {vol['db']:+.1f} dB と高めです"
                              "(クリップの可能性)。", "音量そろえで測って確かめる。", where))
    # Gate.
    for r in rows:
        gate = r.get("gate", {})
        reduction = (gate.get("NOISE REDUCTION") or {}).get("value")
        if reduction is not None and reduction >= 0.8:
            out.append(_issue(INFO, "strong_gate", f"行{r['row'] + 1} の入力ゲートが強め"
                              f"({reduction * 100:.0f}%)です。", "サステインが切れないか弾いて確かめる。"))
    # Blocks.
    for b in n["blocks"]:
        where = presets.block_label(b)
        if b.get("inert_plugin"):
            out.append(_issue(MUST, "inert_plugin", f"{where} は未所有のプラグイン({b['inert_plugin']})です。"
                              "音は素通しになります。",
                              "別のブロックに置き換えるか、プラグインを入手する。", where))
        if b["model_id"] in CAPTURE_MODELS:
            out.append(_issue(INFO, "capture", f"{where}: キャプチャー「{b.get('capture', '?')}」。本体で作った"
                              "ものなら問題ありません。パソコンから変えた場合は音に反映されません。",
                              "変えるときは本体の画面で読み込む。", where))
    if n["blocks"] and all(b.get("bypass") and all(b["bypass"]["scenes"]) for b in n["blocks"]):
        out.append(_issue(WARN, "all_bypassed", "すべてのブロックがバイパスされています。",
                          "意図したものか本体で確認する。"))
    # DSP heuristics.
    gain_stages = sum(1 for b in n["blocks"] if b["category"] in
                      ("Guitar Amplifier", "Bass Amplifier", "Neural Capture", "Neural Capture V2"))
    reverbs = sum(1 for b in n["blocks"] if b["category"] == "Reverb")
    if gain_stages >= HEAVY_GAIN_STAGES or reverbs >= HEAVY_REVERBS or len(n["blocks"]) >= HEAVY_BLOCKS:
        out.append(_issue(WARN, "dsp_heavy", f"アンプ/キャプチャー {gain_stages}、リバーブ {reverbs}、"
                          f"ブロック {len(n['blocks'])} 個。DSP が重い可能性があります(目安)。",
                          "ブロックを足すと置けないことがある。本体の DSP 表示で確認する。"))
    # Scenes.
    uses_scenes = any(b.get("bypass") and b["bypass"]["scene_mode"] and len(set(b["bypass"]["scenes"])) > 1
                      for b in n["blocks"]) or any(
        e.get("scenes") for b in n["blocks"] for e in b["params"].values())
    if uses_scenes and not any(l.strip() for l in n.get("scene_labels", [])):
        out.append(_issue(WARN, "unnamed_scenes", "シーンで切り替えているのに、シーン名がありません。",
                          "本体でシーン名を付ける(ステージで迷わないように)。"))
    # Names and duplicates.
    name = (n.get("name") or "").strip()
    if not name:
        out.append(_issue(WARN, "no_name", "名前がありません。", "本体で名前を付ける。"))
    elif PLACEHOLDER.match(name):
        out.append(_issue(INFO, "placeholder_name", f"「{name}」は仮の名前のようです。",
                          "使うなら分かる名前で保存し直す(名前の変更命令は無いので本体で)。"))
    if same_content:
        out.append(_issue(INFO, "same_content", "中身が同じプリセット: " + ", ".join(same_content),
                          "整理するなら残す方を決めて本体で(qcsuite library dupes)。"))
    if same_name:
        out.append(_issue(INFO, "same_name", "同じ名前が別のフォルダにもあります: " + ", ".join(same_name),
                          "セットリストでは場所(フォルダ:スロット)で指定する。"))
    return sorted(out, key=lambda i: ORDER[i["severity"]])


def scan(items: list[dict], source: str = "") -> dict:
    """Check every preset; duplicates are judged across the whole selection."""
    by_print, by_name = defaultdict(list), defaultdict(list)
    for n in items:
        where = src.ref_text(n.get("folder"), n.get("position"))
        by_print[n.get("fingerprint")].append(where)
        by_name[(n.get("name") or "").lower()].append((n.get("folder"), where))
    results = []
    for n in items:
        where = src.ref_text(n.get("folder"), n.get("position"))
        same_content = [w for w in by_print.get(n.get("fingerprint"), []) if w != where]
        same_name = [w for f, w in by_name.get((n.get("name") or "").lower(), [])
                     if w != where and f != n.get("folder")]
        issues = check_preset(n, same_content=same_content, same_name=same_name)
        worst = issues[0]["severity"] if issues else None
        results.append({"name": n.get("name"), "where": where, "folder": n.get("folder"),
                        "position": n.get("position"), "issues": issues, "worst": worst})
    results.sort(key=lambda r: (ORDER.get(r["worst"], 3), r["where"]))
    counts = {s: sum(1 for r in results for i in r["issues"] if i["severity"] == s)
              for s in (MUST, WARN, INFO)}
    return {"scanned": len(items), "source": source, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "counts": counts, "presets": results}


def report_markdown(report: dict) -> str:
    c = report["counts"]
    lines = [f"# プリセット健康診断({report['at']})", "",
             f"対象 {report['scanned']} 件({report['source']}) — 要対応 {c[MUST]}、注意 {c[WARN]}、情報 {c[INFO]}",
             "", "DSP とレベルのしきい値は目安です。", ""]
    for r in report["presets"]:
        if not r["issues"]:
            continue
        lines.append(f"## {r['name']}({r['where']})")
        for i in r["issues"]:
            lines.append(f"- **{i['severity']}** {i['message']} → {i['fix']}")
        lines.append("")
    clean = [r for r in report["presets"] if not r["issues"]]
    if clean:
        names = ", ".join(f"{r['name']}({r['where']})" for r in clean)
        lines.append(f"問題なし: {names}")
    return "\n".join(lines) + "\n"


def health_dir(mock: bool) -> Path:
    return paths.sub("health", mock)


def save_report(report: dict, mock: bool) -> Path:
    path = health_dir(mock) / "last.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return path


def last_report(mock: bool) -> dict:
    path = health_dir(mock) / "last.json"
    if not path.exists():
        raise FileNotFoundError("まだ健康診断をしていません(qcsuite health scan)")
    return json.loads(path.read_text())


# -- folder scans read presets one by one: a resumable job -----------------------------

def create_folder_job(device, folder: str, mock: bool) -> jobs.Job:
    refs = src.folder_refs(device, folder)
    items = [{"key": f"{k}#{p}", "folder": k, "position": p} for k, p in refs]
    return jobs.create("health", {"folder": src.folder_key(folder)}, items, mock,
                       title=f"健康診断: {guard.folder_name(src.folder_key(folder))}")


def run_folder_job(device, job: jobs.Job, *, mock: bool, cancel=None, on_progress=None) -> dict:
    def work(item):
        return src.read(device, item["folder"], item["position"], mock=mock)
    jobs.run(job, work, cancel=cancel, on_progress=on_progress)
    return finish_folder_job(job, mock)


def finish_folder_job(job: jobs.Job, mock: bool) -> dict:
    items = [r["result"] for r in job.results() if r.get("ok")]
    report = scan(items, source=job.spec.get("title", "フォルダ"))
    save_report(report, mock)
    return report


# -- gig check (idea B) ------------------------------------------------------------------

def gig_check(device, setlist_name: str, *, mock: bool, read_missing: bool = True) -> dict:
    """The pre-gig checklist for a saved setlist."""
    setlist = setlist_mod.load_setlist(setlist_name, mock)
    setlist_mod.refresh_targets(device, setlist)
    measured = src.leveler_measurements(mock)
    backup = src.backup_by_key(mock)
    rows = []
    for s in setlist["songs"]:
        mode = (setlist.get("placement") or {}).get("mode", "map")
        where = s.get("target") if mode == "copy" else s.get("source")
        row = {"n": s["n"], "song": s["song"], "checks": [], "lufs": None, "where": None}
        rows.append(row)
        if where is None:
            row["checks"].append({"ok": False, "item": "プリセットが決まっていない", "detail": s["status"]})
            continue
        key, position = where["folder"], where["position"]
        row["where"] = src.ref_text(key, position)
        exists = any(p["index"] == position for p in device.presets(key))
        row["checks"].append({"ok": exists, "item": "プリセットがある",
                              "detail": row["where"] + ("" if exists else "(空きスロット)")})
        if not exists:
            continue
        n = backup.get((key, position))
        if n is None and read_missing:
            n = presets.normalize(device.read_stored(key, position), device.catalog,
                                  folder=key, position=position)
        if n is None:
            row["checks"].append({"ok": None, "item": "健康診断", "detail": "バックアップに無く、読み込みも省略"})
        else:
            issues = check_preset(n)
            must = [i for i in issues if i["severity"] == MUST]
            row["checks"].append({"ok": not must, "item": "要対応が無い",
                                  "detail": " / ".join(i["message"] for i in must) or "なし"})
            ins = [r["in_port"] for r in _rows_with_content(n)]
            row["checks"].append({"ok": any(p in GUITAR_INPUTS for p in ins) and
                                  not any(p.startswith("USB_") for p in ins),
                                  "item": "入力が Input 1", "detail": ", ".join(ins)})
            row["checks"].append({"ok": any(r["out_port"] in REACHES_OUT for r in n["rows"]),
                                  "item": "Out 1/2 に届く", "detail": ""})
            uses = any(i["code"] == "unnamed_scenes" for i in issues)
            labelled = any(l.strip() for l in n.get("scene_labels", []))
            row["checks"].append({"ok": not uses, "item": "シーン名",
                                  "detail": "あり" if labelled else "なし(シーンを使っていなければ問題なし)"})
        m = (measured or {}).get((key, position)) or (
            (measured or {}).get((s["source"]["folder"], s["source"]["position"])) if s.get("source") else None)
        row["lufs"] = m.get("lufs") if m else None
    values = [r["lufs"] for r in rows if r["lufs"] is not None]
    median = statistics.median(values) if values else None
    for r in rows:
        if measured is None or r["lufs"] is None:
            r["checks"].append({"ok": None, "item": "音量(±1 LU)", "detail": "未測定(qcsuite level measure)"})
        else:
            diff = r["lufs"] - median
            r["checks"].append({"ok": abs(diff) <= LU_TOLERANCE, "item": "音量(±1 LU)",
                                "detail": f"{r['lufs']:.1f} LUFS(中央値との差 {diff:+.1f} LU)"})
    for r in rows:
        states = [c["ok"] for c in r["checks"]]
        r["status"] = "NG" if False in states else ("未確認あり" if None in states else "OK")
    return {"setlist": setlist["name"], "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "median_lufs": median, "measured": measured is not None and bool(values),
            "songs": rows, "ok": all(r["status"] == "OK" for r in rows)}


def gig_markdown(g: dict) -> str:
    mark = {True: "[x]", False: "[ ] **NG**", None: "[ ] 未確認"}
    lines = [f"# ライブ前チェック: {g['setlist']}({g['at']})", "",
             "全体: " + ("すべて OK" if g["ok"] else "確認が必要な曲があります"), ""]
    if g["median_lufs"] is not None:
        lines.append(f"音量の中央値: {g['median_lufs']:.1f} LUFS(±{LU_TOLERANCE:.0f} LU 以内を OK)")
        lines.append("")
    for r in g["songs"]:
        lines.append(f"## {r['n']}. {r['song']}({r['where'] or '未解決'}) — {r['status']}")
        for c in r["checks"]:
            lines.append(f"- {mark[c['ok']]} {c['item']}" + (f": {c['detail']}" if c["detail"] else ""))
        lines.append("")
    lines.append("音の判断(この音で良いか)はチェックしていません。リハーサルで耳で確かめてください。")
    return "\n".join(lines) + "\n"


# -- CLI ----------------------------------------------------------------------------------

def _print_report(ctx, report: dict, fmt: str):
    if fmt == "json" or ctx.json:
        ctx.out(report)
    else:
        print(report_markdown(report), end="")


def cmd_scan(ctx, a):
    refs = [src.parse_ref(r) for r in (a.preset or [])]
    if not (refs or a.folder or a.current or a.from_backup):
        raise SystemExit("--from-backup、--folder、--current、--preset のどれかを指定してください。")
    with ctx.rig("health scan") as rig:
        device = rig.device
        if a.from_backup:
            items = src.collect(device, mock=ctx.mock, from_backup=True, folder=a.folder)
            if not items:
                raise SystemExit("バックアップがありません(qcsuite backup run)。")
            report = scan(items, "バックアップ")
        elif a.folder:
            if a.resume:
                job = jobs.open_job(a.resume, ctx.mock)
            else:
                refs_f = src.folder_refs(device, a.folder)
                recalls = src.needs_recall(refs_f, ctx.mock)
                if recalls:
                    print(f"{recalls} 個のプリセットを本体に読み込みます(音が切り替わり、本体で保存していない"
                          "編集は消えます)。バックアップがあれば --from-backup で読み込みなしに診断できます。",
                          file=sys.stderr)
                    if not ctx.ask("続けますか?"):
                        raise SystemExit("中止しました。")
                job = create_folder_job(device, a.folder, ctx.mock)
            report = run_folder_job(device, job, mock=ctx.mock)
        else:
            if refs and src.needs_recall(refs, ctx.mock):
                print("指定のプリセットを本体に読み込みます(音が切り替わります)。", file=sys.stderr)
                if not ctx.ask("続けますか?"):
                    raise SystemExit("中止しました。")
            items = src.collect(device, mock=ctx.mock, refs=refs, current_grid=a.current)
            report = scan(items, "現在のグリッド" if a.current and not refs else "指定のプリセット")
    save_report(report, ctx.mock)
    _print_report(ctx, report, a.format)


def cmd_report(ctx, a):
    _print_report(ctx, last_report(ctx.mock), a.format)


def cmd_gig(ctx, a):
    read = not a.no_read
    if read:
        print("バックアップに無いプリセットは本体に読み込んで診断します(音が切り替わります)。"
              "読み込まない場合は --no-read。", file=sys.stderr)
        read = ctx.ask("読み込んでもよいですか?")
    with ctx.rig("health gig") as rig:
        g = gig_check(rig.device, a.name, mock=ctx.mock, read_missing=read)
    if a.format == "json" or ctx.json:
        ctx.out(g)
    else:
        print(gig_markdown(g), end="")


def add_cli(sub):
    p = sub.add_parser("health", help="プリセットの健康診断とライブ前チェック")
    s = p.add_subparsers(dest="health_cmd", required=True)
    c = s.add_parser("scan", help="プリセットを診断する")
    c.add_argument("--from-backup", action="store_true", help="最新のバックアップを診断(本体の読み込みなし)")
    c.add_argument("--folder", help="フォルダ全体(バックアップに無いものは読み込む = 音が切り替わる)")
    c.add_argument("--current", action="store_true", help="本体の現在のグリッド(副作用なし)")
    c.add_argument("--preset", action="append", metavar="FOLDER:SLOT")
    c.add_argument("--resume", metavar="JOB_ID", help="中断したフォルダ診断を再開する")
    c.add_argument("--format", choices=["md", "json"], default="md")
    c.set_defaults(fn=cmd_scan)
    c = s.add_parser("report", help="前回の診断結果を表示")
    c.add_argument("--format", choices=["md", "json"], default="md")
    c.set_defaults(fn=cmd_report)
    c = s.add_parser("gig", help="セットリストのライブ前チェック")
    c.add_argument("name", help="セットリストの名前(qcsuite setlist list)")
    c.add_argument("--no-read", action="store_true",
                   help="バックアップに無いプリセットを読み込まない(その分は未確認になる)")
    c.add_argument("--format", choices=["md", "json"], default="md")
    c.set_defaults(fn=cmd_gig)
