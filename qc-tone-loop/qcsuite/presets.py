"""Presets as stable, diffable JSON: normalize, fingerprint, compare, describe.

A ``BinaryPreset`` from the unit is a positional protobuf full of traps (empty
cells padded out, NaN in unused scene slots, a gain-reduction meter sampled at
save time, list values that rescale when blocks are added). :func:`normalize`
turns one into a plain dict with every knob named, in real units where the
catalog gives them, and nothing that changes between two saves of the same rig
- so a git diff of two backups shows only what the player changed.

Schema ``qcsuite.preset/1``::

    {"schema", "name", "folder", "position", "slot", "default_scene",
     "scene_labels": [8], "tags", "rows": [{"row", "in_port", "out_port",
     "lane": {"VOLUME": {"value", "db"}, "PAN", "MUTE", "SOLO"}, "gate": {...}}],
     "splits": [{"row", "split_column", "mix_column"}],
     "blocks": [{"row", "col", "model_id", "model", "category", "plugin",
                 "inert_plugin", "capture", "mic", "bypass": {"scene_mode", "scenes"},
                 "params": {"GAIN": {"index", "value", "real", "units"},
                            "BRIGHT": {"index", "value", "option", "options"},
                            "file_name": {"index", "text"}, ...}}],
     "stomps", "midi_on_load", "fingerprint"}
"""

from __future__ import annotations

import hashlib
import json
import math

import pyquadcortex as pq

from qcsuite.device import inert

SCHEMA = "qcsuite.preset/1"
SCENES = "ABCDEFGH"
LANE_MODEL, GATE_MODEL = 23000, 28000
VALUE_TOLERANCE = 1e-3


def _clean(v, digits=4):
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, digits)
    return v


def _port(enum, value):
    try:
        return enum(value).name
    except ValueError:
        return str(value)


def _sub_params(collection, catalog, model_id, skip=()):
    if not len(collection):
        return {}
    model = catalog.get(model_id)
    names = [s.name for s in model.parameters] if model else []
    out = {}
    for i, prm in enumerate(collection[0].params):
        name = names[i] if i < len(names) else f"index {i}"
        if name in skip or not len(prm.param_values):
            continue
        pv = prm.param_values[0]
        if not pq.field_present(pv, "float_value"):
            continue
        entry = {"value": _clean(pv.float_value)}
        if model_id == LANE_MODEL and name == "VOLUME" and entry["value"] is not None:
            entry["db"] = round(pq.lane_level_db(pv.float_value), 2)
        if prm.scene_mode:
            entry["scenes"] = [_clean(x.float_value) for x in prm.param_values]
        out[name] = entry
    return out


def _param_entry(p, b, spec):
    try:
        state = pq.param_state(p, b.row, b.column, spec.index)
    except IndexError:
        return None
    values = state.values
    first = values[0] if values else None
    entry = {"index": spec.index}
    if isinstance(first, str):
        entry["text"] = first
        return entry
    if first is None or (isinstance(first, float) and math.isnan(first)):
        entry["value"] = None
        return entry
    entry["value"] = _clean(first)
    options = pq.param_options(p, b.row, b.column, spec.index)
    if options:
        entry["option"] = pq.option_at(options, first)
        entry["options"] = options
    elif spec.option_count:
        entry["option"] = spec.value_to_option(first)
    elif not spec.range_is_placeholder:
        entry["real"] = _clean(spec.to_real(first), 3)
        if spec.units:
            entry["units"] = spec.units
    if state.scene_mode:
        entry["scenes"] = [_clean(v) if isinstance(v, float) else v for v in values]
    return entry


def normalize(p, catalog, *, folder: str | None = None, position: int | None = None) -> dict:
    """The JSON form of a ``BinaryPreset`` (see module docstring)."""
    rows = []
    for i, chain in enumerate(p.chains):
        row = chain.row if pq.field_present(chain, "row") else i
        rows.append({
            "row": row,
            "in_port": _port(pq.Input, chain.in_portid),
            "out_port": _port(pq.Output, chain.out_portid),
            "lane": _sub_params(chain.output_control, catalog, LANE_MODEL),
            "gate": _sub_params(chain.input_control, catalog, GATE_MODEL,
                                skip=("GAIN REDUCTION", "DUMMY")),
        })
    blocks = []
    for b in pq.blocks(p):
        model = catalog.get(b.model_id)
        entry = {"row": b.row, "col": b.column, "model_id": b.model_id,
                 "model": model.name if model else f"unknown {b.model_id}",
                 "category": model.category if model else None,
                 "plugin": (getattr(model, "plugin_id", None) or None) if model else None,
                 "inert_plugin": inert.inert_plugin(model)}
        params = {}
        if model is not None:
            for spec in model.parameters:
                if spec.type == "grMeter":
                    continue
                pe = _param_entry(p, b, spec)
                if pe is None:
                    continue
                key = spec.name if spec.name not in params else f"{spec.name}#{spec.index}"
                params[key] = pe
        entry["params"] = params
        text = params.get("file_name", {}).get("text")
        if text:
            entry["capture"] = text[64:] if len(text) > 64 else text
        mic = params.get("ir selector", {}).get("text")
        if mic:
            entry["mic"] = mic
        ir_name = params.get("IR NAME", {}).get("text")
        if ir_name:
            entry["ir"] = ir_name
        try:
            bp = pq.bypass_state(p, b.row, b.column)
            entry["bypass"] = {"scene_mode": bp.scene_mode, "scenes": list(bp.scenes)}
        except IndexError:
            entry["bypass"] = None
        blocks.append(entry)
    blocks.sort(key=lambda e: (e["row"], e["col"]))
    out = {
        "schema": SCHEMA,
        "name": p.name if pq.field_present(p, "name") else None,
        "folder": folder,
        "position": position,
        "slot": pq.position_to_slot(position) if position is not None and 0 <= position < 256 else None,
        "default_scene": SCENES[p.default_scene] if p.default_scene < 8 else str(p.default_scene),
        "scene_labels": [l if l.strip() else "" for l in p.scene_labels],
        "tags": list(p.tags),
        "rows": rows,
        "splits": [s._asdict() for s in pq.splits(p)],
        "blocks": blocks,
        "stomps": [s._asdict() for s in pq.stomp_assignments(p)],
        "midi_on_load": [m._asdict() for m in pq.preset_load_midi_out(p)],
    }
    out["fingerprint"] = fingerprint(out)
    return out


def _sound_relevant(n: dict) -> dict:
    """The part of a normalized preset that decides how it sounds."""
    def r(v):
        return round(v, 3) if isinstance(v, float) else v
    return {
        "rows": [{"in": x["in_port"], "out": x["out_port"],
                  "lane": {k: r(v.get("value")) for k, v in x["lane"].items()},
                  "gate": {k: r(v.get("value")) for k, v in x["gate"].items()}}
                 for x in n["rows"]],
        "splits": n["splits"],
        "blocks": [{"at": (b["row"], b["col"]), "model": b["model_id"],
                    "params": {k: r(v.get("value", v.get("text"))) for k, v in b["params"].items()},
                    "bypass": b["bypass"]}
                   for b in n["blocks"]],
    }


def fingerprint(n: dict) -> str:
    """Content hash: equal for presets that sound the same whatever their name."""
    blob = json.dumps(_sound_relevant(n), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def block_label(b: dict) -> str:
    return f"行{b['row'] + 1} 列{b['col'] + 1} {b['model']}"


def _value_text(entry: dict | None):
    if entry is None:
        return None
    for k in ("option", "real", "text", "value"):
        if k in entry and entry[k] is not None:
            unit = entry.get("units", "") if k == "real" else ""
            return f"{entry[k]}{(' ' + unit) if unit else ''}"
    return None


def _same(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    if "text" in a or "text" in b:
        return a.get("text") == b.get("text")
    va, vb = a.get("value"), b.get("value")
    if va is None or vb is None:
        return va is vb
    if a.get("scenes") != b.get("scenes") and (a.get("scenes") or b.get("scenes")):
        return False
    return abs(va - vb) <= VALUE_TOLERANCE


def diff(a: dict, b: dict) -> list[dict]:
    """What changed from normalized preset ``a`` to ``b``, as readable entries."""
    out = []

    def add(kind, where, before=None, after=None):
        out.append({"kind": kind, "where": where, "before": before, "after": after})

    if a.get("name") != b.get("name"):
        add("changed", "名前", a.get("name"), b.get("name"))
    for ra, rb in zip(a["rows"], b["rows"]):
        label = f"行{ra['row'] + 1}"
        for key, word in (("in_port", "入力"), ("out_port", "出力")):
            if ra[key] != rb[key]:
                add("changed", f"{label} {word}", ra[key], rb[key])
        for name in sorted(set(ra["lane"]) | set(rb["lane"])):
            if not _same(ra["lane"].get(name), rb["lane"].get(name)):
                ea, eb = ra["lane"].get(name) or {}, rb["lane"].get(name) or {}
                add("changed", f"{label} 出力 {name}",
                    ea.get("db", ea.get("value")), eb.get("db", eb.get("value")))
        for name in sorted(set(ra["gate"]) | set(rb["gate"])):
            if not _same(ra["gate"].get(name), rb["gate"].get(name)):
                add("changed", f"{label} ゲート {name}",
                    (ra["gate"].get(name) or {}).get("value"),
                    (rb["gate"].get(name) or {}).get("value"))
    cells_a = {(x["row"], x["col"]): x for x in a["blocks"]}
    cells_b = {(x["row"], x["col"]): x for x in b["blocks"]}
    for cell in sorted(set(cells_a) | set(cells_b)):
        ba, bb = cells_a.get(cell), cells_b.get(cell)
        if ba is None:
            add("added", block_label(bb))
            continue
        if bb is None:
            add("removed", block_label(ba))
            continue
        if ba["model_id"] != bb["model_id"]:
            add("replaced", f"行{cell[0] + 1} 列{cell[1] + 1}", ba["model"], bb["model"])
            continue
        for name in sorted(set(ba["params"]) | set(bb["params"]),
                           key=lambda k: (ba["params"].get(k) or bb["params"].get(k))["index"]):
            pa_, pb_ = ba["params"].get(name), bb["params"].get(name)
            if not _same(pa_, pb_):
                add("changed", f"{block_label(bb)} {name}", _value_text(pa_), _value_text(pb_))
        if ba.get("bypass") != bb.get("bypass"):
            add("changed", f"{block_label(bb)} バイパス",
                _bypass_text(ba.get("bypass")), _bypass_text(bb.get("bypass")))
    for i, (la, lb) in enumerate(zip(a["scene_labels"], b["scene_labels"])):
        if la != lb:
            add("changed", f"シーン {SCENES[i]} の名前", la, lb)
    return out


def _bypass_text(bp):
    if not bp:
        return None
    marks = "".join("-" if s else "o" for s in bp["scenes"])
    return f"{'シーン別' if bp['scene_mode'] else '共通'} {marks}"


def diff_text(changes: list[dict]) -> str:
    words = {"changed": "変更", "added": "追加", "removed": "削除", "replaced": "差し替え"}
    lines = []
    for c in changes:
        if c["kind"] in ("added", "removed"):
            lines.append(f"[{words[c['kind']]}] {c['where']}")
        else:
            lines.append(f"[{words[c['kind']]}] {c['where']}: {c['before']} → {c['after']}")
    return "\n".join(lines) if lines else "(違いはありません)"


def chain_text(n: dict) -> list[str]:
    """One line per used row: input -> blocks -> output."""
    lines = []
    for r in n["rows"]:
        blocks = [b for b in n["blocks"] if b["row"] == r["row"]]
        if not blocks and r["in_port"] == "EMPTY":
            continue
        parts = []
        for b in blocks:
            label = b["model"]
            if b.get("capture"):
                label += f" «{b['capture']}»"
            bp = b.get("bypass") or {}
            if bp and all(bp.get("scenes", [])):
                label += " (バイパス)"
            parts.append(label)
        lines.append(f"行{r['row'] + 1}: [{r['in_port']}] → " + " → ".join(parts or ["(空)"])
                     + f" → [{r['out_port']}]")
    return lines


def main_knobs(b: dict, limit: int = 8) -> list[tuple[str, str]]:
    """The knobs worth printing for a block (no internal strings)."""
    out = []
    for name, entry in b["params"].items():
        if "text" in entry or name.startswith("RESERVED"):
            continue
        out.append((name, _value_text(entry)))
    return out[:limit]
