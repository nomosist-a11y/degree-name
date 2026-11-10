"""エディターの追加の設定: 行の出力・入力ゲート・分岐・シーン・テンポ・フットスイッチ・ペダル.

The settings Cortex Control edits that are not blocks: each row's output
(VOLUME / PAN / MUTE / SOLO) and input gate, the splitter and mixer of a split
row (rows 1 and 3 on screen) and the split's MUTE, scene colours and scene
copy / swap, a block parameter's scene mode, the preset's tempo, the STOMP
footswitches (assignment, label, momentary) and expression pedals.
:func:`decorate` adds what the unit holds for all of them to the editor's view
(:func:`qcsuite.features.editor.view`); :func:`write` carries out one change.

Rules every write follows (nothing here saves anything):

* **The unit is the source of truth.** A write reads the live preset first and
  compares it with what the page showed (``expect``; for block-keyed settings
  ``model_id`` too). If they differ, nothing is written and
  :class:`~qcsuite.features.editor.EditorConflict` (HTTP 409) tells the page to
  reload. Every write then reads back, reports what the unit actually holds and
  warns when that is not what was sent (the unit answers most writes with
  silence).
* **One write, one undo step**, in the panel's history (:func:`panel.record`).
  These settings are not in the qctone grid snapshot, so the entry's
  ``before`` and ``after`` are the same snapshot (taken after the write, so the
  snapshot restore finds nothing to do) and the entry carries ``undo_extra`` /
  ``redo_extra``: bodies for :func:`apply_extra` with their own ``expect``. A
  write whose read-back shows no change records nothing. Exceptions:
  ``scene_copy`` without ``swap`` overwrites a scene and cannot be undone (no
  entry; the answer says so); ``swap`` is undone by swapping again.
  ``gig_view`` is refused (see below).
* **Switches move only by option name.** The splitter's TYPE / STEREO / MODE
  take ``option`` (a name from the preset's ``dynamic_steps``); without names
  from the unit they are shown but not writable. Row MUTE / SOLO and the gate's
  BYPASS take ``on`` (true / false), which pyquadcortex documents as 1.0 / 0.0
  (MUTE 1.0 = muted; gate BYPASS 1.0 = gate off).
* **Levels in dB** (row VOLUME, splitter LEVEL TO A/B, mixer LEVEL A/B and
  MIXER LEVEL) use :func:`pyquadcortex.lane_level_db` /
  :func:`~pyquadcortex.db_to_lane_level` (-40..+12 dB, 0 dB = 0.769), like
  :meth:`Device.lane_volume_db`. Wire 0.0 is the knob's "Off" position.
* **Tempo** converts BPM through the catalog's Tempo Control (model 25000)
  TEMPO spec and reads back through :func:`pyquadcortex.tempo_params`. When
  the catalog publishes TEMPO as the placeholder 0..1 "BPM" (pyquadcortex
  says it may) there is no conversion: the tempo is shown as unknown and the
  write is refused rather than guessed.

Scenes:

* The view shows **the active scene's value** everywhere. Block parameters come
  from the qctone snapshot (its first value slot, which pyquadcortex documents
  as the active scene's); the row settings read here take the active scene's
  slot when the parameter follows scenes, else the single value.
* A write without a scene lands on the active scene: only that scene for a
  parameter in scene mode, all scenes otherwise (pyquadcortex ``set_param``).
* ``scene_mode`` turns a block parameter's per-scene memory on or off (the
  unit's long press). Whether turning it off keeps the active scene's value is
  not documented; the answer shows what reads back.
* An undo is refused while another scene is active than the one the change was
  made in (the panel compares ``active_scene``), so a per-scene value is never
  restored into the wrong scene.
* The manual says a parameter assigned to an expression pedal is excluded from
  scene data and does not change with scenes: assigning one says so.

Footswitches: a momentary write lands only on a footswitch driving exactly one
block, and the unit ignores it silently otherwise (pyquadcortex
``set_stomp_momentary``) - the write is sent, read back, and a warning says when
it did not land. A label goes to ``single_stomp_labels`` for a switch driving
one block and to ``stomp_labels`` otherwise (pyquadcortex ``set_stomp_label``).

Gig View: ``set_gig_view`` is on the safety rules' list of device methods
qcsuite never calls (:data:`qcsuite.device.guard.FORBIDDEN_METHODS`), so
``gig_view`` is refused with that reason and nothing is sent.

Verified on the mock only (:mod:`qcsuite.device.fake`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pyquadcortex as pq

from qcsuite.errors import QcsuiteError
from qcsuite.features import editor, panel
from qctone import device as qdevice

EditorError = editor.EditorError
EditorConflict = editor.EditorConflict
NOT_SAVED = editor.NOT_SAVED
ENGINE = editor.ENGINE
SCENES = 8
FOOTSWITCHES = "ABCDEFGH"
LANE_MODEL = 23000
GATE_MODEL = 28000
SPLITTER = 10004
MIXER = 11000
TEMPO_MODEL = 25000
TEMPO_RANGE = (30.0, 250.0)
LANE_PARAMS = ("VOLUME", "PAN", "MUTE", "SOLO")
GATE_PARAMS = ("NOISE REDUCTION", "INPUT GAIN", "BYPASS")
SWITCH_TYPES = ("switch", "comboBox", "rotarySwitch")
LABEL_LIMIT = 30
EXPRESSION_WARNING = ("マニュアルによると、エクスプレッションペダルに割り当てたパラメーターはシーンの"
                      "データから外れ、シーンを切り替えても変わりません。")
COPY_NOTE = "シーンを上書きしました。この操作は取り消せません。" + NOT_SAVED
WIRE_TOL = 2e-3


# -- reading the preset ---------------------------------------------------------------

def _scene_index(scene) -> int:
    if isinstance(scene, str):
        return FOOTSWITCHES.index(scene) if scene in FOOTSWITCHES else 0
    try:
        return int(scene)
    except (TypeError, ValueError):
        return 0


def _value(prm, scene: int):
    """A param's float for the active scene (its own slot when it follows scenes)."""
    values = list(prm.param_values)
    if not values:
        return None
    pv = values[scene] if prm.scene_mode and scene < len(values) else values[0]
    if not pq.field_present(pv, "float_value"):
        return None
    v = pv.float_value
    return None if math.isnan(v) else v


def _model(catalog, model_id):
    try:
        return catalog.get(model_id) if catalog is not None else None
    except Exception:                       # noqa: BLE001 - a catalog without .get
        return None


def _spec(catalog, model_id, name):
    model = _model(catalog, model_id)
    if model is None:
        return None
    try:
        return model.parameter(name)
    except KeyError:
        return None


def _index(catalog, model_id, name, fallback):
    spec = _spec(catalog, model_id, name)
    return spec.index if spec is not None else fallback


def _is_switch(spec) -> bool:
    return spec is not None and bool(spec.option_count or spec.type in SWITCH_TYPES)


def _db_text(wire) -> str:
    if wire is None:
        return "—"
    if wire <= 0.0:
        return "Off"
    return f"{pq.lane_level_db(wire):+.1f} dB"


def _pan_text(wire) -> str:
    if wire is None:
        return "—"
    amount = round((wire - 0.5) * 200)
    return "C" if amount == 0 else (f"L{-amount}" if amount < 0 else f"R{amount}")


def lane_state(preset, row: int, scene: int, catalog=None) -> dict:
    """Row output: VOLUME in dB and wire, PAN wire (0.5 centre), MUTE, SOLO."""
    out = {"volume_db": None, "volume": None, "pan": None, "mute": False, "solo": False,
           "volume_off": False}
    try:
        params = preset.chains[row].output_control[0].params
    except IndexError:
        return out
    get = {}
    for k, name in enumerate(LANE_PARAMS):
        i = _index(catalog, LANE_MODEL, name, k)
        get[name] = _value(params[i], scene) if i < len(params) else None
    vol = get["VOLUME"]
    out["volume"] = None if vol is None else round(vol, 6)
    out["volume_off"] = vol is not None and vol <= 0.0
    out["volume_db"] = None if vol is None or vol <= 0.0 else round(pq.lane_level_db(vol), 1)
    out["pan"] = None if get["PAN"] is None else round(get["PAN"], 6)
    out["mute"] = bool(get["MUTE"] is not None and get["MUTE"] >= 0.5)
    out["solo"] = bool(get["SOLO"] is not None and get["SOLO"] >= 0.5)
    return out


def gate_state(preset, row: int, scene: int, catalog=None) -> dict:
    """Row input gate: NOISE REDUCTION in percent, INPUT GAIN in dB, BYPASS."""
    out = {"noise_reduction": None, "input_gain_db": None, "bypassed": None}
    try:
        params = preset.chains[row].input_control[0].params
    except IndexError:
        return out
    for key, name, fallback in (("noise_reduction", "NOISE REDUCTION", 0),
                                ("input_gain_db", "INPUT GAIN", 3)):
        spec = _spec(catalog, GATE_MODEL, name)
        i = spec.index if spec is not None else fallback
        v = _value(params[i], scene) if i < len(params) else None
        if v is not None and spec is not None and not spec.range_is_placeholder:
            out[key] = round(spec.to_real(v), 2)
    i = _index(catalog, GATE_MODEL, "BYPASS", 1)
    v = _value(params[i], scene) if i < len(params) else None
    out["bypassed"] = None if v is None else v >= 0.5
    return out


def split_of(preset, row: int):
    for s in pq.splits(preset):
        if s.row == row:
            return s
    return None


def _row_model(preset, row: int, which: str):
    """(element, model id) for a row's splitter or mixer, or (None, None).

    The splitter is written into ``combined_splitter`` and reported in the
    read-only ``splitter[]`` (pyquadcortex); whichever carries parameters is read.
    A legacy splitter id (10000 / 10002 / 10003) is read by its own catalog
    model, and written through the unified model by parameter name.
    """
    try:
        chain = preset.chains[row]
    except IndexError:
        return None, None
    if which == "splitter":
        for coll in (chain.combined_splitter, chain.splitter):
            if len(coll) and len(coll[0].params):
                return coll[0], (coll[0].hash or SPLITTER)
        return None, None
    if len(chain.mixer) and len(chain.mixer[0].params):
        return chain.mixer[0], (chain.mixer[0].hash or MIXER)
    return None, None


def _sub_param(prm, spec, unified_spec, scene: int) -> dict:
    v = _value(prm, scene)
    out = {"name": spec.name, "value": None if v is None else round(v, 6), "real": None,
           "units": (spec.units or "").strip(), "text": "—", "kind": "knob", "options": None,
           "writable": False}
    if _is_switch(spec) or _is_switch(unified_spec):
        names = list(prm.dynamic_steps)
        out["kind"] = "select"
        out["options"] = names or None
        if v is not None and names:
            out["text"] = pq.option_at(names, v)
        out["writable"] = bool(names) and unified_spec is not None
        return out
    if v is not None:
        name = spec.name.upper()
        if spec.range_is_placeholder and "LEVEL" in name:
            out["real"] = None if v <= 0.0 else round(pq.lane_level_db(v), 1)
            out["units"] = "dB"
            out["text"] = _db_text(v)
        elif not spec.range_is_placeholder:
            real = spec.to_real(v)
            out["real"] = round(real, 4)
            out["text"] = editor.value_text({"min": spec.minimum, "max": spec.maximum,
                                             "units": spec.units}, real)
        elif name.startswith("PAN"):
            out["text"] = _pan_text(v)
        else:
            out["text"] = f"{v:.2f}"
    out["writable"] = unified_spec is not None and v is not None
    return out


def sub_state(preset, row: int, which: str, scene: int, catalog) -> dict | None:
    """``{"params": [...]}`` for a split row's splitter or mixer, or None."""
    if row % 2 or split_of(preset, row) is None:
        return None
    element, model_id = _row_model(preset, row, which)
    if element is None:
        return None
    model = _model(catalog, model_id)
    unified = _model(catalog, SPLITTER if which == "splitter" else MIXER)
    params = []
    for i, prm in enumerate(element.params):
        if model is None or i >= len(model.parameters):
            continue
        spec = model.parameters[i]
        if spec.type in ("empty", "grMeter", "text") or spec.name.upper().startswith("RESERVED"):
            continue
        uspec = None
        if unified is not None:
            try:
                uspec = unified.parameter(spec.name)
            except KeyError:
                uspec = None
        params.append(_sub_param(prm, spec, uspec, scene))
    return {"model_id": model_id, "params": params}


def split_muted(preset, row: int):
    if row % 2 or split_of(preset, row) is None:
        return None
    try:
        chain = preset.chains[row]
    except IndexError:
        return None
    for coll in (chain.mixBypass, chain.splitBypass):    # the unit reports in mixBypass
        if len(coll):
            return bool(coll[0].bypass)
    return None


def _tempo_spec(catalog):
    return _spec(catalog, TEMPO_MODEL, "TEMPO")


def tempo_bpm(preset, catalog):
    """(BPM or None, writable). Index 0 of tempo_params through the catalog's TEMPO."""
    spec = _tempo_spec(catalog)
    if spec is None or spec.range_is_placeholder:
        return None, False
    wire = pq.tempo_params(preset).get(pq.QuadCortex.TEMPO_PARAMS["TEMPO"])
    if wire is None:
        return None, True
    return round(spec.to_real(wire), 1), True


def _hex(argb: int):
    if not argb:
        return None
    return f"#{int(argb) & 0xFFFFFF:06x}"


def scene_colors(preset) -> list:
    colors = list(getattr(preset, "scene_colors", []) or [])
    return [_hex(colors[i]) if i < len(colors) else None for i in range(SCENES)]


def _argb(preset, scene: int) -> int:
    colors = list(preset.scene_colors)
    return int(colors[scene]) if scene < len(colors) else 0


def stomp_map(preset) -> dict:
    """``{"A": {"label", "momentary", "blocks": [{"row", "col"}]}}`` for assigned switches."""
    out = {}
    for a in pq.stomp_assignments(preset):
        if not 0 <= a.footswitch < len(FOOTSWITCHES):
            continue
        fs = FOOTSWITCHES[a.footswitch]
        out.setdefault(fs, {"label": "", "momentary": False, "blocks": []})
        out[fs]["blocks"].append({"row": a.row, "col": a.column})
    for fs, entry in out.items():
        i = FOOTSWITCHES.index(fs)
        single = preset.single_stomp_labels.get(i, "") if i in preset.single_stomp_labels else ""
        multi = preset.stomp_labels.get(i, "") if i in preset.stomp_labels else ""
        entry["label"] = (single or multi) if len(entry["blocks"]) == 1 else (multi or single)
        entry["momentary"] = bool(preset.stomp_is_momentary.get(i, False)) \
            if i in preset.stomp_is_momentary else False
    return out


def _stomp_of(preset, row: int, col: int):
    for a in pq.stomp_assignments(preset):
        if (a.row, a.column) == (row, col) and 0 <= a.footswitch < len(FOOTSWITCHES):
            return FOOTSWITCHES[a.footswitch]
    return None


def param_extra(preset, row: int, col: int, index) -> dict:
    """scene_mode, expression pedal (0 none) and its sweep, for one block parameter."""
    out = {"scene_mode": False, "expression": 0, "exp_min": None, "exp_max": None}
    try:
        prm = preset.chains[row].models[col].params[int(index)]
    except (IndexError, TypeError, ValueError):
        return out
    out["scene_mode"] = bool(prm.scene_mode)
    out["expression"] = int(prm.expression)
    if out["expression"]:
        out["exp_min"] = round(prm.expression_min, 6)
        out["exp_max"] = round(prm.expression_max, 6)
    return out


def decorate(view: dict, preset, catalog) -> dict:
    """Add the extras to the editor's view, from the same read of the live preset."""
    scene = _scene_index(view.get("scene"))
    for r in view["rows"]:
        row = r["row"]
        r["lane"] = lane_state(preset, row, scene, catalog)
        r["gate"] = gate_state(preset, row, scene, catalog)
        r["splitter"] = sub_state(preset, row, "splitter", scene, catalog)
        r["mixer"] = sub_state(preset, row, "mixer", scene, catalog)
        r["split_muted"] = split_muted(preset, row)
        for cell in r["cells"]:
            if cell is None:
                continue
            cell["stomp"] = _stomp_of(preset, cell["row"], cell["col"])
            for p in cell["params"]:
                p.update(param_extra(preset, cell["row"], cell["col"], p.get("index")))
    bpm, writable = tempo_bpm(preset, catalog)
    if bpm is None and not writable:
        bpm = view.get("tempo")             # the preset's own tempo field, display only
    view["tempo"] = bpm
    view["tempo_writable"] = writable
    view["scene_colors"] = scene_colors(preset)
    view["stomp"] = stomp_map(preset)
    return view


# -- request checks --------------------------------------------------------------------

def _row(body: dict, even: bool = False) -> int:
    row = body.get("row")
    if not isinstance(row, int) or isinstance(row, bool) or not 0 <= row < qdevice.ROWS:
        raise EditorError("row は 0〜3 で指定してください。")
    if even and row % 2:
        raise EditorError("スプリッターとミキサーは行 1 と行 3 にだけあります。")
    return row


def _number(body: dict, key: str, lo=None, hi=None) -> float:
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(float(v)):
        raise EditorError(f"{key} は数値で指定してください。")
    v = float(v)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _bool(body: dict, key: str) -> bool:
    v = body.get(key)
    if not isinstance(v, bool):
        raise EditorError(f"{key} は true か false です。")
    return v


def _scene_arg(body: dict, key: str) -> int:
    v = body.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < SCENES:
        raise EditorError(f"{key} は 0〜7 で指定してください。")
    return v


def _footswitch(body: dict, allow_none: bool = False):
    v = body.get("footswitch")
    if v is None and allow_none:
        return None
    if not isinstance(v, str) or v.upper() not in FOOTSWITCHES or len(v) != 1:
        raise EditorError("footswitch は A〜H で指定してください。")
    return v.upper()


def _same(a, b, tol: float = WIRE_TOL) -> bool:
    if a is None or b is None or isinstance(a, (bool, str)) or isinstance(b, (bool, str)):
        return a == b
    return abs(float(a) - float(b)) <= tol


def _check(body: dict, now, what: str, required: bool = False, tol: float = WIRE_TOL):
    if "expect" not in body:
        if required:
            raise EditorError("expect(画面に出ていた値)を指定してください。")
        return
    expect = body["expect"]
    if isinstance(now, bool) and isinstance(expect, (int, float)) and not isinstance(expect, bool):
        expect = expect >= 0.5
    if not _same(now, expect, tol):
        raise EditorConflict(f"{what} が本体側で変わっていました(画面 {expect} / 本体 {now})。"
                             "何も書き込まず、画面を読み直します。")


def _block(device, body: dict) -> dict:
    """The block at row/col, checked against the model the page showed."""
    grid = device.grid()
    row, col = body.get("row"), body.get("col")
    if not isinstance(row, int) or not isinstance(col, int) or not 0 <= row < qdevice.ROWS \
            or not 0 <= col < qdevice.COLUMNS:
        raise EditorError("row と col を指定してください。")
    if not isinstance(body.get("model_id"), int):
        raise EditorError("model_id(画面に出ていたブロックのモデル)を指定してください。")
    return editor._locate(grid, body)


def _block_param(block: dict, name) -> dict:
    param = editor._param(block, name or "")
    if param is None:
        raise EditorError(f"{editor._where(block)} に {name} というパラメーターはありません。")
    if "text" in param:
        raise EditorError(f"{name}: {editor.TEXT_WHY}")
    return param


# -- writes ------------------------------------------------------------------------------

@dataclass
class _Done:
    label: str
    changed: bool
    undo: list = field(default_factory=list)
    redo: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    readback: dict = field(default_factory=dict)
    history: bool = True
    note: str = NOT_SAVED


def _live(device):
    return device.current_preset(), _scene_index(device.active_scene())


def _lane(device, body: dict) -> _Done:
    row = _row(body)
    name = str(body.get("param") or "").upper()
    if name not in LANE_PARAMS:
        raise EditorError(f"param は {', '.join(LANE_PARAMS)} のどれかです。")
    catalog = device.catalog
    preset, scene = _live(device)
    key = name.lower()
    before = lane_state(preset, row, scene, catalog)
    _check(body, before[key], f"行{row + 1} の出力 {name}", required=True)
    if name in ("MUTE", "SOLO"):
        on = _bool(body, "on")
        sent = 1.0 if on else 0.0
    elif name == "VOLUME" and "db" in body:
        try:
            sent = pq.db_to_lane_level(_number(body, "db"))
        except ValueError:
            raise EditorError("VOLUME は -40〜+12 dB です(無音にするなら value 0)。") from None
    else:
        sent = _number(body, "value", 0.0, 1.0)
    device.set_lane_param(row, name, sent)
    after = lane_state(device.current_preset(), row, scene, catalog)
    warnings = []
    if name in ("MUTE", "SOLO"):
        landed = after[key] == on
        old_text, new_text = ("オン" if before[key] else "オフ"), ("オン" if after[key] else "オフ")
        undo = {"action": "lane", "row": row, "param": name, "on": before[key],
                "expect": after[key]}
        redo = {"action": "lane", "row": row, "param": name, "on": after[key],
                "expect": before[key]}
    else:
        landed = after[key] is not None and abs(after[key] - sent) <= WIRE_TOL
        fmt = _db_text if name == "VOLUME" else _pan_text
        old_text, new_text = fmt(before[key]), fmt(after[key])
        undo = {"action": "lane", "row": row, "param": name, "value": before[key],
                "expect": after[key]}
        redo = {"action": "lane", "row": row, "param": name, "value": after[key],
                "expect": before[key]}
    if not landed:
        warnings.append(f"行{row + 1} の出力 {name} を送りましたが、読み戻すと {new_text} のままでした。")
    if name == "SOLO" and after[key]:
        warnings.append("SOLO は本体で効き方を確かめていません(pyquadcortex でも未測定)。")
    return _Done(f"エディター: 行{row + 1} の出力 {name} {old_text} → {new_text}",
                 not _same(before[key], after[key]), [undo], [redo], warnings, {"lane": after})


def _gate(device, body: dict) -> _Done:
    row = _row(body)
    name = str(body.get("param") or "").upper()
    if name not in GATE_PARAMS:
        raise EditorError(f"param は {', '.join(GATE_PARAMS)} のどれかです。")
    catalog = device.catalog
    preset, scene = _live(device)
    before = gate_state(preset, row, scene, catalog)
    key = {"NOISE REDUCTION": "noise_reduction", "INPUT GAIN": "input_gain_db",
           "BYPASS": "bypassed"}[name]
    spec = _spec(catalog, GATE_MODEL, name)
    if spec is None:
        raise EditorError(f"入力ゲートの {name} がカタログにありません。")
    tol = 0.05 if name != "BYPASS" else WIRE_TOL
    if name != "BYPASS" and spec is not None and not spec.range_is_placeholder:
        tol = abs(spec.maximum - spec.minimum) * 0.002 + 1e-3
    _check(body, before[key], f"行{row + 1} の入力ゲート {name}", required=True, tol=tol)
    if name == "BYPASS":
        on = _bool(body, "on")
        sent = 1.0 if on else 0.0
    else:
        if spec.range_is_placeholder:
            raise EditorError(f"{name} は実際の単位の範囲がカタログから分からないため書けません。")
        real = _number(body, "real", spec.minimum, spec.maximum)
        sent = spec.to_normalized(real)
    device.set_gate_param(row, name, sent)
    after = gate_state(device.current_preset(), row, scene, catalog)
    warnings = []
    if name == "BYPASS":
        landed = after[key] == on
        text = lambda b: "—" if b is None else ("オフ(バイパス)" if b else "オン")
        undo = {"action": "gate", "row": row, "param": name, "on": before[key],
                "expect": after[key]}
        redo = {"action": "gate", "row": row, "param": name, "on": after[key],
                "expect": before[key]}
    else:
        landed = after[key] is not None and abs(after[key] - real) <= tol
        unit = "%" if name == "NOISE REDUCTION" else " dB"
        text = lambda v: "—" if v is None else f"{v:g}{unit}"
        undo = {"action": "gate", "row": row, "param": name, "real": before[key],
                "expect": after[key]}
        redo = {"action": "gate", "row": row, "param": name, "real": after[key],
                "expect": before[key]}
    if not landed:
        warnings.append(f"行{row + 1} の入力ゲート {name} を送りましたが、読み戻すと {text(after[key])} "
                        "でした。")
    changed = not _same(before[key], after[key], tol if name != "BYPASS" else WIRE_TOL)
    if before[key] is None:            # nothing to go back to
        undo = []
    return _Done(f"エディター: 行{row + 1} の入力ゲート {name} {text(before[key])} → {text(after[key])}",
                 changed, [undo] if undo else [], [redo], warnings, {"gate": after})


def _sub(device, body: dict, which: str) -> _Done:
    row = _row(body, even=True)
    name = str(body.get("param") or "")
    catalog = device.catalog
    preset, scene = _live(device)
    word = "スプリッター" if which == "splitter" else "ミキサー"
    state = sub_state(preset, row, which, scene, catalog)
    if state is None:
        raise EditorConflict(f"行{row + 1} に分岐がありません(本体側で変わった可能性があります)。"
                             "何も書き込まず、画面を読み直します。")
    param = next((p for p in state["params"] if p["name"].upper() == name.upper()), None)
    if param is None:
        raise EditorError(f"行{row + 1} の{word}に {name} というパラメーターはありません。")
    if not param["writable"]:
        raise EditorError(f"{word}の {param['name']} はここでは変えられません"
                          + ("(選択肢の名前が本体から読めないため。スイッチは数値では書きません)。"
                             if param["kind"] == "select" else "。"))
    if param["kind"] == "select":
        if "value" in body and "option" not in body:
            raise EditorError(f"{param['name']} はスイッチです。option(選択肢の名前)で指定してください。")
        option = body.get("option")
        if option not in (param["options"] or []):
            raise EditorError(f"{param['name']} に「{option}」という選択肢はありません。")
        _check(body, param["text"], f"行{row + 1} の{word} {param['name']}")
        sent = pq.option_value(param["options"], option)
    else:
        _check(body, param["value"], f"行{row + 1} の{word} {param['name']}")
        if "db" in body and param["units"] == "dB" and "LEVEL" in param["name"].upper():
            try:
                sent = pq.db_to_lane_level(_number(body, "db"))
            except ValueError:
                raise EditorError("レベルは -40〜+12 dB です。") from None
        else:
            sent = _number(body, "value", 0.0, 1.0)
    setter = device.set_splitter_param if which == "splitter" else device.set_mixer_param
    try:
        setter(row, param["name"], sent)
    except KeyError:
        raise EditorError(f"{param['name']} はカタログの{word}にありません。") from None
    after_state = sub_state(device.current_preset(), row, which, scene, catalog) or {"params": []}
    after = next((p for p in after_state["params"] if p["name"] == param["name"]), {})
    warnings = []
    if not _same(after.get("value"), round(sent, 6)):
        warnings.append(f"行{row + 1} の{word} {param['name']} を送りましたが、読み戻すと "
                        f"{after.get('text', '—')} でした。")
    key = "option" if param["kind"] == "select" else "value"
    old = param["text"] if key == "option" else param["value"]
    new = after.get("text") if key == "option" else after.get("value")
    undo = {"action": which, "row": row, "param": param["name"], key: old,
            "expect": new} if old is not None else None
    redo = {"action": which, "row": row, "param": param["name"], key: new, "expect": old}
    return _Done(f"エディター: 行{row + 1} の{word} {param['name']} {param['text']} → "
                 f"{after.get('text', '—')}", not _same(param["value"], after.get("value")),
                 [undo] if undo else [], [redo], warnings, {which: after_state})


def _split_mute(device, body: dict) -> _Done:
    row = _row(body, even=True)
    muted = _bool(body, "muted")
    preset, _ = _live(device)
    before = split_muted(preset, row)
    if before is None:
        raise EditorConflict(f"行{row + 1} に分岐がありません(本体側で変わった可能性があります)。"
                             "何も書き込まず、画面を読み直します。")
    _check(body, before, f"行{row + 1} の分岐のミュート")
    device.set_split_mute(row, muted)
    after = split_muted(device.current_preset(), row)
    text = lambda b: "ミュート" if b else "オン"
    warnings = [] if after == muted else [
        f"行{row + 1} の分岐のミュートを送りましたが、読み戻すと {text(after)} でした。"]
    return _Done(f"エディター: 行{row + 1} の分岐 {text(before)} → {text(after)}",
                 before != after,
                 [{"action": "split_mute", "row": row, "muted": before, "expect": after}],
                 [{"action": "split_mute", "row": row, "muted": after, "expect": before}],
                 warnings, {"split_muted": after})


def _parse_color(body: dict):
    if isinstance(body.get("argb"), int) and not isinstance(body.get("argb"), bool):
        return int(body["argb"]) & 0xFFFFFFFF
    color = body.get("color")
    if not isinstance(color, str) or len(color) != 7 or color[0] != "#":
        raise EditorError("color は #rrggbb で指定してください。")
    try:
        return 0xFF000000 | int(color[1:], 16)
    except ValueError:
        raise EditorError("color は #rrggbb で指定してください。") from None


def _scene_color(device, body: dict) -> _Done:
    scene = _scene_arg(body, "scene")
    argb = _parse_color(body)
    preset, _ = _live(device)
    before = _argb(preset, scene)
    _check(body, _hex(before), f"シーン {FOOTSWITCHES[scene]} の色")
    device.set_scene_color(scene, argb)
    after = _argb(device.current_preset(), scene)
    warnings = [] if after == argb else [
        f"シーン {FOOTSWITCHES[scene]} の色を送りましたが、読み戻すと {_hex(after) or 'なし'} でした。"]
    if not before:
        warnings.append("元の色が読めなかったため、取り消すと色の値 0 を書きます(本体での見え方は"
                        "確かめていません)。")
    return _Done(f"エディター: シーン {FOOTSWITCHES[scene]} の色 {_hex(before) or 'なし'} → "
                 f"{_hex(after) or 'なし'}", before != after,
                 [{"action": "scene_color", "scene": scene, "argb": before, "expect": _hex(after)}],
                 [{"action": "scene_color", "scene": scene, "argb": after, "expect": _hex(before)}],
                 warnings, {"scene_colors": scene_colors(device.current_preset())})


def _scene_copy(device, body: dict) -> _Done:
    a = _scene_arg(body, "from")
    b = _scene_arg(body, "to")
    swap = body.get("swap", False)
    if not isinstance(swap, bool):
        raise EditorError("swap は true か false です。")
    if a == b:
        raise EditorError("コピー元とコピー先が同じシーンです。")
    preset, _ = _live(device)
    labels = list(preset.scene_labels)
    device.copy_scene(a, b, swap)
    after = device.current_preset()
    new_labels = list(after.scene_labels)
    warnings = []
    if b < len(labels) and a < len(labels) and b < len(new_labels) and new_labels[b] != labels[a]:
        warnings.append("コピーを送りましたが、読み戻すとコピー先のシーン名が変わっていません"
                        "(本体が受け付けなかった可能性があります)。")
    sa, sb = FOOTSWITCHES[a], FOOTSWITCHES[b]
    readback = {"scene_labels": [str(x) for x in new_labels[:SCENES]],
                "scene_colors": scene_colors(after)}
    if not swap:
        return _Done(f"エディター: シーン {sa} を シーン {sb} にコピー", True, warnings=warnings,
                     readback=readback, history=False, note=COPY_NOTE)
    item = {"action": "scene_copy", "from": a, "to": b, "swap": True}
    return _Done(f"エディター: シーン {sa} と シーン {sb} を入れ替え", True, [item], [item],
                 warnings, readback)


def _scene_mode(device, body: dict) -> _Done:
    block = _block(device, body)
    param = _block_param(block, body.get("param"))
    enabled = _bool(body, "enabled")
    row, col, index = block["row"], block["col"], param["index"]
    before = param_extra(device.current_preset(), row, col, index)
    _check(body, before["scene_mode"], f"{editor._where(block)} {param['name']} のシーンごとの値")
    device.set_param_scene_mode(row, col, index, enabled)
    after = param_extra(device.current_preset(), row, col, index)
    warnings = []
    if after["scene_mode"] != enabled:
        warnings.append(f"{param['name']} のシーンごとの設定を送りましたが、読み戻すと変わっていません"
                        "でした。")
    elif enabled:
        warnings.append("このつまみはシーンごとに値を覚えます。いまの値が全シーンの出発点です。")
    else:
        warnings.append("シーンごとの値をやめました。値は 1 つになります(どのシーンの値が残るかは"
                        "本体が決めます。読み戻した値を表示します)。")
    if after["expression"]:
        warnings.append(EXPRESSION_WARNING)
    word = lambda b: "シーンごと" if b else "全シーン共通"
    base = {"action": "scene_mode", "row": row, "col": col, "model_id": block["model_id"],
            "param": param["name"]}
    return _Done(f"エディター: {editor._where(block)} {param['name']} {word(before['scene_mode'])}"
                 f" → {word(after['scene_mode'])}", before["scene_mode"] != after["scene_mode"],
                 [{**base, "enabled": before["scene_mode"], "expect": after["scene_mode"]}],
                 [{**base, "enabled": after["scene_mode"], "expect": before["scene_mode"]}],
                 warnings, {"param": {"name": param["name"], **after}})


def _tempo(device, body: dict) -> _Done:
    catalog = device.catalog
    spec = _tempo_spec(catalog)
    if spec is None:
        raise EditorError("テンポのモデル(25000)の TEMPO がカタログにないため、テンポは変えられません。")
    if spec.range_is_placeholder:
        raise EditorError("カタログがテンポの範囲を 0〜1 としか示していないため、BPM を本体の値に"
                          "変換できません。テンポは本体で変えてください(何も書き込んでいません)。")
    lo, hi = max(TEMPO_RANGE[0], spec.minimum), min(TEMPO_RANGE[1], spec.maximum)
    bpm = round(_number(body, "bpm", lo, hi), 1)
    preset, _ = _live(device)
    before, _ = tempo_bpm(preset, catalog)
    _check(body, before, "テンポ", tol=0.06)
    device.set_tempo(bpm)
    after, _ = tempo_bpm(device.current_preset(), catalog)
    warnings = [] if after is not None and abs(after - bpm) <= 0.06 else [
        f"テンポ {bpm:g} BPM を送りましたが、読み戻すと {after if after is not None else '—'} でした。"]
    text = lambda v: "—" if v is None else f"{v:g} BPM"
    undo = [{"action": "tempo", "bpm": before, "expect": after}] if before is not None else []
    return _Done(f"エディター: テンポ {text(before)} → {text(after)}",
                 not _same(before, after, 0.06), undo,
                 [{"action": "tempo", "bpm": after, "expect": before}], warnings,
                 {"tempo": after})


def _switch_state(preset) -> dict:
    return {fs: (e["label"], e["momentary"], len(e["blocks"])) for fs, e in stomp_map(preset).items()}


def _side_effects(before: dict, after: dict) -> tuple[list, list]:
    """Label / momentary restores for switches whose label or flag changed as a side effect."""
    undo, notes = [], []
    for fs, (label, momentary, _count) in before.items():
        now = after.get(fs, ("", False, 0))
        if label and now[0] != label:
            undo.append({"action": "stomp_label", "footswitch": fs, "label": label,
                         "expect": now[0]})
            notes.append(f"フットスイッチ {fs} の名前「{label}」が本体で消えました(取り消すと戻します)。")
        if momentary and not now[1]:
            undo.append({"action": "stomp_momentary", "footswitch": fs, "momentary": True,
                         "expect": False})
            notes.append(f"フットスイッチ {fs} のモーメンタリーが本体で外れました(取り消すと戻します)。")
    return undo, notes


def _stomp(device, body: dict) -> _Done:
    block = _block(device, body)
    target = _footswitch(body, allow_none=True)
    row, col = block["row"], block["col"]
    preset = device.current_preset()
    before = _stomp_of(preset, row, col)
    _check(body, before, f"{editor._where(block)} のフットスイッチ")
    switches = _switch_state(preset)
    if target == before:
        return _Done("", False, readback={"stomp": before})
    if target is None:
        device.clear_stomp_assignment(row, col)
    else:
        device.set_stomp_assignment(row, col, FOOTSWITCHES.index(target))
    after_preset = device.current_preset()
    after = _stomp_of(after_preset, row, col)
    warnings = [] if after == target else [
        f"{editor._where(block)} のフットスイッチを送りましたが、読み戻すと {after or 'なし'} でした。"]
    extra_undo, notes = _side_effects(switches, _switch_state(after_preset))
    warnings += notes
    base = {"action": "stomp", "row": row, "col": col, "model_id": block["model_id"]}
    return _Done(f"エディター: {editor._where(block)} のフットスイッチ {before or 'なし'} → "
                 f"{after or 'なし'}", before != after,
                 [{**base, "footswitch": before, "expect": after}] + extra_undo,
                 [{**base, "footswitch": after, "expect": before}], warnings,
                 {"stomp": after, "switches": stomp_map(after_preset)})


def _assigned(preset, fs: str) -> dict:
    entry = stomp_map(preset).get(fs)
    if entry is None:
        raise EditorError(f"フットスイッチ {fs} にはブロックが割り当てられていません。")
    return entry


def _stomp_label(device, body: dict) -> _Done:
    fs = _footswitch(body)
    label = body.get("label")
    if not isinstance(label, str):
        raise EditorError("label は文字列で指定してください。")
    label = label.strip()[:LABEL_LIMIT]
    preset = device.current_preset()
    entry = _assigned(preset, fs)
    before = entry["label"]
    _check(body, before, f"フットスイッチ {fs} の名前")
    single = len(entry["blocks"]) == 1
    device.set_stomp_label(FOOTSWITCHES.index(fs), label, single=single)
    after = stomp_map(device.current_preset()).get(fs, {}).get("label", "")
    warnings = [] if after == label else [
        f"フットスイッチ {fs} の名前を送りましたが、読み戻すと「{after}」でした。"]
    return _Done(f"エディター: フットスイッチ {fs} の名前「{before}」→「{after}」", before != after,
                 [{"action": "stomp_label", "footswitch": fs, "label": before, "expect": after}],
                 [{"action": "stomp_label", "footswitch": fs, "label": after, "expect": before}],
                 warnings, {"label": after})


def _stomp_momentary(device, body: dict) -> _Done:
    fs = _footswitch(body)
    momentary = _bool(body, "momentary")
    preset = device.current_preset()
    entry = _assigned(preset, fs)
    before = entry["momentary"]
    _check(body, before, f"フットスイッチ {fs} のモーメンタリー")
    device.set_stomp_momentary(FOOTSWITCHES.index(fs), momentary)
    after = stomp_map(device.current_preset()).get(fs, {}).get("momentary", False)
    warnings = []
    if after != momentary:
        why = ("このフットスイッチは 2 つ以上のブロックを動かしているため、本体はモーメンタリーを受け"
               "付けません(本体の仕様)。" if len(entry["blocks"]) != 1 else "")
        warnings.append(f"フットスイッチ {fs} のモーメンタリーを送りましたが、読み戻すと変わって"
                        f"いませんでした。{why}")
    word = lambda b: "モーメンタリー" if b else "ラッチ"
    return _Done(f"エディター: フットスイッチ {fs} {word(before)} → {word(after)}", before != after,
                 [{"action": "stomp_momentary", "footswitch": fs, "momentary": before,
                   "expect": after}],
                 [{"action": "stomp_momentary", "footswitch": fs, "momentary": after,
                   "expect": before}], warnings, {"momentary": after})


def _expression(device, body: dict) -> _Done:
    block = _block(device, body)
    param = _block_param(block, body.get("param"))
    pedal = body.get("pedal")
    if pedal not in (0, 1, 2) or isinstance(pedal, bool):
        raise EditorError("pedal は 0(外す)、1、2 のどれかです。")
    row, col, index = block["row"], block["col"], param["index"]
    before = param_extra(device.current_preset(), row, col, index)
    expect = body.get("expect")
    if isinstance(expect, dict):
        now = before
        if expect.get("pedal") != now["expression"] or \
                (now["expression"] and (not _same(expect.get("min"), now["exp_min"]) or
                                        not _same(expect.get("max"), now["exp_max"]))):
            raise EditorConflict(f"{editor._where(block)} {param['name']} のペダルの割り当てが本体側で"
                                 "変わっていました。何も書き込まず、画面を読み直します。")
    else:
        _check(body, before["expression"], f"{editor._where(block)} {param['name']} のペダル")
    if pedal:
        lo = _number(body, "min", 0.0, 1.0)
        hi = _number(body, "max", 0.0, 1.0)
    else:
        lo = before["exp_min"] if before["exp_min"] is not None else 0.0
        hi = before["exp_max"] if before["exp_max"] is not None else 1.0
    device.set_expression(row, col, index, pedal, lo, hi)
    after = param_extra(device.current_preset(), row, col, index)
    warnings = []
    landed = after["expression"] == pedal and (not pedal or (
        _same(after["exp_min"], lo) and _same(after["exp_max"], hi)))
    if not landed:
        warnings.append(f"{param['name']} のペダルの割り当てを送りましたが、読み戻すと "
                        f"{'EXP ' + str(after['expression']) if after['expression'] else 'なし'} "
                        "でした。")
    if pedal and after["expression"]:
        warnings.append(EXPRESSION_WARNING)
    if not pedal:
        warnings.append("ペダルを外す書き込み(expression 0)は、本体での動作を確かめていません。"
                        "読み戻した結果を表示しています。")
    word = lambda e: f"EXP {e['expression']}" if e["expression"] else "なし"
    base = {"action": "expression", "row": row, "col": col, "model_id": block["model_id"],
            "param": param["name"]}

    def body_of(state, other):
        return {**base, "pedal": state["expression"],
                "min": state["exp_min"] if state["exp_min"] is not None else 0.0,
                "max": state["exp_max"] if state["exp_max"] is not None else 1.0,
                "expect": {"pedal": other["expression"], "min": other["exp_min"],
                           "max": other["exp_max"]}}
    changed = before != after
    return _Done(f"エディター: {editor._where(block)} {param['name']} のペダル {word(before)} → "
                 f"{word(after)}", changed, [body_of(before, after)], [body_of(after, before)],
                 warnings, {"param": {"name": param["name"], **after}})


def _gig_view(device, body: dict) -> _Done:
    _bool(body, "shown")
    device.set_gig_view(body["shown"])          # refused by the safety rules (ForbiddenCall)
    raise EditorError("Gig View は切り替えません。")           # not reached


HANDLERS = {
    "lane": _lane, "gate": _gate,
    "splitter": lambda d, b: _sub(d, b, "splitter"), "mixer": lambda d, b: _sub(d, b, "mixer"),
    "split_mute": _split_mute, "scene_color": _scene_color, "scene_copy": _scene_copy,
    "scene_mode": _scene_mode, "tempo": _tempo, "stomp": _stomp, "stomp_label": _stomp_label,
    "stomp_momentary": _stomp_momentary, "expression": _expression, "gig_view": _gig_view,
}
ACTIONS = tuple(HANDLERS)


def _run(device, action: str, body: dict) -> _Done:
    handler = HANDLERS.get(action)
    if handler is None:
        raise EditorError(f"知らない操作です: {action}")
    if not isinstance(body, dict):
        raise EditorError("本文は JSON のオブジェクトで送ってください。")
    try:
        return handler(device, body)
    except ValueError as exc:           # odd row, bad option... from pyquadcortex
        raise EditorError(f"本体に送れませんでした(何も書き込んでいません): {exc}") from None


def write(device, action: str, body: dict) -> dict:
    """Carry out one change and record it as one undo step.

    Answers ``{"id": history id or None, "warnings": [...], "note": str, ...}``
    plus what was read back. Raises EditorConflict (nothing written) when the
    unit no longer shows what the page showed, EditorError for refusals.
    """
    done = _run(device, action, body)
    entry_id = None
    if done.history and done.changed:
        snap = device.grid()
        entry_id = panel.record(device.mock, {"note": "editor extra", "ops": []}, snap, snap,
                                instruction=done.label, engine=ENGINE, summary=[done.label],
                                warnings=done.warnings, undo_extra=done.undo,
                                redo_extra=done.redo)
    return {"id": entry_id, "warnings": done.warnings, "note": done.note, **done.readback}


def apply_extra(device, action: str, body: dict) -> dict:
    """The same change without a history entry (panel undo / redo replay it)."""
    done = _run(device, action, body)
    return {"warnings": done.warnings, "note": done.note, **done.readback}


def add_cli(sub):
    """No CLI commands (the editor page uses these through the web app)."""


__all__ = ["write", "apply_extra", "decorate", "ACTIONS", "QcsuiteError"]
