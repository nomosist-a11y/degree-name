"""Rebuild the live grid from a normalized preset (for restores into the sandbox).

There is no host command that writes a whole preset: a full ``write_preset`` is
dropped by the unit (pyquadcortex docstring). So a restore replays the preset
as keyed edits - clear the grid, place each block, write its knobs, routing,
row levels, scene names - and then saves into a sandbox slot.

What this cannot bring back, and reports as warnings rather than hiding:

* captures, cab mics and IR references are written as strings and read back,
  but the unit does not play them until they are loaded on its screen
  (measured on the player's unit);
* blocks from unowned plugins pass audio through;
* per-scene bypass of a block whose cell is not already in scene mode (the
  scene-mode flag is not host-writable);
* list parameters whose option names are unknown (switches are never written
  as raw values - fact gathered on hardware), and the preset's tags.
"""

from __future__ import annotations

import pyquadcortex as pq

from qcsuite.device import inert

LANE_NAMES = ("VOLUME", "PAN", "MUTE", "SOLO")


def ops_for(normalized: dict, current_grid: dict) -> tuple[list[dict], list[dict], list[str]]:
    """``(qctone ops, string writes, warnings)`` turning the grid into ``normalized``."""
    ops, texts, warnings = [], [], []
    for b in current_grid["blocks"]:
        ops.append({"op": "remove_block", "row": b["row"], "col": b["col"]})
    for r in normalized["rows"]:
        if r["in_port"] in pq.Input.__members__:
            ops.append({"op": "set_input", "row": r["row"], "port": r["in_port"]})
        if r["out_port"] in pq.Output.__members__:
            ops.append({"op": "set_output", "row": r["row"], "port": r["out_port"]})
    for b in normalized["blocks"]:
        where = f"行{b['row'] + 1} 列{b['col'] + 1} {b['model']}"
        ops.append({"op": "set_block", "row": b["row"], "col": b["col"], "model": b["model_id"]})
        if b.get("inert_plugin"):
            warnings.append(f"{where}: 未所有のプラグインなので音は素通しです。")
        if b["model_id"] in inert.CAPTURE_MODELS:
            warnings.append(f"{where}: キャプチャー「{b.get('capture', '?')}」は本体の画面で"
                            "読み込み直すまで鳴りません。")
        for name, entry in b["params"].items():
            base = name.split("#")[0]
            if "text" in entry:
                if entry["text"]:
                    texts.append({"row": b["row"], "col": b["col"], "index": entry["index"],
                                  "text": entry["text"], "param": base})
                continue
            if entry.get("value") is None:
                continue
            if "option" in entry:
                if isinstance(entry["option"], str):
                    ops.append({"op": "set_option", "row": b["row"], "col": b["col"],
                                "param": base, "option": entry["option"]})
                else:
                    warnings.append(f"{where} {base}: 選択肢の名前が不明なので書きません"
                                    "(スイッチは値で触らない)。")
                continue
            ops.append({"op": "set_param", "row": b["row"], "col": b["col"], "param": base,
                        "value": float(min(1.0, max(0.0, entry["value"])))})
            if entry.get("scenes"):
                warnings.append(f"{where} {base}: シーンごとの値は復元しません(シーン A の値のみ)。")
        bp = b.get("bypass")
        if bp:
            scenes = bp["scenes"]
            if bp["scene_mode"] and len(set(scenes)) > 1:
                warnings.append(f"{where}: シーンごとのバイパスは復元できません(本体の画面で設定)。")
            ops.append({"op": "bypass", "row": b["row"], "col": b["col"],
                        "bypassed": bool(scenes[0]) if scenes else False})
    if texts:
        warnings.append(f"文字列パラメーター {len(texts)} 件(マイク・IR・キャプチャー)は読み戻せますが、"
                        "パソコンからの書き込みでは音に反映されません。")
    if normalized.get("tags"):
        warnings.append("タグは保存で引き継げません。")
    return ops, texts, warnings


def rebuild_grid(device, normalized: dict) -> list[str]:
    """Make the live grid match ``normalized``. Unsaved; returns the warnings."""
    grid = device.grid()
    ops, texts, warnings = ops_for(normalized, grid)
    device.apply_patch({"note": f"rebuild {normalized.get('name')}", "ops": ops})
    for t in texts:
        device.set_text(t["row"], t["col"], t["index"], t["text"])
    for r in normalized["rows"]:
        lane = r.get("lane", {})
        if "VOLUME" in lane and lane["VOLUME"].get("db") is not None:
            device.set_lane_volume_db(r["row"], lane["VOLUME"]["db"])
        for name in ("PAN", "MUTE"):
            if name in lane and lane[name].get("value") is not None:
                device.qc.set_lane_output(r["row"], name, value=lane[name]["value"])
    for i, label in enumerate(normalized.get("scene_labels", [])):
        device.set_scene_label(i, label)
    return warnings
