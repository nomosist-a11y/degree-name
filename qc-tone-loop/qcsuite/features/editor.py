"""エディター: the live grid drawn like the unit's own editor, with knobs that move.

The page (``static/editor.html``) shows the grid as rounded blocks on signal
lines and, for the selected block, a parameter screen: 10 parameters a page,
knobs, switches and drop-downs, in the unit's own screen order. This module
builds that view and carries out the edits.

What it relies on and what it refuses:

* **The view** comes from one read of the live preset (plus the active scene).
  Display attributes - screen order, skew, step names, "OFF" at the minimum -
  come from the unit's raw ModelRepo (:mod:`.modelrepo`, cached per firmware);
  without it the catalog order is used. Nothing is polled.
* **Every edit goes through** :class:`~qcsuite.device.core.Device` (its lock,
  its policy) and **into the panel's history** (:func:`panel.record`), so the
  panel's undo / redo / A/B work on editor changes and both pages show them.
* **One edit changes one thing**: the knob dragged, the switch clicked, the
  block's bypass. Switches move only by option name (from the preset's
  ``dynamic_steps``); a switch whose names the unit does not report is shown
  but not writable. String parameters (a cab's mic, a capture's file) are
  display only. Knobs on capture blocks and unowned-plugin blocks are writable
  but marked: they read back and do not change the sound.
* **A drag is one undo unit**: ``begin`` reads the grid (the "before" of the
  history entry) and checks the knob still shows what the page showed;
  ``move`` sends intermediate values (the page sends at most one in flight,
  about every 100 ms, always the latest; a move older than one already seen is
  dropped); ``end`` sends the final value through the validated patch path and
  records the entry.
* **Edits on the unit's own screen** in the meantime: before a write the
  value the page showed (``expect``) is compared with a fresh read; if they
  differ nothing is written and the page is told to reload (HTTP 409).
* **Nothing is saved.** Saving stays the panel's "sandbox に保存" plan.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from types import SimpleNamespace

from qcsuite.device import inert
from qcsuite.errors import QcsuiteError
from qcsuite.features import modelrepo, panel
from qctone import device as qdevice

ENGINE = "editor"
NOT_SAVED = "保存していません。本体でプリセットを呼び出し直すと元に戻ります。"
INERT_NOTE = "読み戻せるが音は変わらない"
STALE_SECONDS = 30.0
TEXT_WHY = "文字列パラメーターはパソコンから変えても音に反映されません(表示のみ)"


class EditorError(QcsuiteError):
    """An editor request that cannot be carried out (nothing was written)."""


class EditorConflict(EditorError):
    """The unit no longer shows what the page showed: reload, nothing was written."""


# -- display rules (mirrored in static/editor_core.js) --------------------------------

def proportion(p: dict, v: float) -> float:
    """JUCE-style knob position: ((v - min) / (max - min)) ** skew, clamped to 0..1."""
    lo, hi = p.get("min"), p.get("max")
    if lo is None or hi is None or hi == lo or v is None:
        return 0.0
    x = min(1.0, max(0.0, (v - lo) / (hi - lo)))
    return x ** (p.get("skew") or 1.0)


def knob_angle(p: dict, v: float) -> float:
    """Degrees from 12 o'clock, clockwise: -135 (7 o'clock) to +135 (5 o'clock)."""
    return -135.0 + 270.0 * proportion(p, v)


def value_from_proportion(p: dict, prop: float) -> float:
    """Inverse of :func:`proportion`, snapped to ``steps`` and integers like the knob."""
    lo, hi = p["min"], p["max"]
    prop = min(1.0, max(0.0, prop))
    skew = p.get("skew") or 1.0
    v = lo + (hi - lo) * (prop ** (1.0 / skew) if prop > 0 else 0.0)
    steps = p.get("steps")
    if steps and steps > 1:
        interval = (hi - lo) / (steps - 1)
        v = lo + round((v - lo) / interval) * interval
    if p.get("int"):
        v = round(v)
    return min(hi, max(lo, v))


def format_value(p: dict, v) -> tuple[str, str]:
    """(text, unit) as the parameter screen shows it."""
    if v is None:
        return "—", ""
    if p.get("placeholder"):
        return f"{v:.2f}", ""
    lo, hi = p.get("min"), p.get("max")
    if p.get("min_string") and lo is not None and v <= lo + 1e-9:
        return p["min_string"], ""
    if p.get("max_string") and hi is not None and v >= hi - 1e-9:
        return p["max_string"], ""
    unit = (p.get("units") or "").strip()
    u = unit.lower()
    if p.get("int") or u in ("%", "bpm") or (u in ("hz", "ms") and abs(v) >= 10):
        text = str(int(math.floor(v + 0.5)))
    elif v != 0 and abs(v) < 1:
        text = f"{v:.2f}"
    else:
        text = f"{v:.1f}"
    return text, unit


def value_text(p: dict, v) -> str:
    text, unit = format_value(p, v)
    return f"{text} {unit}" if unit else text


# -- the view ---------------------------------------------------------------------------

def _pinned_snapshot(device):
    """One read of the live preset, and the qctone snapshot of that same read."""
    preset = device.current_preset()
    scene = device.active_scene()
    catalog = device.catalog
    shim = SimpleNamespace(read_current_preset=lambda: preset, active_scene=lambda: scene,
                           catalog=catalog)
    return preset, qdevice.snapshot(shim), catalog


def _raw(preset, row, col, index):
    """The raw value at a wire index (float or str), or None."""
    import pyquadcortex as pq
    try:
        state = pq.param_state(preset, row, col, index)
    except (IndexError, AttributeError):
        return None
    if not state.values:
        return None
    value = state.values[0]
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _capture_name(text: str) -> str:
    """A capture's ``file_name`` is a 64-character hash followed by its name."""
    text = (text or "").strip()
    if len(text) > 64 and all(c in "0123456789abcdefABCDEF" for c in text[:64]):
        return text[64:].strip(" _-") or text
    return text


def _mic_name(text: str) -> str:
    return (text or "").rsplit("_", 1)[-1].strip()


def _knob(rp, snap, spec, raw, inert_block) -> dict:
    out = {"name": rp.name, "index": rp.index, "kind": "knob", "units": rp.units,
           "skew": rp.skew, "steps": rp.steps, "int": rp.show_as_integer,
           "min_string": rp.min_string, "max_string": rp.max_string,
           "placeholder": False, "inert": inert_block, "writable": False, "why_not": None,
           "min": rp.minimum, "max": rp.maximum, "default": rp.default, "value": None}
    if snap is not None and snap.get("real") is not None:
        out["value"] = snap["real"]
        lo, hi = snap.get("range") or (rp.minimum, rp.maximum)
        out["min"], out["max"] = lo, hi
        out["writable"] = spec is not None
    elif snap is not None and snap.get("value") is not None:
        # The catalog range is the wire's own 0..1 (pyquadcortex range_is_placeholder):
        # show and write the normalized value.
        out.update(placeholder=True, value=snap["value"], min=0.0, max=1.0, skew=1.0,
                   steps=None, int=False, min_string=None, max_string=None, units="",
                   default=None)    # the published default is in real units, not 0..1
        out["writable"] = spec is not None
    elif isinstance(raw, float) and rp.range_known:
        out["value"] = round(rp.minimum + min(1.0, max(0.0, raw)) * (rp.maximum - rp.minimum), 4)
    if not out["writable"]:
        out["why_not"] = ("qcsuite のカタログ(pyquadcortex)ではこのパラメーターを名前で扱えないため、"
                          "表示のみです。" if spec is None or snap is None else
                          "値が読めないため変更できません。")
    if out["value"] is None and out["why_not"] is None:
        out["why_not"] = "値が読めないため変更できません。"
    return out


def _select(rp, snap, spec, raw, inert_block) -> dict:
    options = list(snap["options"]) if snap and snap.get("options") else None
    labels = options or rp.labels()
    selected = None
    if options and snap.get("option") in options:
        selected = options.index(snap["option"])
    else:
        value = snap.get("value") if snap else (raw if isinstance(raw, float) else None)
        if value is not None and len(labels) > 1:
            selected = min(len(labels) - 1, max(0, round(value * (len(labels) - 1))))
    out = {"name": rp.name, "index": rp.index, "kind": "select", "labels": labels,
           "selected": selected, "options": options if spec is not None else None,
           "inert": inert_block, "writable": bool(options) and spec is not None, "why_not": None}
    if not out["writable"]:
        out["why_not"] = ("選択肢の名前が本体から読めないため、ここでは切り替えられません"
                          "(スイッチは数値では書きません)。" if spec is not None else
                          "qcsuite のカタログ(pyquadcortex)ではこのパラメーターを名前で扱えないため、"
                          "表示のみです。")
    return out


def block_view(block: dict, catalog, repo, preset) -> dict:
    row, col, model_id = block["row"], block["col"], block["model_id"]
    model = catalog.get(model_id) if catalog is not None else None
    rmodel = repo.get(model_id) if repo is not None else None
    flags = panel.block_flags(block, catalog)
    responds = not any(f.startswith(("キャプチャー", "未所有")) for f in flags)
    snaps = {p["index"]: p for p in block["params"]}
    if rmodel is not None:
        display = repo.display_params(model_id)
        mics = repo.mic_params(model_id)
        wire = repo.wire_params(model_id)
    else:
        wire = modelrepo.from_catalog(model)
        display = modelrepo.display_order(wire)
        mics = [p for p in wire if p.name.lower().startswith("ir selector")][:2]
    params = []
    for k, rp in enumerate(mics):
        snap = snaps.get(rp.index)
        text = snap.get("text") if snap and "text" in snap else _raw(preset, row, col, rp.index)
        if not isinstance(text, str):
            text = rp.default_text
        params.append({"name": f"MIC {'AB'[k]}", "index": rp.index, "kind": "text",
                       "text": _mic_name(text) or "—", "writable": False, "why_not": TEXT_WHY,
                       "inert": True})
    for rp in display:
        snap = snaps.get(rp.index)
        if snap is not None and snap["name"].lower() != rp.name.lower():
            snap = None
        spec = None
        if model is not None and rp.index < len(model.parameters) and \
                model.parameters[rp.index].name.lower() == rp.name.lower():
            try:
                if model.parameter(rp.name).index == rp.index:
                    spec = model.parameters[rp.index]
            except KeyError:
                spec = None
        raw = None if snap is not None else _raw(preset, row, col, rp.index)
        if rp.kind == "select" or (spec is not None and spec.option_count):
            params.append(_select(rp, snap, spec, raw, not responds))
        else:
            params.append(_knob(rp, snap, spec, raw, not responds))
    title = block["model"]
    for p in block["params"]:
        if p["name"] == "file_name" and p.get("text"):
            title = _capture_name(p["text"]) or title
    category = block.get("category") or (rmodel.category if rmodel else None)
    return {"row": row, "col": col, "model_id": model_id, "model": block["model"],
            "title": title, "category": category,
            "category_label": modelrepo.category_label(category),
            "color": modelrepo.color_key(category),
            "based_on": (rmodel.based_on if rmodel else "") or (model.based_on if model else ""),
            "bypassed": block.get("bypassed"), "responds": responds, "flags": flags,
            "lossy": _lossy(block),
            "params": params}


def repo_info(repo) -> dict:
    if repo is None:
        return {"source": "none", "firmware": "",
                "note": "本体のモデル一覧(ModelRepo)を読めなかったため、並び順と表示はカタログの"
                        "ままです(操作はできます)。"}
    where = {"device": "本体から取得", "cache": "キャッシュ", "file": "ファイル"}.get(repo.source,
                                                                                repo.source)
    return {"source": repo.source, "firmware": repo.firmware,
            "note": f"並び順と表示は本体のモデル一覧(ModelRepo、ファームウェア {repo.firmware}、"
                    f"{where})に従っています。"}


def load_repo(device):
    return modelrepo.load(device)


def _feeds(out_port: str, row: int) -> bool:
    """Whether a row output (NEXT_ROW_3, NEXT_ROW_4, NEXT_ROW_3_4) feeds ``row`` (0-based)."""
    if not out_port.startswith("NEXT_ROW_"):
        return False
    return str(row + 1) in out_port[len("NEXT_ROW_"):].split("_")


def view(device, repo="load") -> dict:
    """The editor's whole view. Reads the live preset once; no side effects."""
    if repo == "load":
        repo = load_repo(device)
    preset, grid, catalog = _pinned_snapshot(device)
    fix_skew(grid, repo)
    cells = {(b["row"], b["col"]): b for b in grid["blocks"]}
    rows = []
    for r in grid["rows"]:
        line = []
        for col in range(qdevice.COLUMNS):
            b = cells.get((r["row"], col))
            line.append(None if b is None else block_view(b, catalog, repo, preset))
        in_port = r["in_port"]
        if in_port == "EMPTY" and any(_feeds(o["out_port"], r["row"]) for o in grid["rows"]):
            in_port = "PREV_ROW"        # the unit shows "Prev. row" for a row fed by another
        rows.append({"row": r["row"], "in_port": in_port, "out_port": r["out_port"],
                     "in_label": port_label(in_port), "out_label": port_label(r["out_port"]),
                     "split": r.get("split"),
                     "is_input": in_port not in ("EMPTY",),
                     "reaches_output": r["out_port"] in panel.REACHES_OUT, "cells": line})
    labels = list(getattr(preset, "scene_labels", []) or [])
    out = {"scene": grid.get("active_scene"), "rows": rows, "repo": repo_info(repo),
            "preset": {"name": getattr(preset, "name", "") or "",
                       "dirty": device.preset_dirty()},
            "scene_labels": [str(x) for x in labels[:8]],
            "ports": {"in": [{"id": p, "label": port_label(p)} for p in PORTS_IN],
                      "out": [{"id": p, "label": port_label(p)} for p in PORTS_OUT]},
            "tempo": round(float(getattr(preset, "tempo", 0) or 0), 1) or None,
            "note": NOT_SAVED}
    # Row output, gate, splitter / mixer, scene colours, tempo, footswitches,
    # per-parameter scene mode and pedals: from the same read of the preset.
    from qcsuite.features import editor_extras      # imports this module
    return editor_extras.decorate(out, preset, catalog)


# -- writes ------------------------------------------------------------------------------

def fix_skew(grid: dict, repo) -> dict:
    """Correct the real values of skewed knobs in a qctone snapshot, in place.

    Measured on the player's unit (CorOS 4.1.0, d14e, 2026-11-10): the wire's
    0..1 for a parameter with a JUCE ``skew`` is the SKEWED position,
    ((real - min) / (max - min)) ** skew - a cab's OUTPUT VOLUME at 0.0 dB
    (-96..12, skew 4.93) reads 0.5595, a reverb's HIGH PASS at 80 Hz reads
    0.2146. pyquadcortex converts linearly, so its ``real`` is wrong for these.
    Each corrected param gets ``skew`` so writes can convert back.
    """
    if repo is None:
        return grid
    for b in grid["blocks"]:
        if repo.get(b["model_id"]) is None:
            continue
        wire = repo.wire_params(b["model_id"])
        for p in b["params"]:
            if p.get("real") is None or p.get("value") is None or p["index"] >= len(wire):
                continue
            rp = wire[p["index"]]
            if rp.name.lower() != p["name"].lower() or rp.skew == 1.0 or not rp.range_known:
                continue
            lo, hi = rp.minimum, rp.maximum
            p["real"] = round(lo + (hi - lo) * min(1.0, max(0.0, p["value"])) ** (1.0 / rp.skew), 4)
            p["skew"] = rp.skew
            p["range"] = [lo, hi]
    return grid


def _read(device) -> dict:
    """A fresh grid snapshot with skewed knobs corrected."""
    return fix_skew(device.grid(), load_repo(device))


def _to_wire(param: dict, real: float) -> float:
    """Real units -> the wire's 0..1 for a skewed knob."""
    lo, hi = param["range"]
    x = 0.0 if hi == lo else min(1.0, max(0.0, (real - lo) / (hi - lo)))
    return round(x ** param["skew"], 6)


def _block(grid: dict, row, col):
    return next((b for b in grid["blocks"] if b["row"] == row and b["col"] == col), None)


def _param(block: dict, name: str):
    wanted = (name or "").lower()
    return next((p for p in block["params"] if p["name"].lower() == wanted), None)


def _where(block) -> str:
    return f"行{block['row'] + 1} 列{block['col'] + 1} {block['model']}"


def _locate(grid: dict, req: dict) -> dict:
    row, col = req.get("row"), req.get("col")
    if not isinstance(row, int) or not isinstance(col, int):
        raise EditorError("row と col を指定してください。")
    block = _block(grid, row, col)
    if block is None or block["model_id"] != req.get("model_id"):
        raise EditorConflict(f"行{row + 1} 列{col + 1} のブロックが画面と違います(本体側で変わった"
                             "可能性があります)。何も書き込まず、画面を読み直します。")
    return block


def _changed(before: dict, after: dict) -> bool:
    strip = lambda g: (g["rows"], sorted((b["row"], b["col"], b["model_id"], b.get("bypassed"),
                                          tuple(p.get("value") for p in b["params"]))
                                         for b in g["blocks"]))
    return strip(before) != strip(after)


def _lossy(block: dict) -> bool:
    """A block whose state includes a string (capture file, IR): lost on re-placing."""
    return block["model_id"] in inert.CAPTURE_MODELS or \
        any("text" in p and p.get("text") for p in block.get("params") or [])


PORTS_IN = ["EMPTY", "INPUT_1", "INPUT_2", "INPUT_1_2", "RETURN_1", "RETURN_2", "RETURN_1_2",
            "USB_5", "USB_6", "USB_7", "USB_8", "USB_5_6", "USB_7_8"]
PORTS_OUT = ["EMPTY", "XLR_1_2", "OUT_3_4", "SEND_1_2", "XLR_1", "XLR_2", "OUT_3", "OUT_4",
             "SEND_1", "SEND_2", "USB_3", "USB_4", "USB_3_4", "USB_5", "USB_6", "USB_7",
             "USB_8", "USB_5_6", "USB_7_8", "NEXT_ROW_3", "NEXT_ROW_4", "NEXT_ROW_3_4",
             "MULTIPLE"]
_PORT_LABEL = {"EMPTY": "なし", "INPUT_1": "In 1", "INPUT_2": "In 2", "INPUT_1_2": "In 1/2",
               "RETURN_1": "Return 1", "RETURN_2": "Return 2", "RETURN_1_2": "Return 1/2",
               "PREV_ROW": "Prev. row", "XLR_1_2": "Out 1/2", "OUT_3_4": "Out 3/4",
               "SEND_1_2": "Send 1/2", "XLR_1": "Out 1", "XLR_2": "Out 2", "OUT_3": "Out 3",
               "OUT_4": "Out 4", "SEND_1": "Send 1", "SEND_2": "Send 2",
               "NEXT_ROW_3": "次の行へ(3)", "NEXT_ROW_4": "次の行へ(4)",
               "NEXT_ROW_3_4": "次の行へ(3/4)", "MULTIPLE": "Multi Out"}


def port_label(port: str) -> str:
    if port in _PORT_LABEL:
        return _PORT_LABEL[port]
    if port.startswith("USB_"):
        return "USB " + port[4:].replace("_", "/")
    return port


def _current(param: dict):
    """The value the page compares against: real, normalized, or option name."""
    if "option" in param:
        return param["option"]
    if param.get("real") is not None:
        return param["real"]
    return param.get("value")


def _check_expect(block: dict, param, expect, name: str):
    if expect is None:
        return
    if param is None:      # bypass
        if block.get("bypassed") is not None and bool(block["bypassed"]) != bool(expect):
            raise EditorConflict(f"{_where(block)} のバイパスが本体側で変わっていました。"
                                 "何も書き込まず、画面を読み直します。")
        return
    now = _current(param)
    if isinstance(now, str) or isinstance(expect, str):
        same = str(now) == str(expect)
    else:
        lo, hi = param.get("range") or (0.0, 1.0)
        tol = abs(hi - lo) * 0.002 + 1e-3
        same = now is not None and abs(float(now) - float(expect)) <= tol
    if not same:
        raise EditorConflict(f"{_where(block)} {name} が本体側で変わっていました"
                             f"(画面 {expect} / 本体 {now})。何も書き込まず、画面を読み直します。")


def _knob_op(block, param, name, req) -> dict:
    op = {"op": "set_param", "row": block["row"], "col": block["col"], "param": param["name"]}
    if "options" in param or "option" in param:
        raise EditorError(f"{name} はスイッチです。選択肢の名前で指定してください。")
    if req.get("real") is not None:
        if param.get("real") is None:
            raise EditorError(f"{name} は実際の単位の範囲が分からないため value(0〜1)で指定します。")
        lo, hi = param["range"]
        real = min(hi, max(lo, float(req["real"])))
        if param.get("skew", 1.0) != 1.0:
            op["value"] = _to_wire(param, real)     # pyquadcortex's real= would be linear
        else:
            op["real"] = round(real, 6)
    elif req.get("value") is not None:
        if param.get("real") is not None:
            raise EditorError(f"{name} は real(実際の単位)で指定してください。")
        op["value"] = round(min(1.0, max(0.0, float(req["value"]))), 6)
    else:
        raise EditorError("real か value を指定してください。")
    return op


def build_op(block: dict, req: dict) -> tuple[dict, dict | None]:
    """The single patch op for one request, and the snapshot param it changes."""
    if "bypassed" in req:
        if not isinstance(req["bypassed"], bool):
            raise EditorError("bypassed は true か false です。")
        return {"op": "bypass", "row": block["row"], "col": block["col"],
                "bypassed": req["bypassed"]}, None
    name = req.get("param") or ""
    param = _param(block, name)
    if param is None:
        raise EditorError(f"{_where(block)} に {name} というパラメーターはありません"
                          "(このパラメーターは qcsuite からは書けません)。")
    if "text" in param:
        raise EditorError(f"{name}: {TEXT_WHY}")
    if "option" in req:
        options = param.get("options")
        if not options:
            raise EditorError(f"{name} の選択肢の名前が本体から読めないため、切り替えられません。")
        if req["option"] not in options:
            raise EditorError(f"{name} に「{req['option']}」という選択肢はありません。")
        return {"op": "set_option", "row": block["row"], "col": block["col"],
                "param": param["name"], "option": req["option"]}, param
    return _knob_op(block, param, name, req), param


def _display(param: dict | None, block: dict, which: str):
    if param is None:
        b = block.get("bypassed")
        return "バイパス" if b else "有効"
    if which == "option" or "option" in param:
        return str(param.get("option"))
    p = {"min": (param.get("range") or [None, None])[0],
         "max": (param.get("range") or [None, None])[1], "units": param.get("units"),
         "placeholder": param.get("real") is None}
    return value_text(p, _current(param))


def _label(before_block, after_block, op) -> str:
    if op["op"] == "bypass":
        was = "バイパス" if before_block.get("bypassed") else "有効"
        now = "バイパス" if op["bypassed"] else "有効"
        return f"エディター: {_where(before_block)} {was} → {now}"
    name = op["param"]
    old = _display(_param(before_block, name), before_block, "")
    new = _display(_param(after_block, name), after_block, "") if after_block else "?"
    return f"エディター: {_where(before_block)} {name} {old} → {new}"


def _readback(after: dict, op: dict) -> dict:
    block = _block(after, op["row"], op["col"])
    if block is None:
        return {}
    if op["op"] == "bypass":
        return {"bypassed": block.get("bypassed")}
    param = _param(block, op["param"])
    if param is None:
        return {}
    if "options" in param:
        opts = param["options"]
        return {"option": param.get("option"),
                "selected": opts.index(param["option"]) if param.get("option") in opts else None}
    return {"value": _current(param)}


def _same_value(before_param, after_param) -> bool:
    if before_param is None or after_param is None:
        return False
    a, b = _current(before_param), _current(after_param)
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return a is not None and b is not None and abs(a - b) < 1e-4


def _commit(device, before: dict, block: dict, op: dict, extra_note: str = "") -> dict:
    result = device.apply_patch({"note": "editor", "ops": [op]})
    after = _read(device)
    after_block = _block(after, op["row"], op["col"])
    if op["op"] != "bypass" and after_block is not None and \
            _same_value(_param(block, op["param"]), _param(after_block, op["param"])):
        entry_id = None              # back where it started: nothing to undo
    elif op["op"] == "bypass" and after_block is not None and \
            block.get("bypassed") == after_block.get("bypassed"):
        entry_id = None
    else:
        label = _label(block, after_block, op) + extra_note
        entry_id = panel.record(device.mock, {"note": "editor", "ops": [op]}, before, after,
                                instruction=label, engine=ENGINE, summary=[label],
                                warnings=result["warnings"])
    return {"id": entry_id, "param": _readback(after, op), "warnings": result["warnings"],
            "note": "保存はしていません。本体でプリセットを呼び出し直すと元に戻ります。"}


def _refuse_switch_by_value(op: dict, grid: dict, device):
    """Switches move only by option name, even when the preset has no names for them."""
    if op["op"] != "set_param":
        return
    problems = panel.switch_violations([op], grid, device.catalog)
    if problems:
        raise EditorError("\n".join(problems))


class EditorSession:
    """Gestures in flight for one web app (one unit).

    Edits are serialized: a click or a new drag waits until the previous drag
    has ended, so each history entry's "before" holds only its own change.
    """

    IDLE_WAIT = 5.0
    CHECK_EVERY = 10          # moves between checks that the dragged block is still there

    def __init__(self, stale_seconds: float = STALE_SECONDS):
        self._gestures: dict[str, SimpleNamespace] = {}
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._edit = threading.RLock()      # one edit at a time: before -> write -> record
        self.stale_seconds = stale_seconds

    def _wait_idle(self, device):
        """Finish abandoned drags, then wait for the one in progress to end."""
        self.finish_stale(device)
        with self._idle:
            if not self._idle.wait_for(lambda: not self._gestures, timeout=self.IDLE_WAIT):
                raise EditorError("前のつまみの操作がまだ終わっていません。少し待ってから"
                                  "もう一度操作してください(何も書き込んでいません)。")

    def _drop(self, g):
        with self._idle:
            self._gestures.pop(g.id, None)
            self._idle.notify_all()

    # -- one-shot ----------------------------------------------------------------

    def set(self, device, req: dict) -> dict:
        """A click, a switch, a drop-down, a double-click reset, a bypass toggle."""
        with self._edit:
            return self._set(device, req)

    def _set(self, device, req: dict) -> dict:
        self._wait_idle(device)
        before = _read(device)
        block = _locate(before, req)
        op, param = build_op(block, req)
        _refuse_switch_by_value(op, before, device)
        _check_expect(block, param, req.get("expect"), req.get("param") or "バイパス")
        return _commit(device, before, block, op)

    def replace(self, device, req: dict) -> dict:
        """Put another model in a cell (set_block), confirmed by reading back."""
        with self._edit:
            return self._replace(device, req)

    def _replace(self, device, req: dict) -> dict:
        self._wait_idle(device)
        new_id = req.get("new_model_id")
        if not isinstance(new_id, int):
            raise EditorError("new_model_id を指定してください。")
        entry = _listing_entry(device, new_id)
        if entry is None:
            raise EditorError(f"モデル {new_id} はこの本体の一覧にありません。")
        if not entry["usable"]:
            raise EditorError(f"{entry['name']}: {entry['why_not']}")
        before = _read(device)
        row, col = req.get("row"), req.get("col")
        if req.get("model_id") is None:
            if not isinstance(row, int) or not isinstance(col, int):
                raise EditorError("row と col を指定してください。")
            if _block(before, row, col) is not None:
                raise EditorConflict(f"行{row + 1} 列{col + 1} は空いていませんでした。"
                                     "何も書き込まず、画面を読み直します。")
            block = None
        else:
            block = _locate(before, req)
            if block["model_id"] == new_id:
                return {"id": None, "model_id": new_id, "warnings": [], "note": NOT_SAVED}
        op = {"op": "set_block", "row": row, "col": col, "model": new_id}
        try:
            result = device.apply_patch({"note": "editor", "ops": [op]})
        except Exception as exc:                     # noqa: BLE001 - BlockRefused and friends
            if type(exc).__name__ != "BlockRefused":
                raise
            raise EditorError(f"{entry['name']} を置けませんでした(プリセットの処理能力(DSP)が"
                              f"足りない可能性があります)。何も変わっていません: {exc}") from None
        after = _read(device)
        placed = _block(after, row, col)
        if placed is None or placed["model_id"] != new_id:
            raise EditorError(f"{entry['name']} を送りましたが、読み戻すと置かれていませんでした"
                              "(DSP 不足の可能性)。画面を読み直します。")
        was = block["model"] if block else "空き"
        label = f"エディター: 行{row + 1} 列{col + 1} {was} → {placed['model']}(差し替え)"
        entry_id = panel.record(device.mock, {"note": "editor", "ops": [op]}, before, after,
                                instruction=label, engine=ENGINE, summary=[label],
                                warnings=result["warnings"])
        return {"id": entry_id, "model_id": new_id, "warnings": result["warnings"],
                "note": NOT_SAVED}

    # -- grid layout (Cortex Control's drag & drop, delete, copy / paste) -------------

    def layout(self, device, action: str, req: dict) -> dict:
        """move / delete / paste / duplicate / reset / port / split: one undo step each."""
        handler = {"move": self._relocate, "delete": self._delete, "paste": self._paste,
                   "duplicate": self._duplicate, "reset": self._reset, "port": self._port,
                   "split": self._split}.get(action)
        if handler is None:
            raise EditorError(f"知らない操作です: {action}")
        with self._edit:
            self._wait_idle(device)
            return handler(device, _read(device), req)

    def _layout_commit(self, device, before: dict, ops: list, label: str,
                       expect: dict | None = None, notes=(), stages=None) -> dict:
        """Apply ``ops`` (or ``stages``: lists of ops, each checked before the
        next), read back, record one history entry.

        ``expect`` maps (row, col) -> model id (None = empty) that must hold
        afterwards; a mismatch is reported (the entry stays, so undo works).
        A failure part-way still records what changed, so it can be undone.
        """
        stages = stages or [(ops, None)]
        warnings = list(notes)
        done = []
        try:
            for stage_ops, check in stages:
                result = device.apply_patch({"note": "editor", "ops": stage_ops})
                warnings += result["warnings"]
                done += stage_ops
                if check is not None:
                    check(_read(device))
        except Exception:
            if done or ops:
                after = _read(device)
                if _changed(before, after):
                    panel.record(device.mock, {"note": "editor", "ops": done}, before, after,
                                 instruction=label + "(途中で失敗)", engine=ENGINE,
                                 summary=[label], warnings=warnings)
            raise
        ops = done
        after = _read(device)
        for (row, col), model_id in (expect or {}).items():
            got = _block(after, row, col)
            if (got["model_id"] if got else None) != model_id:
                warnings.append(f"行{row + 1} 列{col + 1}: 本体の配置が想定と違います"
                                "(読み戻した結果を表示しています。取り消しで元に戻せます)。")
        entry_id = panel.record(device.mock, {"note": "editor", "ops": ops}, before, after,
                                instruction=label, engine=ENGINE, summary=[label],
                                warnings=warnings)
        return {"id": entry_id, "warnings": warnings, "note": NOT_SAVED}

    @staticmethod
    def _cell(req: dict, prefix: str = "") -> tuple[int, int]:
        row, col = req.get(f"{prefix}row"), req.get(f"{prefix}col")
        if not isinstance(row, int) or not isinstance(col, int) or \
                not 0 <= row < qdevice.ROWS or not 0 <= col < qdevice.COLUMNS:
            raise EditorError(f"{prefix}row / {prefix}col を正しく指定してください。")
        return row, col

    @staticmethod
    def _free_cell(grid: dict, row: int, avoid=()) -> tuple[int, int] | None:
        used = {(b["row"], b["col"]) for b in grid["blocks"]} | set(avoid)
        order = [row] + [r for r in range(qdevice.ROWS) if r != row]
        return next(((r, c) for r in order for c in range(qdevice.COLUMNS)
                     if (r, c) not in used), None)

    def _relocate(self, device, before: dict, req: dict) -> dict:
        block = _locate(before, req)
        src = (block["row"], block["col"])
        dst = self._cell(req, "to_")
        if dst == src:
            return {"id": None, "warnings": [], "note": NOT_SAVED}
        other = _block(before, *dst)
        shown = req.get("to_model_id")
        if (other["model_id"] if other else None) != shown and \
                (shown is not None or other is not None) and "to_model_id" in req:
            raise EditorConflict(f"行{dst[0] + 1} 列{dst[1] + 1} が画面と違います。"
                                 "何も書き込まず、画面を読み直します。")
        if other is not None and "to_model_id" not in req:
            raise EditorConflict(f"行{dst[0] + 1} 列{dst[1] + 1} は空いていません。"
                                 "何も書き込まず、画面を読み直します。")
        stages = None
        if other is None:
            ops = [{"op": "move_block", "row": src[0], "col": src[1],
                    "to_row": dst[0], "to_col": dst[1]}]
            if isinstance(block.get("bypassed"), bool):
                # where a cell's bypass goes on a move is not documented: say it again
                ops.append({"op": "bypass", "row": dst[0], "col": dst[1],
                            "bypassed": block["bypassed"]})
            label = f"エディター: {block['model']} を 行{src[0] + 1} 列{src[1] + 1} → " \
                    f"行{dst[0] + 1} 列{dst[1] + 1} へ移動"
            expect = {src: None, dst: block["model_id"]}
        else:
            # Swap, like dropping a block onto another in Cortex Control: park the
            # target in a free cell (same row first), check it got there, then move
            # the dragged block in and bring the parked one back.
            if other["model_id"] == block["model_id"]:
                raise EditorError("同じモデルのブロック同士は入れ替えられません(取り消しで"
                                  "見分けがつかないため)。片方を空きマスへ動かしてください。")
            park = self._free_cell(before, dst[0], avoid=(src, dst))
            if park is None:
                raise EditorError("グリッドに空きマスが無いので入れ替えられません。")
            first = [{"op": "move_block", "row": dst[0], "col": dst[1], "to_row": park[0],
                      "to_col": park[1]}]
            rest = [{"op": "move_block", "row": src[0], "col": src[1], "to_row": dst[0],
                     "to_col": dst[1]},
                    {"op": "move_block", "row": park[0], "col": park[1], "to_row": src[0],
                     "to_col": src[1]}]
            for b, cell in ((block, dst), (other, src)):
                if isinstance(b.get("bypassed"), bool):
                    rest.append({"op": "bypass", "row": cell[0], "col": cell[1],
                                 "bypassed": b["bypassed"]})

            def parked(grid, park=park, dst=dst, other=other):
                here, there = _block(grid, *park), _block(grid, *dst)
                if here is None or here["model_id"] != other["model_id"] or there is not None:
                    raise EditorError("入れ替えの 1 手目(空きマスへの退避)を本体が受け付けません"
                                      "でした。それ以上は送っていません。画面を読み直します。")
            stages = [(first, parked), (rest, None)]
            ops = first + rest
            label = f"エディター: {block['model']} と {other['model']} を入れ替え" \
                    f"(行{src[0] + 1} 列{src[1] + 1} ⇄ 行{dst[0] + 1} 列{dst[1] + 1})"
            expect = {src: other["model_id"], dst: block["model_id"]}
        notes = []
        if dst[0] != src[0] or (other is not None and park[0] != dst[0]):
            notes.append("行をまたぐ移動では、本体が並列の経路(分岐)を自動で作ることがあります。"
                         "グリッドの分岐の表示を確かめてください。")
        return self._layout_commit(device, before, ops, label, expect, notes, stages)

    def _delete(self, device, before: dict, req: dict) -> dict:
        block = _locate(before, req)
        op = {"op": "remove_block", "row": block["row"], "col": block["col"]}
        notes = []
        if _lossy(block):
            notes.append("取り消しても、キャプチャー・IR などの選択(文字列)は戻りません。")
        label = f"エディター: {_where(block)} {block['model']} を削除"
        return self._layout_commit(device, before, [op], label,
                                   {(block["row"], block["col"]): None}, notes)

    def _paste_ops(self, device, source: dict, row: int, col: int, target) -> tuple[list, list]:
        """Ops that make the cell a copy of ``source`` (a block from GET /block)."""
        model_id = source.get("model_id")
        model = device.catalog.get(model_id) if isinstance(model_id, int) else None
        if model is None:
            raise EditorError("コピーしたブロックのモデルが、この本体にありません。")
        if model_id in inert.CAPTURE_MODELS or str(model.category).startswith("Neural Capture"):
            raise EditorError("キャプチャーのブロックはコピーできません(パソコンから選んだキャプチャーは"
                              "鳴らないため)。本体で複製してください。")
        entry = _listing_entry(device, model_id)
        if entry is not None and not entry["usable"]:
            raise EditorError(f"{entry['name']}: {entry['why_not']}")
        ops, notes = [], []
        if target is None or target["model_id"] != model_id:
            ops.append({"op": "set_block", "row": row, "col": col, "model": model_id})
        names = {spec.name for spec in model.parameters}
        switches = {spec.name for spec in model.parameters
                    if spec.option_count or spec.type in ("switch", "comboBox", "rotarySwitch")}
        skipped = []
        for prm in source.get("params") or []:
            name = prm.get("name")
            if name not in names:
                continue
            if "text" in prm:
                skipped.append(name)
            elif "options" in prm and prm.get("option") in (prm.get("options") or []):
                ops.append({"op": "set_option", "row": row, "col": col, "param": name,
                            "option": prm["option"]})
            elif "option" in prm or name in switches:
                skipped.append(name)          # a switch without option names: never by value
            elif isinstance(prm.get("value"), (int, float)) and 0 <= prm["value"] <= 1:
                ops.append({"op": "set_param", "row": row, "col": col, "param": name,
                            "value": float(prm["value"])})
        if isinstance(source.get("bypassed"), bool):
            ops.append({"op": "bypass", "row": row, "col": col, "bypassed": source["bypassed"]})
        if skipped:
            notes.append("コピーしなかった項目(文字列か、選択肢の名前が分からないスイッチ): "
                         + "、".join(skipped))
        return ops, notes

    def _paste(self, device, before: dict, req: dict) -> dict:
        row, col = self._cell(req)
        target = _block(before, row, col)
        if (target["model_id"] if target else None) != req.get("model_id"):
            raise EditorConflict(f"行{row + 1} 列{col + 1} が画面と違います。"
                                 "何も書き込まず、画面を読み直します。")
        source = req.get("source")
        if not isinstance(source, dict):
            raise EditorError("貼り付けるブロック(source)がありません。")
        ops, notes = self._paste_ops(device, source, row, col, target)
        name = source.get("model") or str(source.get("model_id"))
        label = f"エディター: 行{row + 1} 列{col + 1} に {name} を貼り付け"
        return self._layout_commit(device, before, ops, label,
                                   {(row, col): source["model_id"]}, notes)

    def _duplicate(self, device, before: dict, req: dict) -> dict:
        block = _locate(before, req)
        dst = self._cell(req, "to_")
        if _block(before, *dst) is not None:
            raise EditorConflict(f"行{dst[0] + 1} 列{dst[1] + 1} は空いていません。"
                                 "何も書き込まず、画面を読み直します。")
        ops, notes = self._paste_ops(device, block, dst[0], dst[1], None)
        label = f"エディター: {_where(block)} {block['model']} を 行{dst[0] + 1} 列{dst[1] + 1} に複製"
        return self._layout_commit(device, before, ops, label, {dst: block["model_id"]}, notes)

    def _reset(self, device, before: dict, req: dict) -> dict:
        """Every knob back to the model's defaults: the unit places the model afresh."""
        block = _locate(before, req)
        if _lossy(block):
            raise EditorError("キャプチャーや IR のブロックはリセットできません(選択が消えるため)。")
        row, col = block["row"], block["col"]
        ops = [{"op": "set_block", "row": row, "col": col, "model": block["model_id"]}]
        if isinstance(block.get("bypassed"), bool):
            ops.append({"op": "bypass", "row": row, "col": col, "bypassed": block["bypassed"]})
        label = f"エディター: {_where(block)} {block['model']} を初期値にリセット"
        res = self._layout_commit(device, before, ops, label, {(row, col): block["model_id"]})
        now = _block(_read(device), row, col)
        if now is not None and [p.get("value") for p in now["params"]] == \
                [p.get("value") for p in block["params"]]:
            res["warnings"].append("本体のつまみの値が変わりませんでした(もともと初期値だったか、"
                                   "本体が置き直しを無視した可能性があります)。")
        return res

    def _port(self, device, before: dict, req: dict) -> dict:
        row = req.get("row")
        which = req.get("which")
        if not isinstance(row, int) or not 0 <= row < qdevice.ROWS or which not in ("in", "out"):
            raise EditorError("row と which(in / out)を指定してください。")
        allowed = PORTS_IN if which == "in" else PORTS_OUT
        port = req.get("port")
        if port not in allowed:
            raise EditorError(f"ポートは次のどれかです: {', '.join(allowed)}")
        line = next(r for r in before["rows"] if r["row"] == row)
        key = "in_port" if which == "in" else "out_port"
        if req.get("expect") is not None and req["expect"] != line[key] and \
                not (req["expect"] == "PREV_ROW" and line[key] == "EMPTY"):
            raise EditorConflict(f"行{row + 1} の{'入力' if which == 'in' else '出力'}が画面と"
                                 "違います。何も書き込まず、画面を読み直します。")
        if line[key] == port:
            return {"id": None, "warnings": [], "note": NOT_SAVED}
        op = {"op": "set_input" if which == "in" else "set_output", "row": row, "port": port}
        label = f"エディター: 行{row + 1} の{'入力' if which == 'in' else '出力'} " \
                f"{port_label(line[key])} → {port_label(port)}"
        return self._layout_commit(device, before, [op], label)

    def _split(self, device, before: dict, req: dict) -> dict:
        row = req.get("row")
        if row not in (0, 2):
            raise EditorError("分岐を作れるのは行 1 と行 3 だけです(本体の仕様)。")
        line = next(r for r in before["rows"] if r["row"] == row)
        if "expect" in req and req["expect"] != line.get("split"):
            raise EditorConflict(f"行{row + 1} の分岐が画面と違います。何も書き込まず、画面を読み直します。")
        if not req.get("clear") and [req.get("split"), req.get("mix")] == line.get("split"):
            return {"id": None, "warnings": [], "note": NOT_SAVED}
        if req.get("clear"):
            if line.get("split") is None:
                return {"id": None, "warnings": [], "note": NOT_SAVED}
            op = {"op": "clear_split", "row": row}
            label = f"エディター: 行{row + 1} の分岐を外す"
        else:
            sc, mc = req.get("split"), req.get("mix")
            op = {"op": "set_split", "row": row, "split": sc, "mix": mc}
            where = f"列{sc + 1} で分岐" if isinstance(sc, int) else "?"
            joined = "合流しない" if mc == -1 else f"列{mc + 1} で合流" if isinstance(mc, int) else "?"
            label = f"エディター: 行{row + 1} を {where}・{joined}(並列の経路は行{row + 2})"
        return self._layout_commit(device, before, [op], label)

    # -- presets (loading one changes the sound and drops unsaved edits) ----------------

    def recall(self, device, req: dict) -> dict:
        """Load a stored preset: ``{folder, position}`` or ``{step: +1 | -1}``.

        Like Cortex Control, asks first only when the live preset has unsaved
        edits: without ``confirm: true`` that case answers ``needs_confirm``
        and loads nothing. Undo history stops at the switch.
        """
        with self._edit:
            self._wait_idle(device)
            step = req.get("step")
            if step in (1, -1):
                here = device.current_slot()
                if not here:
                    raise EditorError("本体で読み込まれているスロットが分かりません。プリセット名を"
                                      "クリックして一覧から選んでください。")
                key = here["folder"]
                listing = device.presets(key)
                taken = sorted(p["index"] for p in listing if p["index"] >= 0)
                if not taken:
                    raise EditorError("このセットリストにプリセットがありません。")
                later = [i for i in taken if (i - here["position"]) * step > 0]
                position = (min(later) if step > 0 else max(later)) if later else \
                    (taken[0] if step > 0 else taken[-1])          # wrap like the unit
            else:
                key, position = req.get("folder"), req.get("position")
                if not isinstance(key, str) or not key or not isinstance(position, int) \
                        or not 0 <= position < 256:
                    raise EditorError("folder と position(0〜255)を指定してください。")
                listing = device.presets(key)
            name = next((p["name"] for p in listing if p["index"] == position), None)
            if name is None:
                raise EditorError(f"{qslot(position)} は空きスロットです。")
            dirty = device.preset_dirty()
            if dirty and not req.get("confirm"):
                return {"needs_confirm": True, "folder": key, "position": position,
                        "slot": qslot(position), "name": name,
                        "message": "本体に保存していない編集があります。呼び出すと、その編集は消えます"
                                   "(音も切り替わります)。"}
            device.recall(key, position)
            panel.mark_preset_change(device.mock, f"{key} {qslot(position)} {name}")
            return {"loaded": True, "folder": key, "position": position,
                    "slot": qslot(position), "name": name,
                    "note": "プリセットを呼び出しました(音が切り替わります)。取り消しの履歴はここで区切られます。"}

    # -- scenes (not grid edits: no history) --------------------------------------------

    def scene(self, device, req: dict) -> dict:
        with self._edit:
            self._wait_idle(device)
            index = req.get("scene")
            if not isinstance(index, int) or not 0 <= index < 8:
                raise EditorError("scene は 0〜7 で指定してください。")
            if "label" in req:
                label = str(req.get("label") or "").strip()[:30]
                device.set_scene_label(index, label)
                return {"scene": index, "label": label, "note": NOT_SAVED}
            device.switch_scene(index)
            return {"scene": index, "active": "ABCDEFGH"[device.active_scene()],
                    "note": NOT_SAVED}

    # -- gestures ----------------------------------------------------------------

    def begin(self, device, req: dict) -> dict:
        with self._edit:
            return self._begin(device, req)

    def _begin(self, device, req: dict) -> dict:
        self._wait_idle(device)
        before = _read(device)
        block = _locate(before, req)
        name = req.get("param") or ""
        param = _param(block, name)
        if param is None or "text" in param or "options" in param or "option" in param:
            raise EditorError(f"{name} はドラッグで動かせるつまみではありません。")
        _refuse_switch_by_value({"op": "set_param", "row": block["row"], "col": block["col"],
                                 "param": param["name"], "value": 0.0}, before, device)
        _check_expect(block, param, req.get("expect"), name)
        device.policy.check_grid("グリッドの編集")
        g = SimpleNamespace(id=uuid.uuid4().hex[:10], row=block["row"], col=block["col"],
                            model_id=block["model_id"], param=param["name"],
                            real=param.get("real") is not None, before=before, block=block,
                            newest=-1, ended=False, sent_any=False, moves=0,
                            skew_param=param if param.get("skew", 1.0) != 1.0 else None,
                            touched=time.monotonic(),
                            mutex=threading.Lock(), send_lock=threading.Lock())
        with self._lock:
            self._gestures[g.id] = g
        return {"gesture": g.id}

    def _get(self, gesture_id: str):
        with self._lock:
            g = self._gestures.get(gesture_id)
        if g is None:
            raise EditorError("この操作はもう終わっています(画面を読み直してください)。")
        return g

    def _amount(self, g, req) -> dict:
        if g.real:
            if req.get("real") is None:
                raise EditorError("real を指定してください。")
            return {"real": float(req["real"])}
        if req.get("value") is None:
            raise EditorError("value を指定してください。")
        return {"value": float(req["value"])}

    def _still_there(self, device, g) -> bool:
        import pyquadcortex as pq
        preset = device.current_preset()
        return any(b.row == g.row and b.column == g.col and b.model_id == g.model_id
                   for b in pq.blocks(preset))

    def move(self, device, gesture_id: str, req: dict) -> dict:
        g = self._get(gesture_id)
        seq = int(req.get("seq", 0))
        amount = self._amount(g, req)
        with g.mutex:
            if g.ended or seq <= g.newest:
                return {"sent": False}
            g.newest = seq
        with g.send_lock:
            with g.mutex:
                if g.ended or seq < g.newest:
                    return {"sent": False}       # a newer value arrived while waiting
            g.touched = time.monotonic()
            if g.moves and g.moves % self.CHECK_EVERY == 0 and not self._still_there(device, g):
                with g.mutex:
                    g.ended = True
                self._drop(g)
                raise EditorConflict(f"行{g.row + 1} 列{g.col + 1} のブロックが本体側で変わりました。"
                                     "ドラッグを止めました。画面を読み直します。")
            if g.skew_param is not None and "real" in amount:
                amount = {"value": _to_wire(g.skew_param, amount["real"])}
            device.set_knob(g.row, g.col, g.model_id, g.param, **amount)
            g.moves += 1
            g.sent_any = True
            g.touched = time.monotonic()
        return {"sent": True}

    def end(self, device, gesture_id: str, req: dict) -> dict:
        g = self._get(gesture_id)
        amount = self._amount(g, req)
        with g.mutex:
            g.ended = True
            g.newest = math.inf
        with g.send_lock:                         # waits for a move in flight
            try:
                # The block may have been swapped on the unit's screen mid-drag.
                _locate(_read(device), {"row": g.row, "col": g.col, "model_id": g.model_id})
                op = _knob_op(g.block, _param(g.block, g.param), g.param, amount)
                return _commit(device, g.before, g.block, op)
            finally:
                self._drop(g)

    def finish_stale(self, device) -> list[str]:
        """Record gestures whose page vanished mid-drag, so undo can still revert them.

        A gesture with a write still in flight is not stale (its lock is held).
        """
        now = time.monotonic()
        stale = []
        with self._lock:
            for g in list(self._gestures.values()):
                if g.ended or now - g.touched <= self.stale_seconds:
                    continue
                if not g.send_lock.acquire(blocking=False):
                    continue                     # a move is being sent right now
                stale.append(g)
                self._gestures.pop(g.id, None)
            self._idle.notify_all()
        ids = []
        for g in stale:
            try:
                with g.mutex:
                    g.ended = True
                if not g.sent_any:
                    continue
                after = _read(device)
                after_block = _block(after, g.row, g.col)
                op = {"op": "set_param", "row": g.row, "col": g.col, "param": g.param}
                current = _param(after_block, g.param) if after_block else None
                if current is not None:
                    op["real" if g.real else "value"] = _current(current)
                label = _label(g.block, after_block, op) + "(ドラッグが途中で終わりました)"
                ids.append(panel.record(device.mock, {"note": "editor", "ops": [op]}, g.before,
                                        after, instruction=label, engine=ENGINE, summary=[label]))
            finally:
                g.send_lock.release()
        return ids

    def pending(self) -> int:
        with self._lock:
            return len(self._gestures)


# -- the device list (left sidebar) ------------------------------------------------------

#: Sidebar order and labels; each maps to catalog category names.
SIDEBAR = [
    ("plugins", "PLUGINS", ()),
    ("amp", "AMP", ("Guitar Amplifier", "Bass Amplifier")),
    ("capture", "NEURAL CAPTURE", ("Neural Capture", "Neural Capture V2")),
    ("cab", "CAB", ("Cabsim Guitar (M)", "Cabsim Guitar (ST)", "Cabsim Bass (M)",
                    "Cabsim Bass (ST)")),
    ("overdrive", "OVERDRIVE", ("Guitar Overdrive", "Bass Overdrive")),
    ("delay", "DELAY", ("Delay",)),
    ("reverb", "REVERB", ("Reverb",)),
    ("compressor", "COMPRESSOR", ("Compressor",)),
    ("pitch", "PITCH", ("Pitch",)),
    ("modulation", "MODULATION", ("Modulation",)),
    ("filter", "FILTER", ("Filter",)),
    ("wah", "WAH", ("Wah",)),
    ("eq", "EQ", ("Equalizer",)),
    ("morph", "MORPH", ("Morph",)),
    ("synth", "SYNTH", ("Synth",)),
    ("ir", "IR LOADER", ("IRLoaders",)),
    ("utility", "UTILITY", ("Utility",)),
    ("fxloop", "FX LOOP", ("FX Loop",)),
    ("looper", "LOOPER", ("Loopers",)),
]
_SIDEBAR_COLOR = {"plugins": "cmp"}


def qslot(position: int) -> str:
    import pyquadcortex as pq
    return pq.position_to_slot(position) if 0 <= position < 256 else str(position)


def preset_list(device, folder: str | None = None) -> dict:
    """Setlists (cached ~60 s: a fresh listing takes ~20 s) and one setlist's presets."""
    setlists = [{"key": f["key"], "name": f["name"], "kind": f["kind"]}
                for f in device.setlists_cached()]
    here = device.current_slot()
    key = folder or (here["folder"] if here else None) or \
        next((f["key"] for f in setlists if f["kind"] == "user"), None)
    presets = []
    if key:
        presets = [{"position": p["index"], "slot": p["slot"], "name": p["name"]}
                   for p in device.presets(key) if p["index"] >= 0]
    return {"setlists": setlists, "folder": key, "presets": presets, "current": here}


def changes(device) -> dict:
    """What the page needs to follow the unit: a push counter (no unit I/O)."""
    return device.changes()


def block_copy(device, row: int, col: int) -> dict:
    """One block as stored (model, wire values, option names, bypass): the clipboard."""
    grid = device.grid()
    block = _block(grid, row, col)
    if block is None:
        raise EditorError(f"行{row + 1} 列{col + 1} にブロックがありません。")
    return {"model_id": block["model_id"], "model": block["model"],
            "bypassed": block.get("bypassed"), "lossy": _lossy(block),
            "params": [{k: p[k] for k in ("name", "value", "option", "options", "text") if k in p}
                       for p in block["params"]]}


def catalog_listing(device, repo="load") -> dict:
    """Every placeable model, grouped the way the sidebar shows them. Names only.

    Hidden, internal and superseded models are left out. Unowned plugins and
    capture models are listed but greyed (``usable: false``): an unowned plugin
    passes audio through, and a capture block placed from here has no capture
    to play (captures are chosen on the unit).
    """
    if repo == "load":
        repo = load_repo(device)
    catalog = device.catalog
    by_cat = {}
    for m in catalog:
        rm = repo.get(m.id) if repo is not None else None
        if m.hidden or m.internal or m.category_hidden or m.superseded or \
                (rm is not None and (rm.hidden or rm.internal or rm.category_hidden)):
            continue
        plugin = getattr(m, "plugin_id", None) or getattr(m, "sku", None)
        unowned = inert.inert_plugin(m)
        why = None
        if unowned:
            why = f"未所有のプラグイン({unowned})です。置いても音は素通しです。"
        elif m.id in inert.CAPTURE_MODELS or m.category.startswith("Neural Capture"):
            why = "キャプチャーは本体で選んでください(ここから置いても鳴りません)。"
        entry = {"id": m.id, "name": m.name, "category": m.category,
                 "color": modelrepo.color_key(m.category),
                 "bass": m.category.startswith(("Bass", "Cabsim Bass")),
                 "based_on": (rm.based_on if rm else "") or m.based_on or "",
                 "plugin": bool(plugin), "usable": why is None, "why_not": why}
        key = "plugins" if plugin else next(
            (k for k, _, cats in SIDEBAR if m.category in cats), "utility")
        by_cat.setdefault(key, []).append(entry)
    groups = []
    for key, label, cats in SIDEBAR:
        items = sorted(by_cat.get(key, []), key=lambda e: e["name"].lower())
        if not items:
            continue
        color = _SIDEBAR_COLOR.get(key) or modelrepo.color_key(cats[0] if cats else None)
        groups.append({"key": key, "label": label, "color": color,
                       "has_bass": any(e["bass"] for e in items), "models": items})
    return {"groups": groups}


def _listing_entry(device, model_id: int):
    for g in catalog_listing(device)["groups"]:
        for e in g["models"]:
            if e["id"] == model_id:
                return e
    return None


# -- CLI (read-only: the editor itself is the web page) --------------------------------

def view_text(v: dict) -> str:
    lines = [f"シーン {v['scene']}  ({v['repo']['note']})"]
    for r in v["rows"]:
        blocks = [c for c in r["cells"] if c]
        if not blocks and not r["is_input"]:
            continue
        lines.append(f"行{r['row'] + 1}: [{r['in_port']}] → [{r['out_port']}]")
        for b in blocks:
            mark = " (バイパス)" if b["bypassed"] else ""
            quiet = f" [{INERT_NOTE}]" if not b["responds"] else ""
            lines.append(f"  列{b['col'] + 1} {b['category_label']} {b['title']}{mark}{quiet}")
            parts = []
            for p in b["params"]:
                if p["kind"] == "knob":
                    parts.append(f"{p['name']}={value_text(p, p['value'])}")
                elif p["kind"] == "select":
                    sel = p["selected"]
                    parts.append(f"{p['name']}={p['labels'][sel] if sel is not None else '?'}")
                else:
                    parts.append(f"{p['name']}={p['text']}")
            if parts:
                lines.append("      " + "  ".join(parts))
    lines.append(v["note"])
    return "\n".join(lines)


def cmd_show(ctx, a):
    with ctx.rig("editor show") as rig:
        v = view(rig.device)
    ctx.out(v, view_text(v))


def add_cli(sub):
    p = sub.add_parser("editor", help="エディター(グリッドとパラメーター画面。操作は qcsuite serve の画面で)")
    s = p.add_subparsers(dest="editor_cmd", required=True)
    c = s.add_parser("show", help="エディターが表示する内容を文字で出す(副作用なし)")
    c.set_defaults(fn=cmd_show)

