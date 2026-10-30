"""言葉で操作するパネル (feature 5) and A/B 切り替え (idea E).

The panel shows the live grid and turns the player's words into grid edits:
「暗く」「ゲインを 6 に」「ブースト追加」「リバーブを外して」. It only operates.
Whether the result sounds better is for the player to hear - which is what the
A/B switch is for: two grid states, flipped with one click, nothing saved.

Flow, the same in the CLI and the web UI:

1. **preview** - the words become a qctone patch plus a summary
   (:mod:`.panel_rules`, or :mod:`.panel_claude` when chosen and available);
   nothing is sent. A phrase that is not understood refuses the whole
   instruction, so nothing half-understood reaches the unit.
2. **apply** - the patch is validated against the live grid again and applied
   (unsaved). The grid before and after goes to the history
   (``panel/history.jsonl``) with the words and the patch.
3. **undo / redo** - put the grid back to the state before (or after) a change.
   Structure, routing, numeric knobs, switches (by option name) and bypass come
   back; string parameters (capture, mic, IR) do not, and a change made on the
   unit's own screen in between is overwritten for the knobs the undo touches.

Saving is not the panel's job; the one exception is "sandbox に保存", which
builds a plan with a single save into a free sandbox slot (dry run first).

What sounds and what only reads back follows the facts measured on the
player's unit: knobs on capture blocks and on unowned-plugin blocks read back
but do not change the sound, and the panel says so on the grid and in every
preview that touches them.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from qcsuite import paths
from qcsuite import plan as plan_mod
from qcsuite.device import guard, inert
from qcsuite.errors import QcsuiteError
from qcsuite.features import panel_claude, panel_rules
from qctone import device as qdevice

ENGINES = ("rules", "claude")
SLOTS = ("A", "B")
REACHES_OUT = {"XLR_1_2", "XLR_1", "XLR_2", "MULTIPLE"}


class PanelError(QcsuiteError):
    """A panel request that cannot be carried out, with the reason."""


def panel_dir(mock: bool) -> Path:
    return paths.sub("panel", mock)


# -- the grid as the panel shows it ---------------------------------------------------

def _knob_text(p: dict) -> str:
    if "text" in p:
        return p["text"]
    if "option" in p:
        return str(p["option"])
    if p.get("real") is not None:
        unit = p.get("units") or ""
        return f"{p['real']}{(' ' + unit) if unit else ''}"
    return "" if p.get("value") is None else f"{p['value']}"


def block_flags(block: dict, catalog) -> list[str]:
    """Why edits to this block from the computer may not change the sound."""
    flags = []
    if block["model_id"] in inert.CAPTURE_MODELS:
        flags.append("キャプチャー: パソコンからの変更(キャプチャーの差し替え・つまみ)は音に反映されません")
    plugin = inert.inert_plugin(catalog.get(block["model_id"]) if catalog is not None else None)
    if plugin:
        flags.append(f"未所有のプラグイン({plugin}): 音は素通しです")
    texts = [p["name"] for p in block["params"] if "text" in p]
    if texts:
        flags.append(f"文字列パラメーター({', '.join(texts)})はパソコンから変えても音に反映されません")
    return flags


def grid_view(device) -> dict:
    """The live grid for display: 4 x 8 cells, knobs, flags. No side effects."""
    grid = device.grid()
    catalog = device.catalog
    live = device.current_preset()
    cells = {(b["row"], b["col"]): b for b in grid["blocks"]}
    rows = []
    for r in grid["rows"]:
        row = r["row"]
        try:
            lane_db = round(device.lane_volume_db(row, live), 1)
        except IndexError:
            lane_db = None
        line = []
        for col in range(qdevice.COLUMNS):
            b = cells.get((row, col))
            if b is None:
                line.append(None)
                continue
            flags = block_flags(b, catalog)
            line.append({
                "row": row, "col": col, "model": b["model"], "model_id": b["model_id"],
                "category": b.get("category"), "bypassed": b.get("bypassed"),
                "responds": not any(f.startswith(("キャプチャー", "未所有")) for f in flags),
                "flags": flags,
                "knobs": [{"name": p["name"], "display": _knob_text(p),
                           "switch": "options" in p, "options": p.get("options"),
                           "text": "text" in p}
                          for p in b["params"] if not p["name"].startswith("RESERVED")],
            })
        rows.append({"row": row, "in_port": r["in_port"], "out_port": r["out_port"],
                     "is_input": r["in_port"] not in ("EMPTY",),
                     "reaches_output": r["out_port"] in REACHES_OUT,
                     "lane_db": lane_db, "cells": line})
    return {"active_scene": grid.get("active_scene"), "rows": rows,
            "engines": engines(), "note": "表示は読み取りのみです(本体の音は変わりません)。"}


def grid_text(view: dict) -> str:
    lines = [f"シーン {view['active_scene']}"]
    for r in view["rows"]:
        blocks = [c for c in r["cells"] if c]
        if not blocks and not r["is_input"]:
            continue
        lane = "" if r["lane_db"] is None else f" {r['lane_db']:+.1f} dB"
        lines.append(f"行{r['row'] + 1}: [{r['in_port']}] → [{r['out_port']}]{lane}")
        for c in blocks:
            mark = " (バイパス)" if c["bypassed"] else ""
            lines.append(f"  列{c['col'] + 1} {c['model']}{mark}")
            knobs = [f"{k['name']}={k['display']}" for k in c["knobs"] if not k["text"]]
            if knobs:
                lines.append("      " + "  ".join(knobs))
            for f in c["flags"]:
                lines.append(f"      ! {f}")
    return "\n".join(lines)


# -- engines -------------------------------------------------------------------------------

def engines() -> dict:
    return {"rules": {"available": True, "reason": ""}, "claude": panel_claude.availability()}


def switch_violations(ops: list, grid: dict, catalog) -> list[str]:
    """Raw-value writes to switches (forbidden: switches move only by option name)."""
    cells = {(b["row"], b["col"]): b["model_id"] for b in grid["blocks"]}
    out = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "set_block":
            try:
                cells[(op["row"], op["col"])] = qdevice.resolve_model(catalog, op["model"]).id
            except (qdevice.PatchError, KeyError):
                pass
            continue
        if op.get("op") != "set_param":
            continue
        model = catalog.get(cells.get((op.get("row"), op.get("col")), -1))
        if model is None:
            continue
        try:
            spec = model.parameter(op.get("param", ""))
        except KeyError:
            continue
        if spec.option_count or spec.type in ("switch", "comboBox", "rotarySwitch"):
            out.append(f"{model.name} {spec.name} はスイッチです。数値では動かさず、選択肢の名前で"
                       "指定してください。")
    return out


def preview(device, instruction: str, engine: str = "rules", client=None) -> dict:
    """Words -> proposed patch + summary. Sends nothing to the unit."""
    if engine not in ENGINES:
        raise PanelError(f"engine は {', '.join(ENGINES)} のどれかです。")
    grid = device.grid()
    catalog = device.catalog
    fallback = None
    if engine == "claude":
        status = panel_claude.availability()
        if client is None and not status["available"]:
            fallback = f"Claude は使えません({status['reason']})。ルールで変換しました。"
            engine = "rules"
    if engine == "claude":
        try:
            result = panel_claude.translate(instruction, grid, catalog, client=client)
        except panel_claude.ClaudeUnavailable as exc:
            result = panel_rules.translate(instruction, grid, catalog)
            fallback = f"{exc} ルールで変換しました。"
    else:
        result = panel_rules.translate(instruction, grid, catalog)
    problems = list(result["not_understood"])
    problems += switch_violations(result["patch"]["ops"], grid, catalog)
    if not problems and result["patch"]["ops"]:
        try:
            qdevice.validate(result["patch"], grid, catalog)
        except qdevice.PatchError as exc:
            problems.append(f"パッチが本体のグリッドと合いません: {exc}")
    result["problems"] = problems
    result["fallback"] = fallback
    result["can_apply"] = not problems and bool(result["patch"]["ops"])
    result["scene"] = grid.get("active_scene")
    return result


def preview_text(result: dict) -> str:
    lines = [f"[{result['engine']}] {result['instruction']}"]
    if result.get("fallback"):
        lines.append(f"  注: {result['fallback']}")
    for s in result["summary"]:
        lines.append(f"  - {s}")
    for w in result["warnings"]:
        lines.append(f"  ! {w}")
    for p in result["problems"]:
        lines.append(f"  × {p}")
    if not result["patch"]["ops"] and not result["problems"]:
        lines.append("  (変更はありません)")
    lines.append("  適用できます(--apply)" if result["can_apply"] else "  適用しません(何も送っていません)")
    return "\n".join(lines)


# -- history, undo, redo --------------------------------------------------------------------

def _history_path(mock: bool) -> Path:
    return panel_dir(mock) / "history.jsonl"


def history(mock: bool, limit: int | None = None) -> list[dict]:
    path = _history_path(mock)
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return entries[-limit:] if limit else entries


def _append(mock: bool, entry: dict):
    with open(_history_path(mock), "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _state(mock: bool) -> dict:
    """Which applied changes are currently undone (replayed from the log)."""
    undone: list[str] = []
    applied: list[dict] = []
    for e in history(mock):
        if e["kind"] == "apply":
            applied.append(e)
            undone.clear()             # a new change ends the redo chain
        elif e["kind"] == "undo":
            undone.append(e["of"])
        elif e["kind"] == "redo" and e["of"] in undone:
            undone.remove(e["of"])
    return {"applied": applied, "undone": undone}


def apply(device, patch: dict, instruction: str = "", engine: str = "rules",
          summary=None) -> dict:
    """Validate against the live grid again and apply. Unsaved; logged for undo."""
    ops = patch.get("ops") if isinstance(patch, dict) else None
    if not ops:
        raise PanelError("適用する操作がありません。")
    before = device.grid()
    violations = switch_violations(ops, before, device.catalog)
    if violations:
        raise PanelError("\n".join(violations))
    result = device.apply_patch(patch)
    after = device.grid()
    entry = {"kind": "apply", "id": uuid.uuid4().hex[:8], "at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "instruction": instruction, "engine": engine, "patch": patch,
             "summary": list(summary or []), "warnings": result["warnings"],
             "before": before, "after": after}
    _append(device.mock, entry)
    return {"id": entry["id"], "applied": result["applied"], "warnings": result["warnings"],
            "note": "保存はしていません。本体でプリセットを呼び出し直すと元に戻ります。"}


def _param_map(block: dict) -> dict:
    return {p["name"]: p for p in block["params"]}


def restore_patch(saved: dict, current: dict) -> tuple[dict, list[str]]:
    """A patch turning ``current`` back into ``saved`` (two qctone grid snapshots).

    Built on ``qctone.device.restore_ops``, then trimmed to what differs, with
    switches written by option name instead of raw value. String parameters are
    not restored (they would not sound anyway when written from here).
    """
    base = qdevice.restore_ops(saved, current)["ops"]
    cur_cells = {(b["row"], b["col"]): b for b in current["blocks"]}
    saved_cells = {(b["row"], b["col"]): b for b in saved["blocks"]}
    replaced = {(op["row"], op["col"]) for op in base if op["op"] == "set_block"}
    ops, notes = [], []
    for op in base:
        cell = (op["row"], op.get("col"))
        if op["op"] == "set_param":
            sp = _param_map(saved_cells[cell]).get(op["param"], {})
            cp = {} if cell in replaced else _param_map(cur_cells.get(cell, {"params": []})).get(
                op["param"], {})
            if "options" in sp:
                if cp.get("option") == sp.get("option") and cell not in replaced:
                    continue
                ops.append({"op": "set_option", "row": op["row"], "col": op["col"],
                            "param": op["param"], "option": sp["option"]})
                continue
            if "option" in sp:          # a list whose option names are unknown
                if cp.get("value") != sp.get("value"):
                    notes.append(f"行{op['row'] + 1} 列{op['col'] + 1} {op['param']}: 選択肢の名前が"
                                 "分からないため戻しません(本体で確認してください)。")
                continue
            if cell not in replaced and cp.get("value") is not None and \
                    abs(cp["value"] - op["value"]) < 1e-4:
                continue
        elif op["op"] == "bypass":
            if cell not in replaced and cur_cells.get(cell, {}).get("bypassed") == op["bypassed"]:
                continue
        ops.append(op)
    if any("text" in p for b in saved["blocks"] for p in b["params"]):
        notes.append("文字列パラメーター(キャプチャー・マイク・IR)は戻しません。")
    return {"note": "panel restore", "ops": ops}, notes


def _restore(device, saved: dict) -> dict:
    current = device.grid()
    patch, notes = restore_patch(saved, current)
    applied = device.apply_patch(patch)["applied"] if patch["ops"] else 0
    return {"applied": applied, "notes": notes}


def undo(device) -> dict:
    state = _state(device.mock)
    live = [e for e in state["applied"] if e["id"] not in state["undone"]]
    if not live:
        raise PanelError("取り消せる変更がありません。")
    entry = live[-1]
    result = _restore(device, entry["before"])
    _append(device.mock, {"kind": "undo", "of": entry["id"],
                          "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return {"undone": entry["id"], "instruction": entry["instruction"], **result}


def redo(device) -> dict:
    state = _state(device.mock)
    if not state["undone"]:
        raise PanelError("やり直せる変更がありません。")
    entry_id = state["undone"][-1]
    entry = next(e for e in state["applied"] if e["id"] == entry_id)
    result = _restore(device, entry["after"])
    _append(device.mock, {"kind": "redo", "of": entry_id,
                          "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return {"redone": entry_id, "instruction": entry["instruction"], **result}


def history_view(mock: bool, limit: int = 30) -> list[dict]:
    state = _state(mock)
    out = []
    for e in state["applied"][-limit:]:
        out.append({"id": e["id"], "at": e["at"], "instruction": e["instruction"],
                    "engine": e["engine"], "summary": e["summary"],
                    "ops": len(e["patch"]["ops"]), "undone": e["id"] in state["undone"]})
    return out


# -- A/B -------------------------------------------------------------------------------------------

def _ab_path(mock: bool) -> Path:
    return panel_dir(mock) / "ab.json"


def ab_state(mock: bool) -> dict:
    path = _ab_path(mock)
    return json.loads(path.read_text()) if path.exists() else {"A": None, "B": None, "active": None}


def _save_ab(mock: bool, state: dict):
    _ab_path(mock).write_text(json.dumps(state, ensure_ascii=False) + "\n")


def _check_slot(slot: str) -> str:
    slot = slot.upper()
    if slot not in SLOTS:
        raise PanelError("A か B を指定してください。")
    return slot


def ab_capture(device, slot: str, label: str = "") -> dict:
    slot = _check_slot(slot)
    state = ab_state(device.mock)
    state[slot] = {"grid": device.grid(), "label": label or f"{slot} の状態",
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    state["active"] = slot
    _save_ab(device.mock, state)
    return ab_summary(device.mock)


def ab_from_last(device) -> dict:
    """A = before the last applied change, B = after it; the unit stays on B."""
    state = _state(device.mock)
    live = [e for e in state["applied"] if e["id"] not in state["undone"]]
    if not live:
        raise PanelError("適用した変更がまだありません。")
    e = live[-1]
    ab = {"A": {"grid": e["before"], "label": f"変更前({e['instruction']})", "at": e["at"]},
          "B": {"grid": e["after"], "label": f"変更後({e['instruction']})", "at": e["at"]},
          "active": "B"}
    _save_ab(device.mock, ab)
    return ab_summary(device.mock)


def ab_switch(device, slot: str) -> dict:
    """Put the live grid into the stored state ``slot`` (unsaved)."""
    slot = _check_slot(slot)
    state = ab_state(device.mock)
    if not state.get(slot):
        raise PanelError(f"{slot} がまだ記録されていません。")
    result = _restore(device, state[slot]["grid"])
    state["active"] = slot
    _save_ab(device.mock, state)
    return {"active": slot, **result,
            "note": "保存はしていません。耳で聴き比べてください。"}


def grid_diff(a: dict, b: dict) -> list[str]:
    """Readable differences between two grid snapshots."""
    out = []
    for ra, rb in zip(a["rows"], b["rows"]):
        for key, word in (("in_port", "入力"), ("out_port", "出力")):
            if ra[key] != rb[key]:
                out.append(f"行{ra['row'] + 1} {word}: {ra[key]} → {rb[key]}")
    ca = {(x["row"], x["col"]): x for x in a["blocks"]}
    cb = {(x["row"], x["col"]): x for x in b["blocks"]}
    for cell in sorted(set(ca) | set(cb)):
        x, y = ca.get(cell), cb.get(cell)
        where = f"行{cell[0] + 1} 列{cell[1] + 1}"
        if x is None:
            out.append(f"{where}: {y['model']} を追加")
            continue
        if y is None:
            out.append(f"{where}: {x['model']} を削除")
            continue
        if x["model_id"] != y["model_id"]:
            out.append(f"{where}: {x['model']} → {y['model']}")
            continue
        px, py = _param_map(x), _param_map(y)
        for name in px:
            if name in py and _knob_text(px[name]) != _knob_text(py[name]):
                out.append(f"{where} {x['model']} {name}: {_knob_text(px[name])} → {_knob_text(py[name])}")
        if x.get("bypassed") != y.get("bypassed"):
            out.append(f"{where} {x['model']}: {'バイパス' if y.get('bypassed') else '有効'}に")
    return out


def ab_summary(mock: bool) -> dict:
    state = ab_state(mock)
    out = {"active": state.get("active"), "slots": {}}
    for slot in SLOTS:
        s = state.get(slot)
        out["slots"][slot] = None if not s else {"label": s["label"], "at": s["at"]}
    if state.get("A") and state.get("B"):
        out["diff"] = grid_diff(state["A"]["grid"], state["B"]["grid"])
    return out


# -- saving the result (the one write, via a plan) --------------------------------------------

def save_plan(device, name: str, folder: str | None = None) -> plan_mod.Plan:
    """A one-step plan: save the live grid into a free slot of the sandbox (default)."""
    if not name.strip():
        raise PanelError("保存する名前を入れてください。")
    if folder is None:
        key, first, occupied = plan_mod.sandbox_target(device)   # nothing is created yet
    else:
        key, first = guard.folder_key(folder), []
        occupied = {p["index"]: p["name"] for p in device.presets(key)}
    device.policy.check(key, "保存")
    position = next((i for i in range(256) if i not in occupied), None)
    if position is None:
        raise PanelError(f"「{guard.folder_name(key)}」に空きスロットがありません。")
    warnings = ["タグは保存されません(本体の仕様)。同じ名前があると本体が名前を変えます(20 文字 + _N)。"]
    usb = [r["row"] + 1 for r in device.grid()["rows"] if r["in_port"].startswith("USB")]
    if usb:
        warnings.append(f"行 {', '.join(map(str, usb))} の入力が USB のままです。ギターで弾くプリセットに"
                        "するなら、保存前に入力を INPUT_1 に戻してください。")
    return plan_mod.Plan(f"言葉で操作した結果を保存: {name}", steps=first + [plan_mod.Step(
        "save", key, position=position, name=name.strip(),
        note="現在のグリッドを保存")], warnings=warnings)


# -- CLI ------------------------------------------------------------------------------------------

def cmd_show(ctx, a):
    with ctx.rig("panel show") as rig:
        view = grid_view(rig.device)
    ctx.out(view, grid_text(view))


def cmd_say(ctx, a):
    with ctx.rig("panel say") as rig:
        result = preview(rig.device, a.instruction, engine=a.engine)
        print(preview_text(result), file=sys.stderr)
        if not a.apply:
            ctx.out(result, "(プレビューのみ。本体に送るには --apply を付けてください)")
            return
        if not result["can_apply"]:
            raise PanelError("理解できない部分や問題があるため、何も送っていません。")
        applied = apply(rig.device, result["patch"], a.instruction, result["engine"],
                        result["summary"])
    ctx.out(applied, f"適用しました({applied['applied']} 件、変更 ID {applied['id']})。{applied['note']}")


def cmd_undo(ctx, a):
    with ctx.rig("panel undo") as rig:
        result = undo(rig.device)
    lines = [f"取り消しました: {result['instruction']}({result['applied']} 件の操作)"]
    lines += [f"  注: {n}" for n in result["notes"]]
    ctx.out(result, "\n".join(lines))


def cmd_redo(ctx, a):
    with ctx.rig("panel redo") as rig:
        result = redo(rig.device)
    ctx.out(result, f"やり直しました: {result['instruction']}({result['applied']} 件の操作)")


def cmd_history(ctx, a):
    rows = history_view(ctx.mock, a.limit)
    text = "\n".join(f"{r['id']} {r['at']} {'(取り消し済み) ' if r['undone'] else ''}"
                     f"[{r['engine']}] {r['instruction']}  ({r['ops']} 件)" for r in rows)
    ctx.out(rows, text or "(履歴はありません)")


def cmd_ab(ctx, a):
    with ctx.rig("panel ab") as rig:
        d = rig.device
        if a.action == "capture":
            result = ab_capture(d, a.slot or "A", a.label or "")
        elif a.action == "switch":
            result = ab_switch(d, a.slot or "A")
        elif a.action == "last":
            result = ab_from_last(d)
        else:
            result = ab_summary(d.mock)
    if a.action == "switch":
        text = f"{result['active']} に切り替えました({result['applied']} 件の操作、保存なし)"
    else:
        lines = [f"いま本体にあるのは: {result.get('active') or '(未記録)'}"]
        for slot, info in result["slots"].items():
            lines.append(f"  {slot}: {info['label'] + ' ' + info['at'] if info else '(未記録)'}")
        if "diff" in result:
            lines.append("A と B の違い:")
            lines += [f"  {line}" for line in result["diff"]] or ["  (違いはありません)"]
        text = "\n".join(lines)
    ctx.out(result, text)


def cmd_save(ctx, a):
    with ctx.rig("panel save") as rig:
        plan = save_plan(rig.device, a.name, a.to)
        result = ctx.run_plan(plan, rig.device, apply=a.apply)
    ctx.out(result, "保存しました。" if not result.get("dry_run") else "(dry-run)")


def add_cli(sub):
    p = sub.add_parser("panel", help="言葉で操作するパネル(グリッド表示・言葉→操作・取り消し・A/B)")
    s = p.add_subparsers(dest="panel_cmd", required=True)
    c = s.add_parser("show", help="現在のグリッドとつまみを表示(副作用なし)")
    c.set_defaults(fn=cmd_show)
    c = s.add_parser("say", help="言葉をパッチに変換して表示。--apply で本体に送る(保存はしない)")
    c.add_argument("instruction", help="例: 「少し暗く」「ゲインを 6 に」「ブースト追加」")
    c.add_argument("--engine", choices=ENGINES, default="rules",
                   help="rules(既定)か claude(ANTHROPIC_API_KEY が必要)")
    c.add_argument("--apply", action="store_true", help="プレビューのあと本体に送る")
    c.set_defaults(fn=cmd_say)
    c = s.add_parser("undo", help="直前の変更を取り消す")
    c.set_defaults(fn=cmd_undo)
    c = s.add_parser("redo", help="取り消した変更をやり直す")
    c.set_defaults(fn=cmd_redo)
    c = s.add_parser("history", help="変更の履歴")
    c.add_argument("--limit", type=int, default=30)
    c.set_defaults(fn=cmd_history)
    c = s.add_parser("ab", help="A/B 切り替え: capture A|B, switch A|B, last, diff")
    c.add_argument("action", choices=("capture", "switch", "last", "diff"))
    c.add_argument("slot", nargs="?", choices=("A", "B", "a", "b"))
    c.add_argument("--label")
    c.set_defaults(fn=cmd_ab)
    c = s.add_parser("save", help="現在のグリッドを sandbox の空きスロットに保存する計画(--apply で実行)")
    c.add_argument("name")
    c.add_argument("--to", help="保存先フォルダ(既定: sandbox。許可したフォルダのみ)")
    c.add_argument("--apply", action="store_true")
    c.set_defaults(fn=cmd_save)
