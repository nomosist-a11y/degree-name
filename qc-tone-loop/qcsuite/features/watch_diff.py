"""Structured changes between two normalized presets, and replaying them.

:func:`qcsuite.presets.diff` gives readable lines; the edit watcher needs more
than that - which row, column and parameter moved, so rapid knob turns can be
merged into one entry, and so the changes can be replayed as a qctone patch.

What cannot be replayed from the computer is marked ``manual`` instead of being
sent: string parameters (a cab's mic, IR references, a capture's file) and a
capture block's own knobs read back fine when written from the computer but do
not change the sound (measured on the player's unit), and per-scene values of
scene-mode parameters are not addressed here. Those come back as a list of
steps for the player to repeat on the unit's screen.
"""

from __future__ import annotations

import pyquadcortex as pq

from qcsuite import presets
from qcsuite.device import inert

TOLERANCE = 1e-3
MANUAL_NOTE = "本体で行った変更(パソコンからは再現できない可能性)"
#: Changes that mean "the unit loaded a different preset" rather than "the
#: player turned a knob" - a heuristic, not something the unit reports.
JUMP_STRUCTURE = 3
JUMP_TOTAL = 15


def _value_text(entry: dict | None):
    if entry is None:
        return None
    for k in ("option", "real", "text", "value"):
        if k in entry and entry[k] is not None:
            unit = entry.get("units", "") if k == "real" else ""
            return f"{entry[k]}{(' ' + unit) if unit else ''}"
    return None


def _same_param(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    if "text" in a or "text" in b:
        return a.get("text") == b.get("text")
    if (a.get("scenes") or b.get("scenes")) and a.get("scenes") != b.get("scenes"):
        return False
    va, vb = a.get("value"), b.get("value")
    if va is None or vb is None:
        return va is vb
    return abs(va - vb) <= TOLERANCE


def _manual_reason(block: dict, entry: dict | None) -> str | None:
    """Why a change cannot be reproduced from the computer (None = it can).

    ``entry`` None asks about the block itself (placing it); otherwise about
    one of its parameters.
    """
    if entry is None:
        if block.get("model_id") in inert.CAPTURE_MODELS:
            return "パソコンから置いたキャプチャーは本体で読み込むまで鳴らない"
        if block.get("inert_plugin"):
            return "未所有のプラグインは音が素通し"
        return None
    if "text" in entry:
        return "文字列パラメーター(マイク・IR・キャプチャーの指定)はパソコンから書いても音に反映されない"
    if block.get("model_id") in inert.CAPTURE_MODELS:
        return "キャプチャーブロックのノブはパソコンから書いても音に反映されない"
    if entry.get("scenes"):
        return "シーンごとの値は本体で設定する"
    return None


def _bypass_text(bp):
    if not bp:
        return None
    marks = "".join("-" if s else "o" for s in bp["scenes"])
    return f"{'シーン別' if bp['scene_mode'] else '共通'} {marks}"


def changes(a: dict, b: dict) -> list[dict]:
    """Every difference from ``a`` to ``b``, one dict per change.

    Each change has ``kind`` (name, routing, lane, gate, block_added,
    block_removed, block_replaced, param, bypass, scene_label), a ``key`` that
    identifies what moved (used to merge consecutive polls), ``where`` (text),
    ``before``/``after`` (display text) and ``manual`` (a reason, or None).
    """
    out = []

    def add(kind, key, where, before=None, after=None, manual=None, **extra):
        out.append({"kind": kind, "key": key, "where": where, "before": before,
                    "after": after, "manual": manual, **extra})

    if a.get("name") != b.get("name"):
        add("name", ("name",), "プリセット名", a.get("name"), b.get("name"))
    for ra, rb in zip(a["rows"], b["rows"]):
        row = ra["row"]
        label = f"行{row + 1}"
        for field, word in (("in_port", "入力"), ("out_port", "出力")):
            if ra[field] != rb[field]:
                add("routing", ("routing", row, field), f"{label} {word}", ra[field], rb[field],
                    row=row, field=field, value=rb[field])
        for name in sorted(set(ra["lane"]) | set(rb["lane"])):
            ea, eb = ra["lane"].get(name) or {}, rb["lane"].get(name) or {}
            if not _same_param(ea or None, eb or None):
                add("lane", ("lane", row, name), f"{label} 出力 {name}",
                    ea.get("db", ea.get("value")), eb.get("db", eb.get("value")),
                    row=row, param=name, value=eb.get("value"), db=eb.get("db"))
        for name in sorted(set(ra["gate"]) | set(rb["gate"])):
            ea, eb = ra["gate"].get(name) or {}, rb["gate"].get(name) or {}
            if not _same_param(ea or None, eb or None):
                add("gate", ("gate", row, name), f"{label} ゲート {name}",
                    ea.get("value"), eb.get("value"), row=row, param=name, value=eb.get("value"))
    cells_a = {(x["row"], x["col"]): x for x in a["blocks"]}
    cells_b = {(x["row"], x["col"]): x for x in b["blocks"]}
    for cell in sorted(set(cells_a) | set(cells_b)):
        ba, bb = cells_a.get(cell), cells_b.get(cell)
        row, col = cell
        if ba is None:
            add("block_added", ("block", row, col), presets.block_label(bb), None, bb["model"],
                manual=_manual_reason(bb, None), row=row, col=col, model_id=bb["model_id"])
            continue
        if bb is None:
            add("block_removed", ("block", row, col), presets.block_label(ba), ba["model"], None,
                row=row, col=col)
            continue
        if ba["model_id"] != bb["model_id"]:
            add("block_replaced", ("block", row, col), f"行{row + 1} 列{col + 1}",
                ba["model"], bb["model"], manual=_manual_reason(bb, None), row=row, col=col,
                model_id=bb["model_id"])
            continue
        for name in sorted(set(ba["params"]) | set(bb["params"]),
                           key=lambda k: (ba["params"].get(k) or bb["params"].get(k))["index"]):
            pa_, pb_ = ba["params"].get(name), bb["params"].get(name)
            if not _same_param(pa_, pb_):
                add("param", ("param", row, col, name), f"{presets.block_label(bb)} {name}",
                    _value_text(pa_), _value_text(pb_), manual=_manual_reason(bb, pb_),
                    row=row, col=col, param=name.split("#")[0], model=bb["model"])
        if ba.get("bypass") != bb.get("bypass"):
            bp = bb.get("bypass") or {}
            per_scene = bp.get("scene_mode") and len(set(bp.get("scenes", []))) > 1
            add("bypass", ("bypass", row, col), f"{presets.block_label(bb)} バイパス",
                _bypass_text(ba.get("bypass")), _bypass_text(bp),
                manual="シーンごとのバイパスは本体で設定する" if per_scene else None,
                row=row, col=col, bypassed=bool(bp.get("scenes", [False])[0]))
    for i, (la, lb) in enumerate(zip(a.get("scene_labels", []), b.get("scene_labels", []))):
        if la != lb:
            add("scene_label", ("scene_label", i), f"シーン {presets.SCENES[i]} の名前", la, lb,
                scene=i, value=lb)
    return out


def looks_like_another_preset(a: dict, b: dict, found: list[dict] | None = None) -> bool:
    """Whether ``b`` is probably a different preset loaded on the unit (heuristic)."""
    found = changes(a, b) if found is None else found
    if any(c["kind"] == "name" for c in found):
        return True
    structure = sum(1 for c in found if c["kind"].startswith("block_"))
    return structure >= JUMP_STRUCTURE or len(found) >= JUMP_TOTAL


def replay(a: dict, b: dict) -> dict:
    """What turns the grid ``a`` into ``b``, from the computer.

    Returns ``{"patch": qctone patch, "lane": [...], "gate": [...],
    "scene_labels": [...], "manual": [...]}``. Only the patch, lanes, gate and
    scene names are sent by :func:`apply`; ``manual`` lists the rest.
    """
    ops, lane, gate, labels, manual = [], [], [], [], []
    cells_b = {(x["row"], x["col"]): x for x in b["blocks"]}
    for c in changes(a, b):
        kind = c["kind"]
        if c["manual"] and kind not in ("block_added", "block_replaced"):
            manual.append(f"{c['where']}: {c['before']} → {c['after']}({c['manual']})")
            continue
        if kind == "routing":
            enum = pq.Input if c["field"] == "in_port" else pq.Output
            if c["value"] in enum.__members__:
                ops.append({"op": "set_input" if c["field"] == "in_port" else "set_output",
                            "row": c["row"], "port": c["value"]})
            else:
                manual.append(f"{c['where']}: {c['after']}(この経路はパッチで指定できない)")
        elif kind == "lane":
            lane.append({"row": c["row"], "param": c["param"], "value": c["value"], "db": c["db"]})
        elif kind == "gate":
            gate.append({"row": c["row"], "param": c["param"], "value": c["value"]})
        elif kind == "block_removed":
            ops.append({"op": "remove_block", "row": c["row"], "col": c["col"]})
        elif kind in ("block_added", "block_replaced"):
            block = cells_b[(c["row"], c["col"])]
            ops.append({"op": "set_block", "row": c["row"], "col": c["col"],
                        "model": block["model_id"]})
            if c["manual"]:
                manual.append(f"{c['where']}: {c['after']} を置いた({c['manual']})")
            for name, entry in block["params"].items():
                base = name.split("#")[0]
                reason = _manual_reason(block, entry)
                if reason:
                    if "text" in entry and entry["text"]:
                        manual.append(f"{presets.block_label(block)} {base}: "
                                      f"{_value_text(entry)}({reason})")
                    continue
                op = _param_op(block, base, entry)
                if op is not None:
                    ops.append(op)
            bp = block.get("bypass") or {}
            ops.append({"op": "bypass", "row": c["row"], "col": c["col"],
                        "bypassed": bool(bp.get("scenes", [False])[0])})
        elif kind == "param":
            block = cells_b[(c["row"], c["col"])]
            entry = next(v for k, v in block["params"].items() if k.split("#")[0] == c["param"])
            op = _param_op(block, c["param"], entry)
            if op is None:
                manual.append(f"{c['where']}: {c['after']}(選択肢の名前が不明)")
            else:
                ops.append(op)
        elif kind == "bypass":
            ops.append({"op": "bypass", "row": c["row"], "col": c["col"],
                        "bypassed": c["bypassed"]})
        elif kind == "scene_label":
            labels.append({"scene": c["scene"], "label": c["value"]})
    return {"patch": {"note": f"replay {a.get('name')} → {b.get('name')}", "ops": ops},
            "lane": lane, "gate": gate, "scene_labels": labels, "manual": manual}


def _param_op(block: dict, name: str, entry: dict) -> dict | None:
    if entry.get("value") is None:
        return None
    if "option" in entry:
        # Switches are never written as raw values: by option name, or not at all.
        if isinstance(entry["option"], str):
            return {"op": "set_option", "row": block["row"], "col": block["col"],
                    "param": name, "option": entry["option"]}
        return None
    return {"op": "set_param", "row": block["row"], "col": block["col"], "param": name,
            "value": float(min(1.0, max(0.0, entry["value"])))}


def apply(device, r: dict) -> dict:
    """Send a :func:`replay` to the live grid. Unsaved; returns what was sent."""
    result = {"applied": 0, "warnings": [], "manual": r["manual"]}
    if r["patch"]["ops"]:
        result = {**device.apply_patch(r["patch"]), "manual": r["manual"]}
    for item in r["lane"]:
        if item["param"] == "VOLUME" and item.get("db") is not None:
            device.set_lane_volume_db(item["row"], item["db"])
        elif item.get("value") is not None:
            device.set_lane_param(item["row"], item["param"], item["value"])
    for item in r["gate"]:
        if item.get("value") is not None:
            device.set_gate_param(item["row"], item["param"], item["value"])
    for item in r["scene_labels"]:
        device.set_scene_label(item["scene"], item["label"])
    result["lane"] = len(r["lane"])
    result["gate"] = len(r["gate"])
    return result
