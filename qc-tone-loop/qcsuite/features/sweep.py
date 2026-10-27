"""Sweeps (feature 6) and the one-knob sensitivity map (idea M).

A sweep renders many configurations of the grid through the same DI and
records what each one measures - it never decides which is best. The player's
overnight run of about 430 configurations (roughly 16 s each for a 12 s DI on
the unit) worked like this, and this module generalizes it: say *which block,
which models, which knobs over which range*, and every configuration is set,
reamped, analyzed, checked for passthrough, optionally scored against a
reference, saved as FLAC and appended to a JSONL log that a crash resumes from.

Spec (JSON; a file or inline)::

    {"title": "2203 gain x treble",
     "di": "synthetic-riff",            # DI library name or a path
     "seconds": 12,                     # optional: use only the first N seconds
     "base": {"preset": {"folder": "kami", "slot": "1E"}}
           | {"patch": {...qctone patch...}}  |  "current",
     "reference": {"file": "ref.wav", "start": 30, "dur": 12},   # optional
     "mode": "grid" | "one_at_a_time",
     "cells": [
       {"row": 0, "col": 2,
        "models": ["Brit 2203", 1002],  # optional; omitted = keep the block there
        "params": {
          "gain":   [0.2, 0.5, 0.8],                    # normalized 0..1
          "TREBLE": {"from": 0.3, "to": 0.7, "steps": 3},
          "MID GAIN": {"real": [-3, 0, 3]},             # in the knob's own units
          "BRIGHT": {"options": ["Off", "On"]}}}]}      # switches: by option name only

Parameter keys are exact names or aliases resolved per model, because the same
knob is named differently from block to block (measured: the gain knob is GAIN,
DRIVE, VOLUME, INPUT GAIN or GAIN 1/2; tone knobs are BASS, MID or MIDDLE,
TREBLE, PRESENCE, DEPTH or RESONANCE). A key a model lacks is skipped for that
model with a note. A knob with options (a switch) is never swept by value -
only by option name - which is a rule learned on the unit.

``grid`` is the cartesian product of every cell's models and values.
``one_at_a_time`` starts from a baseline (each cell's first model, knobs as in
the base) and varies one thing at a time: each other model alone, then each
value of each knob alone on the first model.

Each configuration starts from the same base: the base is set up once (a
recalled preset re-pointed at USB 5, a patch, or the grid as it is) and
snapshotted, and every configuration first returns the grid to that snapshot
with keyed edits - no recall per configuration, which would cost 10-25 s and
an audio dropout each time on the unit.

Exclusions: a render whose audio is the DI coming back (audio test), a grid
holding a block from an unowned plugin, or a capture block placed or changed by
the sweep (captures do not switch from the computer - measured on the unit) is
marked excluded with the reason and kept out of rankings and tournaments.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pyquadcortex as pq

from qcsuite import jobs, paths
from qcsuite.audio import di as di_mod
from qcsuite.audio import reamp, score
from qcsuite.device import guard, inert
from qcsuite.errors import QcsuiteError
from qctone import device as qdevice
from qctone import features

SR = 48000
#: Above this many configurations a sweep needs an explicit go-ahead.
LARGE_SWEEP = 500
#: Seconds a configuration takes on the unit beyond the DI's own length: the
#: player's measurement was about 16 s for a 12 s DI (render + analysis).
OVERHEAD_S = 4.0
MOCK_SECONDS_PER_CONFIG = 0.3
CAPTURE_MODELS = (14000, 14001)

ALIASES = {
    "gain": ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1", "VOLUME"),
    "gain2": ("GAIN 2",),
    "bass": ("BASS",),
    "mid": ("MID", "MIDDLE"),
    "treble": ("TREBLE",),
    "presence": ("PRESENCE",),
    "depth": ("DEPTH", "RESONANCE"),
    "level": ("MASTER", "VOLUME", "LEVEL"),
}


# -- spec ----------------------------------------------------------------------

def load_spec(raw) -> dict:
    """A spec from a dict, an inline JSON string or a path to a JSON file."""
    if isinstance(raw, dict):
        return raw
    try:
        if str(raw).lstrip().startswith("{"):
            return json.loads(raw)
        return json.loads(Path(raw).read_text())
    except (OSError, ValueError) as exc:
        raise QcsuiteError(f"スイープの指定を読めません: {exc}") from None


def resolve_param(model, key: str):
    """The model's parameter for ``key`` (exact name first, then the alias)."""
    wanted = key.strip().lower()
    for p in model.parameters:
        if p.name.lower() == wanted:
            return p
    for name in ALIASES.get(wanted, ()):
        for p in model.parameters:
            if p.name == name:
                return p
    return None


def _values(spec, value) -> list[dict]:
    """One knob's value list as op fragments: {"value"} / {"real"} / {"option"}."""
    is_switch = bool(spec.option_count)
    if isinstance(value, dict) and "options" in value:
        if not is_switch:
            raise QcsuiteError(f"{spec.name} はスイッチではないので options は使えません")
        return [{"option": str(o)} for o in value["options"]]
    if is_switch:
        raise QcsuiteError(f"{spec.name} はスイッチです。値ではなく {{\"options\": [...]}} で"
                           "選択肢の名前を指定してください(スイッチは値で触らない)")
    if isinstance(value, dict) and "real" in value:
        if spec.range_is_placeholder:
            raise QcsuiteError(f"{spec.name} には実単位の範囲がないので real は使えません")
        reals = _number_list(value["real"])
        for r in reals:
            if not spec.minimum <= r <= spec.maximum:
                raise QcsuiteError(f"{spec.name}={r} は {spec.minimum}..{spec.maximum} の範囲外です")
        return [{"real": round(r, 4)} for r in reals]
    numbers = _number_list(value)
    for v in numbers:
        if not 0.0 <= v <= 1.0:
            raise QcsuiteError(f"{spec.name}={v}: 値は 0..1 で指定します(実単位は real)")
    return [{"value": round(v, 4)} for v in numbers]


def _number_list(value) -> list[float]:
    if isinstance(value, dict):
        steps = int(value.get("steps", 3))
        if steps < 1:
            raise QcsuiteError("steps は 1 以上にしてください")
        return [float(x) for x in np.linspace(float(value["from"]), float(value["to"]), steps)]
    if isinstance(value, (int, float)):
        return [float(value)]
    return [float(x) for x in value]


def _cell_models(cell: dict, catalog, base_grid: dict | None) -> list:
    if cell.get("models"):
        return [qdevice.resolve_model(catalog, m) for m in cell["models"]]
    here = None if base_grid is None else next(
        (b for b in base_grid["blocks"] if b["row"] == cell["row"] and b["col"] == cell["col"]), None)
    if here is None:
        raise QcsuiteError(f"行{cell['row'] + 1} 列{cell['col'] + 1} にはベースのブロックがありません。"
                           "models を指定してください")
    return [catalog[here["model_id"]]]


def _cell_variants(cell, model, notes) -> list[tuple[str, dict]]:
    """``[(param name, {"value"|"real"|"option": ...}), ...]`` per knob key, per model."""
    out = []
    for key, value in (cell.get("params") or {}).items():
        spec = resolve_param(model, key)
        if spec is None:
            notes.append(f"{model.name} には {key} がないので飛ばしました")
            continue
        out.append((spec.name, _values(spec, value)))
    return out


def _label(cells: list[dict]) -> str:
    parts = []
    for c in cells:
        knobs = ", ".join(f"{k}={next(iter(v.values()))}" for k, v in c["values"].items())
        parts.append(f"行{c['row'] + 1}列{c['col'] + 1} {c['model']}" + (f" / {knobs}" if knobs else ""))
    return " | ".join(parts)


def expand(spec: dict, catalog, base_grid: dict | None = None) -> dict:
    """Every configuration of ``spec``: ``{"configs": [...], "notes": [...]}``."""
    if not spec.get("cells"):
        raise QcsuiteError("cells が空です。どのブロックを動かすかを指定してください")
    mode = spec.get("mode", "grid")
    if mode not in ("grid", "one_at_a_time"):
        raise QcsuiteError(f"mode は grid か one_at_a_time です: {mode}")
    notes: list[str] = []
    per_cell = []
    for cell in spec["cells"]:
        for k in ("row", "col"):
            if not isinstance(cell.get(k), int):
                raise QcsuiteError(f"cells の各要素に {k}(0 始まり)が必要です")
        models = _cell_models(cell, catalog, base_grid)
        per_cell.append((cell, [(m, _cell_variants(cell, m, notes)) for m in models]))

    def entry(cell, model, values):
        return {"row": cell["row"], "col": cell["col"], "model_id": model.id, "model": model.name,
                "set_model": bool(cell.get("models")), "values": values}

    combos: list[tuple[list[dict], dict | None]] = []
    if mode == "grid":
        options = []
        for cell, models in per_cell:
            variants = []
            for model, knobs in models:
                names = [n for n, _ in knobs]
                for chosen in itertools.product(*[v for _, v in knobs]) if knobs else [()]:
                    variants.append(entry(cell, model, dict(zip(names, chosen))))
            options.append(variants)
        combos = [(list(c), None) for c in itertools.product(*options)]
    else:
        baseline = [entry(cell, models[0][0], {}) for cell, models in per_cell]
        combos.append((baseline, None))
        for i, (cell, models) in enumerate(per_cell):
            for model, _ in models[1:]:
                cells = list(baseline)
                cells[i] = entry(cell, model, {})
                combos.append((cells, {"cell": i, "model": model.name}))
            first, knobs = models[0]
            for name, values in knobs:
                for v in values:
                    cells = list(baseline)
                    cells[i] = entry(cell, first, {name: v})
                    combos.append((cells, {"cell": i, "param": name, **v}))
    configs = []
    for n, (cells, varied) in enumerate(combos):
        configs.append({"key": f"c{n:04d}", "index": n, "cells": cells, "varied": varied,
                        "label": _label(cells)})
    return {"configs": configs, "notes": list(dict.fromkeys(notes))}


def estimate_seconds(count: int, di_seconds: float, mock: bool) -> float:
    per = MOCK_SECONDS_PER_CONFIG if mock else di_seconds + OVERHEAD_S
    return round(count * per, 1)


# -- the base and per-config patches ------------------------------------------------

def _base_preset_position(ref: dict) -> tuple[str, int]:
    key = guard.folder_key(ref["folder"])
    if "slot" in ref:
        return key, pq.slot_to_position(str(ref["slot"]))
    return key, int(ref["position"])


def establish_base(rig, spec: dict, saved: dict | None = None) -> dict:
    """Set up the sweep's starting grid (recall / patch / as is); return its snapshot.

    With ``saved`` (a resumed job), a patch or "current" base is rebuilt by
    returning the grid to the saved snapshot instead of trusting whatever the
    grid holds now.
    """
    device = rig.device
    base = spec.get("base", "current")
    if isinstance(base, dict) and "preset" in base:
        key, position = _base_preset_position(base["preset"])
        if position not in {p["index"] for p in device.presets(key)}:
            raise QcsuiteError(f"ベースの「{guard.folder_name(key)}」{pq.position_to_slot(position)}"
                               " は空です")
        stored = device.read_stored(key, position)
        device.prepare_reamp(stored)
    elif saved is not None:
        patch = restore_patch(saved, device.grid(), device.catalog)
        if patch["ops"]:                      # already at the base: nothing to send
            device.apply_patch(patch)
    elif isinstance(base, dict) and "patch" in base:
        device.apply_patch(base["patch"])
    elif base != "current":
        raise QcsuiteError(f"base は preset / patch / \"current\" のどれかです: {base!r}")
    grid = device.grid()
    if not any(r["in_port"] in ("USB_5", "USB_5_6") for r in grid["rows"]):
        device.prepare_reamp()
        grid = device.grid()
    if not any(r["in_port"] in ("USB_5", "USB_5_6") for r in grid["rows"]):
        raise QcsuiteError("USB 5 から入る行がありません。ベースの入力を確認してください")
    return grid


def restore_patch(base: dict, current: dict, catalog) -> dict:
    """Keyed edits that return ``current`` to ``base``: only what differs.

    Switches are restored by option name (never by raw value); a switch whose
    option names are unknown was never changed by a sweep, so it is left alone.
    Strings (captures, mics) are not touched - they do not sound from here anyway.
    """
    ops = []
    now_rows = {r["row"]: r for r in current["rows"]}
    for r in base["rows"]:
        now = now_rows.get(r["row"], {})
        if now.get("in_port") != r["in_port"] and r["in_port"] in pq.Input.__members__:
            ops.append({"op": "set_input", "row": r["row"], "port": r["in_port"]})
        if now.get("out_port") != r["out_port"] and r["out_port"] in pq.Output.__members__:
            ops.append({"op": "set_output", "row": r["row"], "port": r["out_port"]})
    base_cells = {(b["row"], b["col"]): b for b in base["blocks"]}
    now_cells = {(b["row"], b["col"]): b for b in current["blocks"]}
    for cell in now_cells:
        if cell not in base_cells:
            ops.append({"op": "remove_block", "row": cell[0], "col": cell[1]})
    for cell, b in base_cells.items():
        now = now_cells.get(cell)
        replaced = now is None or now["model_id"] != b["model_id"]
        if replaced:
            ops.append({"op": "set_block", "row": cell[0], "col": cell[1], "model": b["model_id"]})
        model = catalog.get(b["model_id"])
        now_params = {} if replaced else {p["index"]: p for p in now["params"]}
        for p in b["params"]:
            if "value" not in p:
                continue
            cur = now_params.get(p["index"])
            if cur is not None and cur.get("value") is not None and \
                    abs(cur["value"] - p["value"]) < 1e-4:
                continue
            spec = model.parameters[p["index"]] if model and p["index"] < len(model.parameters) else None
            if "options" in p and "option" in p:
                ops.append({"op": "set_option", "row": cell[0], "col": cell[1],
                            "param": p["name"], "option": p["option"]})
            elif spec is not None and spec.option_count:
                continue
            else:
                ops.append({"op": "set_param", "row": cell[0], "col": cell[1],
                            "param": p["name"], "value": p["value"]})
        if b.get("bypassed") is not None and (replaced or now.get("bypassed") != b["bypassed"]):
            ops.append({"op": "bypass", "row": cell[0], "col": cell[1], "bypassed": b["bypassed"]})
    return {"note": "back to the sweep base", "ops": ops}


def config_ops(config: dict, base_grid: dict) -> list[dict]:
    """The edits that turn the base into ``config``."""
    ops = []
    for c in config["cells"]:
        here = next((b for b in base_grid["blocks"] if b["row"] == c["row"] and b["col"] == c["col"]),
                    None)
        if c["set_model"] and (here is None or here["model_id"] != c["model_id"]):
            ops.append({"op": "set_block", "row": c["row"], "col": c["col"], "model": c["model_id"]})
        for name, v in c["values"].items():
            op = {"row": c["row"], "col": c["col"], "param": name}
            if "option" in v:
                ops.append({"op": "set_option", **op, "option": v["option"]})
            else:
                ops.append({"op": "set_param", **op, **v})
    return ops


def static_reasons(config: dict, grid: dict, catalog) -> list[str]:
    """Blocks in the configured grid that do not sound when set from here."""
    reasons = []
    for b in grid["blocks"]:
        plugin = inert.inert_plugin(catalog.get(b["model_id"]))
        if plugin:
            reasons.append(f"行{b['row'] + 1}列{b['col'] + 1} {b['model']}: 未所有のプラグイン(素通し)")
    for c in config["cells"]:
        if c["model_id"] in CAPTURE_MODELS and (c["set_model"] or c["values"]):
            reasons.append(f"行{c['row'] + 1}列{c['col'] + 1}: キャプチャーはパソコンから切り替えても"
                           "音に反映されない")
    return reasons


# -- running ---------------------------------------------------------------------------

def sweeps_dir(mock: bool) -> Path:
    return paths.sub("sweeps", mock)


def _rel(path, mock: bool) -> str:
    return str(Path(path).resolve().relative_to(paths.home(mock).resolve()))


def load_di(spec: dict, mock: bool) -> tuple[np.ndarray, int]:
    name = spec.get("di", di_mod.SYNTH_NAME)
    data, sr = di_mod.load(name, mock)
    if spec.get("seconds"):
        data = data[: int(float(spec["seconds"]) * sr)]
    return data, sr


def load_reference(spec: dict) -> dict | None:
    ref = spec.get("reference")
    if not ref:
        return None
    if isinstance(ref, str):
        ref = {"file": ref}
    data, sr = features.load(ref["file"], float(ref.get("start", 0.0)), ref.get("dur"))
    return features.analyze_array(data, sr)


def create(rig, spec: dict, *, allow_large: bool = False) -> tuple[jobs.Job, dict]:
    """Set up the base, expand the spec and create the job (nothing rendered yet)."""
    data, sr = load_di(spec, rig.mock)
    base_grid = establish_base(rig, spec)
    expanded = expand(spec, rig.device.catalog, base_grid)
    count = len(expanded["configs"])
    if count > LARGE_SWEEP and not allow_large:
        raise QcsuiteError(f"{count} 通りになります({LARGE_SWEEP} を超えています)。"
                           "本当に実行するなら確認を付けてください(CLI: --yes)")
    meta = {"spec": spec, "notes": expanded["notes"], "count": count,
            "estimate_s": estimate_seconds(count, len(data) / sr, rig.mock)}
    job = jobs.create("sweep", meta, expanded["configs"], rig.mock,
                      title=spec.get("title") or f"スイープ {count} 通り")
    (job.dir / "base_grid.json").write_text(json.dumps(base_grid, ensure_ascii=False, indent=1))
    return job, {"job": job.id, "count": count, "estimate_s": meta["estimate_s"],
                 "notes": expanded["notes"]}


def make_worker(rig, job: jobs.Job, base_grid: dict | None = None, fmt: str = "flac"):
    """``work(config)`` for :func:`qcsuite.jobs.run`. Sets the base up on first use."""
    spec = job.spec["spec"]
    state: dict = {}

    def setup():
        di, sr = load_di(spec, rig.mock)
        saved_path = job.dir / "base_grid.json"
        saved = json.loads(saved_path.read_text()) if saved_path.exists() else None
        grid = base_grid if base_grid is not None else establish_base(rig, spec, saved)
        state.update(di=di, sr=sr, di_features=features.analyze_array(di[:, None], sr),
                     reference=load_reference(spec), base=grid,
                     out=paths.ensure(sweeps_dir(rig.mock) / job.id))

    def work(config: dict) -> dict:
        if not state:
            setup()
        device = rig.device
        catalog = device.catalog
        patch = restore_patch(state["base"], device.grid(), catalog)
        patch["ops"] += config_ops(config, state["base"])
        if patch["ops"]:
            device.apply_patch(patch)
        grid = device.grid()
        reasons = static_reasons(config, grid, catalog)
        info = reamp.render_current(rig, state["di"], state["sr"], state["di_features"],
                                    out_path=state["out"] / f"{config['key']}", fmt=fmt)
        pt = info["passthrough"]
        if pt["passthrough"]:
            reasons.append(pt["reason"])
        record = {"key": config["key"], "label": config["label"], "cells": config["cells"],
                  "varied": config["varied"], "ops": len(patch["ops"]),
                  "render": info["render"], "loudness": info["loudness"],
                  "features": info["features"], "passthrough": pt,
                  "excluded": {"excluded": bool(reasons), "reasons": reasons},
                  "file": _rel(info["file"], rig.mock)}
        if state["reference"] is not None:
            record["score"] = score.distance(info["features"], state["reference"])
        return record

    return work


def run(rig, job: jobs.Job, *, base_grid: dict | None = None, cancel=None, on_progress=None):
    return jobs.run(job, make_worker(rig, job, base_grid), cancel=cancel, on_progress=on_progress)


# -- results -------------------------------------------------------------------------------

SORTS = ("score", "distance", "lufs", "flatness", "crest", "tilt", "centroid", "key")


def rows(job: jobs.Job) -> list[dict]:
    """One flat row per finished configuration, for tables."""
    out = []
    for r in job.results():
        if not r.get("ok"):
            out.append({"key": r["key"], "label": "(失敗)", "error": r.get("error"),
                        "excluded": True, "reasons": [r.get("error", "")]})
            continue
        rec = r["result"]
        f = rec["features"]
        row = {"key": rec["key"], "label": rec["label"], "file": rec["file"],
               "lufs": rec["loudness"]["lufs"], "flatness": f["spectral"]["flatness_1k_5k"],
               "crest": f["dynamics"]["crest_db"], "tilt": f["spectral"]["tilt_db_per_oct"],
               "centroid": round(f["spectral"]["centroid_hz"]),
               "excluded": rec["excluded"]["excluded"], "reasons": rec["excluded"]["reasons"],
               "latency_ms": rec["render"]["latency_ms"], "varied": rec.get("varied")}
        if "score" in rec:
            row["score"] = rec["score"]["score"]
            row["distance"] = rec["score"]["distance"]
        out.append(row)
    return out


def results(job: jobs.Job, sort: str = "key", include_excluded: bool = True) -> dict:
    if sort not in SORTS:
        raise QcsuiteError(f"並べ替えは {', '.join(SORTS)} のどれかです")
    data = rows(job)
    if not include_excluded:
        data = [r for r in data if not r["excluded"]]
    descending = sort in ("score", "lufs", "flatness", "centroid")

    def order(r):
        if sort == "key":
            return (0, r["key"])
        v = r.get(sort)
        if v is None:
            return (1, 0.0)
        return (0, -v if descending else v)
    data.sort(key=order)
    has_ref = any("score" in r for r in data)
    return {"job": job.id, "title": job.spec.get("title"), "summary": job.summary(),
            "notes": job.spec.get("notes", []), "has_reference": has_ref, "rows": data,
            "excluded": sum(1 for r in data if r["excluded"]),
            "note": "数値は測定値です。どれが良い音かは、聴いて決めてください。"}


def to_tournament(job: jobs.Job, mock: bool, top: int | None = None,
                  keys: list[str] | None = None) -> dict:
    """Send the chosen (or top N) non-excluded renders to the blind tournament."""
    from qcsuite.features import tournament
    if not hasattr(tournament, "create"):
        raise QcsuiteError("ブラインド比較(tournament)の機能がまだ使えません")
    data = rows(job)
    usable = [r for r in data if not r["excluded"]]
    if keys:
        wanted = set(keys)
        chosen = [r for r in usable if r["key"] in wanted]
        skipped = sorted(wanted - {r["key"] for r in chosen})
    else:
        if any("score" in r for r in usable):
            usable.sort(key=lambda r: -r["score"])
        chosen = usable[: top or 8]
        skipped = []
    if len(chosen) < 2:
        raise QcsuiteError("比較には除外されていない候補が 2 つ以上必要です")
    home = paths.home(mock)
    candidates = [{"id": r["key"], "label": r["label"], "file": str(home / r["file"]),
                   "meta": {"sweep": job.id, "key": r["key"], "lufs": r["lufs"],
                            "score": r.get("score")}} for r in chosen]
    t = tournament.create(candidates, mock=mock, title=f"{job.spec.get('title')} から",
                          source=f"sweep:{job.id}", di=job.spec["spec"].get("di"))
    return {"tournament": t, "sent": [r["key"] for r in chosen], "skipped": skipped}


# -- idea M: one knob, how the measurements move -----------------------------------------

def sensitivity_spec(row: int, col: int, param: str, steps: int = 7, base="current",
                     di: str | None = None, model=None, seconds: float | None = None) -> dict:
    cell = {"row": row, "col": col, "params": {param: {"from": 0.0, "to": 1.0, "steps": steps}}}
    if model is not None:
        cell["models"] = [model]
    spec = {"title": f"効き方: 行{row + 1}列{col + 1} {param}", "mode": "one_at_a_time",
            "base": base, "di": di or di_mod.SYNTH_NAME, "cells": [cell], "kind": "sensitivity"}
    if seconds:
        spec["seconds"] = seconds
    return spec


def sensitivity(job: jobs.Job) -> dict:
    """The varied knob against tilt, centroid, flatness, crest and loudness."""
    points = []
    for r in job.results():
        if not r.get("ok"):
            continue
        rec = r["result"]
        varied = rec.get("varied") or {}
        if "param" not in varied:
            continue
        f = rec["features"]
        points.append({"value": varied.get("value", varied.get("real")),
                       "tilt": f["spectral"]["tilt_db_per_oct"],
                       "centroid": f["spectral"]["centroid_hz"],
                       "flatness": f["spectral"]["flatness_1k_5k"],
                       "crest": f["dynamics"]["crest_db"], "lufs": rec["loudness"]["lufs"],
                       "excluded": rec["excluded"]["excluded"]})
    points.sort(key=lambda p: p["value"])
    params = {(r["result"].get("varied") or {}).get("param") for r in job.results() if r.get("ok")}
    params.discard(None)
    return {"job": job.id, "param": next(iter(params), None), "points": points,
            "measures": {"tilt": "傾き dB/oct", "centroid": "重心 Hz", "flatness": "1〜5 kHz flatness",
                         "crest": "クレスト dB", "lufs": "LUFS"}}


def sensitivity_png(job: jobs.Job, path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    data = sensitivity(job)
    keys = list(data["measures"])
    fig, axes = plt.subplots(1, len(keys), figsize=(3.2 * len(keys), 3), dpi=100)
    x = [p["value"] for p in data["points"]]
    for ax, k in zip(axes, keys):
        ax.plot(x, [p[k] for p in data["points"]], "o-")
        ax.set_title(k, fontsize=9)
        ax.set_xlabel(data["param"] or "value", fontsize=8)
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


# -- CLI -------------------------------------------------------------------------------------

def _progress(done, total, key):
    print(f"  [{done + 1}/{total}] {key}", file=sys.stderr)


def cmd_preview(ctx, a):
    spec = load_spec(a.spec)
    with ctx.rig("sweep preview") as rig:
        needs_base = any(not c.get("models") for c in spec.get("cells", []))
        base = establish_base(rig, spec) if needs_base else None
        expanded = expand(spec, rig.device.catalog, base)
        data, sr = load_di(spec, rig.mock)
        est = estimate_seconds(len(expanded["configs"]), len(data) / sr, rig.mock)
    lines = [f"{len(expanded['configs'])} 通り、見込み {est / 60:.1f} 分"
             + ("(モック)" if ctx.mock else "(実機で 1 通り約 16 秒の実測から)")]
    lines += [f"  {c['key']}: {c['label']}" for c in expanded["configs"][:20]]
    if len(expanded["configs"]) > 20:
        lines.append("  …")
    lines += [f"注: {n}" for n in expanded["notes"]]
    ctx.out({"count": len(expanded["configs"]), "estimate_s": est, "notes": expanded["notes"],
             "configs": expanded["configs"][:50]}, "\n".join(lines))


def cmd_run(ctx, a):
    with ctx.rig("sweep") as rig:
        if a.resume:
            job = jobs.open_job(a.resume, ctx.mock)
            base = None
            print(f"ジョブ {job.id} を再開します({len(job.done_keys())}/{len(job.items)} 済み)",
                  file=sys.stderr)
        else:
            spec = load_spec(a.spec)
            if isinstance(spec.get("base"), dict) and "preset" in spec["base"]:
                print("ベースのプリセットを本体に読み込みます(音が切り替わります)。", file=sys.stderr)
            try:
                job, info = create(rig, spec, allow_large=ctx.yes)
            except QcsuiteError as exc:
                if "超えています" in str(exc) and ctx.ask(f"{exc}\n続けますか?"):
                    job, info = create(rig, spec, allow_large=True)
                else:
                    raise
            base = json.loads((job.dir / "base_grid.json").read_text())
            print(f"ジョブ {job.id}: {info['count']} 通り、見込み {info['estimate_s'] / 60:.1f} 分",
                  file=sys.stderr)
            for n in info["notes"]:
                print(f"注: {n}", file=sys.stderr)
        ctx.warn_monitor(rig.mock)
        summary = run(rig, job, base_grid=base, on_progress=_progress)
    ctx.out(summary, f"完了: {summary['ok']} 件(失敗 {summary.get('failed') or 0})。"
                     f"結果: qcsuite {'--mock ' if ctx.mock else ''}sweep results {job.id}")


def cmd_results(ctx, a):
    data = results(jobs.open_job(a.job, ctx.mock), a.sort, not a.hide_excluded)
    lines = [f"{data['title']}: {len(data['rows'])} 件(除外 {data['excluded']})  {data['note']}"]
    for r in data["rows"][: a.limit]:
        head = f"{r.get('score', ''):>5} " if data["has_reference"] else ""
        mark = " [除外] " + "; ".join(r["reasons"]) if r["excluded"] else ""
        lines.append(f"{head}{r['key']} LUFS {r.get('lufs')} flat {r.get('flatness')} "
                     f"crest {r.get('crest')}  {r['label']}{mark}")
    ctx.out(data, "\n".join(lines))


def cmd_to_tournament(ctx, a):
    keys = a.keys.split(",") if a.keys else None
    result = to_tournament(jobs.open_job(a.job, ctx.mock), ctx.mock, a.top, keys)
    t = result["tournament"]
    ctx.out(result, f"ブラインド比較 {t.get('id')} に {len(result['sent'])} 件を送りました(名前は伏せてあります)")


def cmd_sensitivity(ctx, a):
    base = "current"
    if a.base and a.base != "current":
        folder, slot = a.base.rsplit(":", 1)
        base = {"preset": {"folder": folder, "slot": slot}}
    spec = sensitivity_spec(a.row - 1, a.col - 1, a.param, a.steps, base, a.di, a.model, a.seconds)
    with ctx.rig("sweep sensitivity") as rig:
        job, info = create(rig, spec)
        base_grid = json.loads((job.dir / "base_grid.json").read_text())
        ctx.warn_monitor(rig.mock)
        run(rig, job, base_grid=base_grid, on_progress=_progress)
    data = sensitivity(job)
    png = Path(a.png) if a.png else sweeps_dir(ctx.mock) / job.id / "sensitivity.png"
    sensitivity_png(job, png)
    lines = [f"{data['param']} の効き方(ジョブ {job.id}、グラフ: {png})"]
    for p in data["points"]:
        lines.append(f"  {p['value']:.2f}: 傾き {p['tilt']:+.2f} 重心 {p['centroid']:.0f} Hz "
                     f"flat {p['flatness']:.3f} クレスト {p['crest']:.1f} LUFS {p['lufs']}")
    ctx.out(data, "\n".join(lines))


def add_cli(sub):
    p = sub.add_parser("sweep", help="総当たり(スイープ)と、ノブの効き方マップ")
    s = p.add_subparsers(dest="sweep_cmd", required=True)
    c = s.add_parser("preview", help="指定を展開して件数と見込み時間を表示(音は出さない)")
    c.add_argument("spec", help="スイープの指定(JSON ファイルかインライン)")
    c.set_defaults(fn=cmd_preview)
    c = s.add_parser("run", help="スイープを実行する(中断しても --resume で再開)")
    c.add_argument("spec", nargs="?", help="スイープの指定(JSON ファイルかインライン)")
    c.add_argument("--resume", metavar="JOB_ID")
    c.set_defaults(fn=lambda ctx, a: (_require(a.spec or a.resume, "spec か --resume"), cmd_run(ctx, a)))
    c = s.add_parser("results", help="結果を表示(並べ替え可)")
    c.add_argument("job")
    c.add_argument("--sort", default="key", choices=SORTS)
    c.add_argument("--limit", type=int, default=50)
    c.add_argument("--hide-excluded", action="store_true")
    c.set_defaults(fn=cmd_results)
    c = s.add_parser("to-tournament", help="上位(または指定)の候補をブラインド比較に送る")
    c.add_argument("job")
    c.add_argument("--top", type=int, default=8)
    c.add_argument("--keys", help="c0001,c0005 のように指定")
    c.set_defaults(fn=cmd_to_tournament)
    c = s.add_parser("sensitivity", help="1 つのノブを動かしたときの測定値の変化(効き方マップ)")
    c.add_argument("--row", type=int, required=True, help="行(1 始まり、本体の表示どおり)")
    c.add_argument("--col", type=int, required=True, help="列(1 始まり)")
    c.add_argument("--param", required=True, help="ノブの名前か別名(gain, treble など)")
    c.add_argument("--steps", type=int, default=7)
    c.add_argument("--base", default="current", help='"current" か FOLDER:SLOT(例 kami:1E)')
    c.add_argument("--model", help="そのセルに置くモデル(省略時は今のブロック)")
    c.add_argument("--di")
    c.add_argument("--seconds", type=float)
    c.add_argument("--png", help="グラフの書き出し先")
    c.set_defaults(fn=cmd_sensitivity)


def _require(value, what):
    if not value:
        raise QcsuiteError(f"{what} を指定してください")
