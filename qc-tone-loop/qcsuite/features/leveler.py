"""Loudness leveler (feature 3, idea L): same DI in, LUFS out, a correction proposal.

Presets that jump in level between songs are the classic live problem, and the
fix is mechanical: play the same DI through each preset, measure integrated
loudness (ITU-R BS.1770, :mod:`qcsuite.audio.lufs`), and move each preset's
level by the difference to a target. The player still decides the target and
listens to the result; the machine does the measuring and the arithmetic.

The flow:

1. **measure** - a resumable job. Each preset is recalled (the unit's sound
   changes), its guitar rows re-pointed at USB 5 (unsaved), and the DI
   reamped through it (:func:`qcsuite.audio.reamp.render_preset`). With
   ``scenes``, each scene is switched to and rendered as well (idea L). Renders
   that are the DI coming back, or presets holding blocks known not to sound
   from the host, are recorded but left out of the target.
2. **propose** - target = the player's number or the median of the measured
   presets; correction = target - measured, by one of three methods:

   * ``lane`` (default): the VOLUME of every row that reaches Out 1/2
     (-39.5..+12 dB, clamped and flagged). Whether a lane VOLUME written from
     the computer changes the sound is NOT known yet - it might read back and
     do nothing, like captures and mics. :func:`check_lane_volume_effect`
     tests exactly that and is the second item of the hardware test plan.
   * ``gain_block``: a Utility "Gain" block placed in a free cell after the
     last non-linear block of each output row, its GAIN set to the correction
     in dB. A plain numeric parameter, so it is expected to sound - still
     unverified on hardware. Needs a free cell and DSP room.
   * ``amp_level``: the amp's MASTER / VOLUME knob. Approximate (the dB law of
     those knobs is not published; 24 dB over the knob's travel is assumed)
     and it changes the tone of most amps (power-amp drive) - verify by ear.

3. **apply** - ONLY to copies: a plan of ``copy`` steps into the sandbox (or an
   allowed folder) whose ``adjust`` carries the correction. The source presets
   are never written. Scene-to-scene differences cannot be fixed with one value
   and are reported as advice for the unit's screen.
4. **verify** - re-render the copies and report the residual against the target.
   The goal is +-1 LU. On the fake unit a level change moves loudness by exactly
   that many dB; on hardware it has not been measured yet (hardware test plan).
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pyquadcortex as pq

from qcsuite import jobs, paths
from qcsuite import plan as plan_mod
from qcsuite.audio import di as di_mod
from qcsuite.audio import reamp
from qcsuite.device import guard
from qcsuite.errors import PlanError
from qctone import features

LANE_MIN_DB = -39.5        # the lowest numeric step; below it the knob reads "Off"
LANE_MAX_DB = 12.0
GOAL_LU = 1.0
#: How close a lane VOLUME change must come to the written delta to count as
#: effective in :func:`check_lane_volume_effect`.
EFFECT_TOLERANCE_DB = 1.5
SCENES = "ABCDEFGH"
MEASURE_KIND, VERIFY_KIND = "level-measure", "level-verify"
METHODS = ("lane", "gain_block", "amp_level")
METHOD_LABELS = {"lane": "行の出力レベル(Lane VOLUME)", "gain_block": "Gain ブロックを追加",
                 "amp_level": "アンプの MASTER / VOLUME"}
GAIN_BLOCK_ID = 16005      # pyquadcortex.models.Utility.GAIN
#: Blocks whose output level changes their sound (a gain after them is not the
#: same as a gain before them).
NONLINEAR = ("Guitar Amplifier", "Bass Amplifier", "Guitar Overdrive", "Bass Overdrive",
             "Compressor", "Neural Capture", "Neural Capture V2", "Utility")
AMP_CATEGORIES = ("Guitar Amplifier", "Bass Amplifier")
AMP_LEVEL_KNOBS = ("MASTER", "VOLUME")
#: Guess: dB per full travel of an amp's MASTER/VOLUME knob. Not published for
#: any model; only good for a first try that verify then checks.
AMP_KNOB_SPAN_DB = 24.0
REACH = ("XLR_1_2", "XLR_1", "XLR_2", "MULTIPLE")


# -- inputs ----------------------------------------------------------------------

def parse_preset(spec: str) -> dict:
    """``"kami:1E"`` / ``"My Presets:3"`` -> ``{"folder": key, "position": int}``."""
    if ":" not in spec:
        raise ValueError(f"プリセットは「フォルダ:スロット」で指定してください(例 kami:1E): {spec}")
    folder, slot = spec.rsplit(":", 1)
    slot = slot.strip()
    position = int(slot) if slot.isdigit() else pq.slot_to_position(slot)
    return {"folder": guard.folder_key(folder.strip()), "position": position}


def parse_scenes(value) -> list[int] | str | None:
    """``"labelled"``, ``"all"``, ``"A,C"`` / ``[0, 2]``, or None."""
    if value in (None, "", "none"):
        return None
    if value in ("labelled", "all"):
        return value
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    out = []
    for v in value:
        if isinstance(v, int):
            out.append(v)
        elif v.upper() in SCENES:
            out.append(SCENES.index(v.upper()))
        else:
            raise ValueError(f"シーンは A〜H で指定してください: {v}")
    return sorted(set(out))


def expand(device, presets: list[dict] = (), folders: list[str] = ()) -> list[dict]:
    """The preset list to measure, with names, from explicit entries and folders."""
    items, seen = [], set()
    listings: dict[str, dict[int, str]] = {}

    def name_of(key, position):
        if key not in listings:
            listings[key] = {p["index"]: p["name"] for p in device.presets(key)}
        return listings[key].get(position)

    for folder in folders:
        key = guard.folder_key(folder)
        for p in device.presets(key):
            if (key, p["index"]) not in seen:
                seen.add((key, p["index"]))
                items.append({"folder": key, "position": p["index"], "name": p["name"]})
    for p in presets:
        key = guard.folder_key(p["folder"])
        position = int(p["position"])
        name = name_of(key, position)
        if name is None:
            raise ValueError(f"「{guard.folder_name(key)}」の {pq.position_to_slot(position)} は空です。")
        if (key, position) not in seen:
            seen.add((key, position))
            items.append({"folder": key, "position": position, "name": name})
    if not items:
        raise ValueError("測るプリセットを 1 つ以上選んでください。")
    for item in items:
        item["key"] = f"{item['folder']}#{item['position']}"
    return items


def load_di(name: str | None, mock: bool):
    """A DI from the library; the synthetic riff when none is named."""
    di_name = name or di_mod.SYNTH_NAME
    audio, sr = di_mod.load(di_name, mock)
    return di_name, audio, sr


# -- does the lane VOLUME sound? ----------------------------------------------------

def check_lane_volume_effect(rig, di, folder: str | None = None, position: int | None = None,
                             delta_db: float = -6.0, sr: int = 48000) -> dict:
    """Write lane VOLUME = current + ``delta_db`` (live grid, unsaved) and listen.

    Renders before and after, compares loudness, and puts the original values
    back. ``effective`` means the loudness moved within +-1.5 dB of the change
    actually written; ``no_effect`` that it did not move (writable but inert).
    With ``folder``/``position`` that preset is recalled first; otherwise the
    live grid is used as it is (re-pointed to USB 5 if no row is fed from it).
    ``di`` is a DI library name or a mono array at ``sr``.
    """
    device = rig.device
    if isinstance(di, str):
        _, di, sr = load_di(di, rig.mock)
    if folder is not None:
        device.read_stored(guard.folder_key(folder), int(position))
        device.prepare_reamp()
    else:
        p = device.current_preset()
        usb = {int(pq.Input.USB_5), int(pq.Input.USB_5_6)}
        if not any(c.in_portid in usb for c in p.chains):
            device.prepare_reamp(p)
    preset = device.current_preset()
    rows = device.output_rows(preset)
    if not rows:
        raise RuntimeError("Out 1/2 に届く行がないため確かめられません。")
    original = {row: device.lane_volume_db(row, preset) for row in rows}
    before = reamp.render_current(rig, di, sr)
    written = {}
    try:
        for row, db in original.items():
            new = min(LANE_MAX_DB, max(LANE_MIN_DB, db + delta_db))
            written[row] = new - db
            device.set_lane_volume_db(row, new)
        readback = {row: round(device.lane_volume_db(row), 2) for row in rows}
        after = reamp.render_current(rig, di, sr)
    finally:
        for row, db in original.items():
            device.set_lane_volume_db(row, db)
    applied = float(np.mean(list(written.values())))
    lufs_before, lufs_after = before["loudness"]["lufs"], after["loudness"]["lufs"]
    if lufs_before is None or lufs_after is None:
        verdict, effective, no_effect, change = "判断できない(無音)", False, False, None
    else:
        change = round(lufs_after - lufs_before, 2)
        effective = abs(change - applied) <= EFFECT_TOLERANCE_DB
        no_effect = abs(change) < 0.5
        verdict = ("効く" if effective else
                   "効かない(書き込みは読み戻せるが音は変わらない)" if no_effect else
                   "一部だけ効く(要確認)")
    return {"delta_db": round(applied, 2), "rows": {str(r): round(original[r], 2) for r in rows},
            "readback_db": {str(r): v for r, v in readback.items()},
            "lufs_before": lufs_before, "lufs_after": lufs_after, "measured_change_db": change,
            "effective": bool(effective), "no_effect": bool(no_effect), "verdict": verdict,
            "restored": True, "mock": rig.mock,
            "recommended_method": "lane" if effective else "gain_block"}


# -- measuring ---------------------------------------------------------------------

def _layout(normalized: dict) -> dict:
    """What a correction needs to know about the preset's output rows."""
    rows = {}
    for r in normalized["rows"]:
        if r["out_port"] not in REACH:
            continue
        blocks = []
        for b in normalized["blocks"]:
            if b["row"] != r["row"]:
                continue
            entry = {"col": b["col"], "model_id": b["model_id"], "model": b["model"],
                     "category": b["category"]}
            if b["category"] in AMP_CATEGORIES:
                entry["knobs"] = {k: b["params"][k].get("value") for k in AMP_LEVEL_KNOBS
                                  if k in b["params"]}
            blocks.append(entry)
        vol = r["lane"].get("VOLUME") or {}
        rows[str(r["row"])] = {"lane_db": vol.get("db"), "blocks": blocks}
    return rows


def measure_preset(rig, item: dict, audio, sr: int, di_features: dict,
                   scenes=None, render_dir: Path | None = None) -> dict:
    """Render one preset (and optionally its scenes) and measure loudness."""
    out_path = None
    if render_dir is not None:
        out_path = render_dir / f"{guard.folder_name(item['folder'])}_{item['position']:03d}.flac"
    info = reamp.render_preset(rig, item["folder"], item["position"], audio, sr,
                               di_features, out_path=out_path)
    normalized = info["preset"]
    layout = _layout(normalized)
    result = {
        "folder": item["folder"], "position": item["position"],
        "slot": pq.position_to_slot(item["position"]), "name": normalized["name"],
        "lufs": info["loudness"]["lufs"], "true_peak_dbtp": info["loudness"]["true_peak_dbtp"],
        "excluded": info["excluded"]["excluded"], "reasons": info["excluded"]["reasons"],
        "default_scene": normalized["default_scene"],
        "lanes": {row: v["lane_db"] for row, v in layout.items()}, "layout": layout,
        "scenes": {}, "file": info.get("file"),
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"), "mock": rig.mock,
    }
    if scenes:
        labels = normalized["scene_labels"]
        if scenes == "labelled":
            wanted = [i for i, label in enumerate(labels) if label]
        elif scenes == "all":
            wanted = list(range(8))
        else:
            wanted = list(scenes)
        for s in wanted:
            rig.device.switch_scene(s)
            scene_info = reamp.render_current(rig, audio, sr, di_features)
            result["scenes"][SCENES[s]] = {
                "label": labels[s] if s < len(labels) else "",
                "lufs": scene_info["loudness"]["lufs"],
                "true_peak_dbtp": scene_info["loudness"]["true_peak_dbtp"],
                "passthrough": scene_info["passthrough"]["passthrough"],
            }
    return result


def start_measure(device, *, presets=(), folders=(), di: str | None = None, scenes=None,
                  mock: bool, title: str = "") -> jobs.Job:
    """Create (not run) a measuring job."""
    items = expand(device, presets, folders)
    scenes = parse_scenes(scenes)
    di_name = di or di_mod.SYNTH_NAME
    return jobs.create(MEASURE_KIND, {"di": di_name, "scenes": scenes}, items, mock,
                       title=title or f"音量測定({len(items)} 件)")


def measure_work(rig, job: jobs.Job):
    """The per-item function for a measuring job (shared by CLI and web)."""
    spec = job.spec
    _, audio, sr = load_di(spec["di"], rig.mock)
    di_features = features.analyze_array(audio[:, None], sr)
    render_dir = paths.ensure(job.dir / "renders")

    def work(item):
        return measure_preset(rig, item, audio, sr, di_features, spec.get("scenes"), render_dir)
    return work


def results(job_id: str, mock: bool) -> list[dict]:
    job = jobs.open_job(job_id, mock)
    return [r["result"] for r in job.results() if r.get("ok")]


def jobs_of(kind: str, mock: bool) -> list[jobs.Job]:
    out = []
    for folder in sorted(jobs.jobs_dir(mock).iterdir()):
        spec = folder / "spec.json"
        if spec.exists() and json.loads(spec.read_text()).get("kind") == kind:
            out.append(jobs.Job(folder))
    return out


def latest_job(mock: bool, kind: str = MEASURE_KIND) -> str | None:
    found = jobs_of(kind, mock)
    return found[-1].id if found else None


def latest_measurements(mock: bool) -> dict:
    """``(folder_key, position) -> latest measurement`` across all measuring runs.

    For the health / gig-check features. Each value carries ``name``, ``lufs``,
    ``true_peak_dbtp``, ``scenes`` and ``measured_at`` (plus ``excluded``).
    Verification runs count too: they measure the sandbox copies.
    """
    latest: dict = {}
    for kind in (MEASURE_KIND, VERIFY_KIND):
        for job in jobs_of(kind, mock):
            for r in job.results():
                if not r.get("ok"):
                    continue
                m = r["result"]
                key = (m["folder"], m["position"])
                if key not in latest or m["measured_at"] >= latest[key]["measured_at"]:
                    latest[key] = m
    return latest


# -- proposal ------------------------------------------------------------------------

def _lane_correction(m: dict, delta: float, row: dict) -> float:
    """Fill ``row["lanes"]``; return the part of ``delta`` the lanes cannot carry."""
    residual = 0.0
    for lane, current in m["lanes"].items():
        current = 0.0 if current is None else current
        wanted = current + delta
        new = min(LANE_MAX_DB, max(LANE_MIN_DB, wanted))
        row["lanes"][lane] = {"current_db": round(current, 2), "new_db": round(new, 2)}
        if new != wanted:
            residual = max(residual, abs(wanted - new))
            row["notes"].append(
                f"行{int(lane) + 1} の出力は {LANE_MIN_DB}〜+{LANE_MAX_DB:g} dB の範囲外になるため "
                f"{new:+.1f} dB で頭打ち(残り {wanted - new:+.1f} dB)")
    return residual


def _gain_block_correction(m: dict, delta: float, row: dict,
                           gain_range=(-24.0, 24.0)) -> float:
    residual = 0.0
    for lane, info in (m.get("layout") or {}).items():
        blocks = info["blocks"]
        last_nonlinear = max((b["col"] for b in blocks if b["category"] in NONLINEAR), default=-1)
        used = {b["col"] for b in blocks}
        free = [c for c in range(last_nonlinear + 1, 8) if c not in used]
        if not free:
            row["notes"].append(f"行{int(lane) + 1}: 最後のアンプ/歪み/コンプより後ろに空きセルがないため、"
                                "Gain ブロックを置けません")
            residual = max(residual, abs(delta))
            continue
        db = min(gain_range[1], max(gain_range[0], delta))
        if db != delta:
            residual = max(residual, abs(delta - db))
            row["notes"].append(f"行{int(lane) + 1}: Gain ブロックの範囲を超えるため {db:+.1f} dB で頭打ち")
        row["gain_blocks"][lane] = {"col": free[0], "db": round(db, 2)}
    if row["gain_blocks"]:
        row["notes"].append("Gain ブロックの追加で DSP が足りない場合、本体が置くのを拒否します(実行時に分かります)")
    return residual


def _amp_level_correction(m: dict, delta: float, row: dict) -> float:
    residual = 0.0
    found = False
    for lane, info in (m.get("layout") or {}).items():
        amps = [b for b in info["blocks"] if b["category"] in AMP_CATEGORIES and b.get("knobs")]
        if not amps:
            continue
        amp = amps[-1]
        knob, value = next(iter(amp["knobs"].items()))
        if value is None:
            continue
        found = True
        wanted = value + delta / AMP_KNOB_SPAN_DB
        new = min(1.0, max(0.0, wanted))
        if new != wanted:
            residual = max(residual, abs(wanted - new) * AMP_KNOB_SPAN_DB)
        row["knobs"].append({"row": int(lane), "col": amp["col"], "model_id": amp["model_id"],
                             "model": amp["model"], "param": knob,
                             "current": round(value, 4), "new": round(new, 4)})
    if not found:
        row["notes"].append("出力に届く行に MASTER / VOLUME のあるアンプがありません")
        return abs(delta)
    row["notes"].append(f"アンプの {'/'.join(AMP_LEVEL_KNOBS)} は音色も変わります。換算は推測"
                        f"(つまみ全体で約 {AMP_KNOB_SPAN_DB:g} dB と仮定)なので、確認で調整してください")
    return residual


def propose(measurements: list[dict], target: float | None = None,
            method: str = "lane") -> dict:
    """Corrections toward ``target`` (default: the median of usable presets)."""
    if method not in METHODS:
        raise ValueError(f"method は {', '.join(METHODS)} のどれかです: {method}")
    usable = [m for m in measurements if not m.get("excluded") and m.get("lufs") is not None]
    if target is None:
        if not usable:
            raise ValueError("目標にできる測定結果がありません(すべて除外されたか無音です)。")
        target = round(statistics.median(m["lufs"] for m in usable), 1)
        source = "中央値"
    else:
        source = "指定"
    rows = []
    for m in measurements:
        row = {"folder": m["folder"], "position": m["position"], "slot": m["slot"],
               "name": m["name"], "lufs": m.get("lufs"), "delta_db": None,
               "lanes": {}, "gain_blocks": {}, "knobs": [], "notes": [], "correctable": False}
        if m.get("excluded"):
            row["notes"].append("比較から除外: " + " / ".join(m.get("reasons", [])))
        elif m.get("lufs") is None:
            row["notes"].append("無音のため測れませんでした")
        elif not m.get("lanes"):
            row["notes"].append("Out 1/2 に届く行がないため補正できません")
        else:
            delta = round(target - m["lufs"], 2)
            row["delta_db"] = delta
            if method == "lane":
                residual = _lane_correction(m, delta, row)
            elif method == "gain_block":
                residual = _gain_block_correction(m, delta, row)
            else:
                residual = _amp_level_correction(m, delta, row)
            row["correctable"] = residual < GOAL_LU and bool(
                row["lanes"] or row["gain_blocks"] or row["knobs"])
            tp = m.get("true_peak_dbtp")
            if tp is not None and tp + delta > -0.5:
                row["notes"].append(f"補正後のピークが約 {tp + delta:+.1f} dBTP になり、"
                                    "クリップの恐れがあります")
        scenes = {k: v for k, v in (m.get("scenes") or {}).items() if v.get("lufs") is not None}
        if len(scenes) > 1:
            spread = max(v["lufs"] for v in scenes.values()) - min(v["lufs"] for v in scenes.values())
            if spread > GOAL_LU:
                loud = max(scenes, key=lambda k: scenes[k]["lufs"])
                quiet = min(scenes, key=lambda k: scenes[k]["lufs"])
                row["notes"].append(
                    f"シーン間で {spread:.1f} LU の差(最大 {loud}、最小 {quiet})。1 つの値では"
                    "そろわないので、本体の画面でシーンごとに調整してください")
        rows.append(row)
    return {"target": target, "target_source": source, "method": method,
            "method_label": METHOD_LABELS[method], "goal_lu": GOAL_LU, "rows": rows}


def proposal_text(p: dict) -> str:
    lines = [f"目標: {p['target']:.1f} LUFS({p['target_source']})  方法: {p['method_label']}",
             f"{'プリセット':<26} {'測定':>7} {'補正':>7}  変更 / メモ"]
    for r in p["rows"]:
        lufs = "—" if r["lufs"] is None else f"{r['lufs']:.1f}"
        delta = "—" if r["delta_db"] is None else f"{r['delta_db']:+.1f}"
        changes = [f"行{int(k) + 1} 出力 {v['current_db']:+.1f}→{v['new_db']:+.1f} dB"
                   for k, v in r["lanes"].items()]
        changes += [f"行{int(k) + 1} 列{v['col'] + 1} に Gain {v['db']:+.1f} dB"
                    for k, v in r["gain_blocks"].items()]
        changes += [f"{k['model']} {k['param']} {k['current'] * 10:.1f}→{k['new'] * 10:.1f}"
                    for k in r["knobs"]]
        label = f"{guard.folder_name(r['folder'])} {r['slot']} {r['name']}"[:26]
        lines.append(f"{label:<26} {lufs:>7} {delta:>7}  {', '.join(changes)}")
        lines.extend(f"{'':<42}* {n}" for n in r["notes"])
    return "\n".join(lines)


# -- applying to copies ------------------------------------------------------------------

def apply_adjust(device, adjust: dict | None):
    """Edit the live grid as a copy step's ``adjust`` says (before it is saved).

    The shared :meth:`~qcsuite.device.core.Device.apply_adjust` understands
    ``lane_db``, ``gain_block`` and ``knobs`` (and refuses unknown keys), so any
    plan runner - this page, ``qcsuite level apply`` or ``qcsuite plan --run`` -
    levels the copies the same way.
    """
    device.apply_adjust(adjust)


def execute(plan: plan_mod.Plan, device, *, confirm: bool, on_progress=None) -> dict:
    return plan_mod.execute(plan, device, confirm=confirm, on_progress=on_progress)


def sessions_dir(mock: bool) -> Path:
    return paths.sub("leveler", mock)


def _gain_model(device):
    """The Utility Gain block and its GAIN range, from the unit's catalog."""
    catalog = device.catalog
    model = catalog.get(GAIN_BLOCK_ID)
    if model is None or model.category != "Utility":
        model = next((m for m in catalog if m.category == "Utility"
                      and m.name.lower() == "gain"), None)
    if model is None:
        raise PlanError("カタログに Utility の Gain ブロックが見つかりません。")
    spec = model.parameter("GAIN")
    if spec.range_is_placeholder:
        raise PlanError("Gain ブロックの GAIN に dB の範囲がないため、この方法は使えません。")
    return model, spec


def build_plan(device, proposal: dict, *, to: str | None = None, di: str | None = None,
               measure_job: str | None = None) -> plan_mod.Plan:
    """Copies of every correctable preset, with the correction, into ``to``.

    ``to`` defaults to the sandbox. The plan's targets are resolved now, and a
    session record is written so :func:`start_verify` knows what to re-measure.
    """
    method = proposal.get("method", "lane")
    if di is None and measure_job is not None:
        di = jobs.open_job(measure_job, device.mock).spec.get("di")
    key = guard.folder_key(to) if to else device.ensure_sandbox()
    device.policy.check(key, "音量をそろえたコピーの保存")
    gain_model = gain_spec = None
    if method == "gain_block":
        gain_model, gain_spec = _gain_model(device)
    taken = {p["index"] for p in device.presets(key)}
    free = (i for i in range(256) if i not in taken)
    steps, entries = [], []
    for r in proposal["rows"]:
        if r["delta_db"] is None or not (r["lanes"] or r["gain_blocks"] or r["knobs"]):
            continue
        adjust = {}
        if r["lanes"]:
            adjust["lane_db"] = {lane: v["new_db"] for lane, v in r["lanes"].items()}
        if r["gain_blocks"]:
            adjust["gain_block"] = {
                lane: {"col": v["col"], "model_id": gain_model.id, "param": gain_spec.name,
                       "db": min(gain_spec.maximum, max(gain_spec.minimum, v["db"]))}
                for lane, v in r["gain_blocks"].items()}
        if r["knobs"]:
            adjust["knobs"] = [{k: v for k, v in knob.items() if k != "model"}
                               for knob in r["knobs"]]
        try:
            position = next(free)
        except StopIteration:
            raise ValueError(f"「{guard.folder_name(key)}」に空きスロットが足りません。") from None
        steps.append(plan_mod.Step(
            "copy", key, position=position, name=r["name"],
            source={"folder": r["folder"], "position": r["position"]}, adjust=adjust,
            note=f"{METHOD_LABELS[method]} で {r['delta_db']:+.1f} dB"))
        entries.append({"source": {"folder": r["folder"], "position": r["position"]},
                        "dest": {"folder": key, "position": position}, "name": r["name"],
                        "measured_lufs": r["lufs"], "delta_db": r["delta_db"]})
    if not steps:
        raise ValueError("補正できるプリセットがありません。")
    warnings = ["元のプリセットは変更しません。コピー先にだけ補正を書き込みます。",
                "コピーではタグが引き継がれず、同じ名前があると本体が名前を変えることがあります。"]
    if method == "amp_level":
        warnings.append("アンプの MASTER/VOLUME で補正するので、音色も変わる可能性があります。")
    plan = plan_mod.Plan(f"音量をそろえたコピー(目標 {proposal['target']:.1f} LUFS、"
                         f"{METHOD_LABELS[method]})", steps=steps, warnings=warnings)
    record = {"plan_id": plan.id, "target": proposal["target"], "method": method, "di": di,
              "measure_job": measure_job, "dest": key, "entries": entries,
              "created": plan.created}
    (sessions_dir(device.mock) / f"{plan.id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return plan


def load_session(plan_id: str | None, mock: bool) -> dict:
    files = sorted(sessions_dir(mock).glob("*.json"))
    if plan_id is None:
        if not files:
            raise FileNotFoundError("適用した音量そろえの記録がありません(先に level apply)。")
        return json.loads(files[-1].read_text())
    path = sessions_dir(mock) / f"{plan_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"音量そろえの記録 {plan_id} がありません。")
    return json.loads(path.read_text())


def start_verify(session: dict, *, di: str | None = None, mock: bool) -> jobs.Job:
    items = [{"key": f"{e['dest']['folder']}#{e['dest']['position']}",
              "folder": e["dest"]["folder"], "position": e["dest"]["position"],
              "name": e["name"]} for e in session["entries"]]
    return jobs.create(VERIFY_KIND, {"di": di or session.get("di") or di_mod.SYNTH_NAME,
                                     "scenes": None, "target": session["target"],
                                     "session": session["plan_id"]},
                       items, mock, title=f"音量そろえの確認({len(items)} 件)")


def verify_summary(job_id: str, mock: bool) -> dict:
    job = jobs.open_job(job_id, mock)
    target = job.spec["target"]
    rows = []
    for m in results(job_id, mock):
        residual = None if m["lufs"] is None else round(m["lufs"] - target, 2)
        rows.append({"folder": m["folder"], "slot": m["slot"], "name": m["name"],
                     "lufs": m["lufs"], "residual_lu": residual,
                     "within_goal": residual is not None and abs(residual) <= GOAL_LU,
                     "excluded": m["excluded"]})
    worst = max((abs(r["residual_lu"]) for r in rows if r["residual_lu"] is not None), default=None)
    return {"target": target, "goal_lu": GOAL_LU, "rows": rows,
            "within_goal": sum(r["within_goal"] for r in rows), "count": len(rows),
            "worst_abs_lu": worst, "mock": mock}


# -- CLI ---------------------------------------------------------------------------------

def _measure_text(rows: list[dict]) -> str:
    lines = [f"{'プリセット':<30} {'LUFS':>7} {'TP dBTP':>8}  メモ"]
    for m in rows:
        label = f"{guard.folder_name(m['folder'])} {m['slot']} {m['name']}"[:30]
        lufs = "—" if m["lufs"] is None else f"{m['lufs']:.1f}"
        tp = "—" if m["true_peak_dbtp"] is None else f"{m['true_peak_dbtp']:.1f}"
        note = "除外: " + " / ".join(m["reasons"]) if m["excluded"] else ""
        lines.append(f"{label:<30} {lufs:>7} {tp:>8}  {note}")
        for s, v in m.get("scenes", {}).items():
            lines.append(f"{'':<4}シーン {s} {v['label'] or ''}: "
                         f"{'—' if v['lufs'] is None else format(v['lufs'], '.1f')} LUFS")
    return "\n".join(lines)


def _run_job(ctx, rig, job: jobs.Job):
    def progress(done, total, key):
        print(f"  [{done + 1}/{total}] {key}", file=sys.stderr)
    return jobs.run(job, measure_work(rig, job), on_progress=progress)


def cmd_measure(ctx, a):
    with ctx.rig("level measure") as rig:
        if a.resume:
            job = jobs.open_job(a.resume, ctx.mock)
        else:
            presets = [parse_preset(p) for p in a.preset]
            job = start_measure(rig.device, presets=presets, folders=a.folder, di=a.di,
                                scenes=a.scenes, mock=ctx.mock)
        print(f"ジョブ {job.id}: {len(job.items)} 件のプリセットを順に読み込みます"
              "(本体の音が切り替わります)。", file=sys.stderr)
        ctx.warn_monitor(rig.mock)
        summary = _run_job(ctx, rig, job)
    rows = results(job.id, ctx.mock)
    ctx.out({"job": summary, "results": rows}, _measure_text(rows) + f"\n(ジョブ {job.id})")


def _proposal_for(ctx, a) -> tuple[dict, str]:
    job_id = a.job or latest_job(ctx.mock)
    if job_id is None:
        raise SystemExit("測定結果がありません。先に `qcsuite level measure` を実行してください。")
    return propose(results(job_id, ctx.mock), a.target, a.method), job_id


def cmd_propose(ctx, a):
    proposal, job_id = _proposal_for(ctx, a)
    ctx.out({"job": job_id, **proposal}, proposal_text(proposal))


def cmd_apply(ctx, a):
    proposal, job_id = _proposal_for(ctx, a)
    print(proposal_text(proposal), file=sys.stderr)
    with ctx.rig("level apply") as rig:
        di = jobs.open_job(job_id, ctx.mock).spec.get("di")
        plan = build_plan(rig.device, proposal, to=a.to, di=di, measure_job=job_id)
        result = ctx.run_plan(plan, rig.device, apply=a.apply)
    text = (f"計画 {plan.id} を実行しました。確認: qcsuite level verify --session {plan.id}"
            if not result.get("dry_run") else f"(dry-run)計画 {plan.id}")
    ctx.out({"plan": plan.id, "dry_run": result.get("dry_run", True)}, text)


def cmd_verify(ctx, a):
    with ctx.rig("level verify") as rig:
        if a.resume:
            job = jobs.open_job(a.resume, ctx.mock)
        else:
            job = start_verify(load_session(a.session, ctx.mock), di=a.di, mock=ctx.mock)
        ctx.warn_monitor(rig.mock)
        _run_job(ctx, rig, job)
    summary = verify_summary(job.id, ctx.mock)
    lines = [f"目標 {summary['target']:.1f} LUFS、±{GOAL_LU:g} LU 以内: "
             f"{summary['within_goal']}/{summary['count']}"
             + ("(モックでの結果。実機は未確認)" if ctx.mock else "")]
    for r in summary["rows"]:
        res = "—" if r["residual_lu"] is None else f"{r['residual_lu']:+.2f} LU"
        lines.append(f"  {r['slot']} {r['name']:<24} {res} {'OK' if r['within_goal'] else '要確認'}")
    ctx.out(summary, "\n".join(lines))


def cmd_check_lane(ctx, a):
    with ctx.rig("level check-lane") as rig:
        ctx.warn_monitor(rig.mock)
        target = parse_preset(a.preset) if a.preset else {"folder": None, "position": None}
        result = check_lane_volume_effect(rig, a.di or di_mod.SYNTH_NAME, target["folder"],
                                          target["position"], a.delta)
    text = (f"行の出力レベルを {result['delta_db']:+.1f} dB 書き込み → 音量の変化 "
            f"{'—' if result['measured_change_db'] is None else format(result['measured_change_db'], '+.2f')} dB: "
            f"{result['verdict']}(元の値に戻しました)。推奨する方法: {result['recommended_method']}"
            + ("(モック)" if rig.mock else ""))
    ctx.out(result, text)


def add_cli(sub):
    p = sub.add_parser("level", help="音量そろえ(同じ DI で LUFS を測り、補正案を出す)")
    s = p.add_subparsers(dest="level_cmd", required=True)
    c = s.add_parser("measure", help="プリセットを順に読み込み、同じ DI で鳴らして LUFS を測る")
    c.add_argument("--preset", action="append", default=[], metavar="FOLDER:SLOT",
                   help="例: kami:1E、'My Presets:3A'。複数可")
    c.add_argument("--folder", action="append", default=[], help="フォルダ内のすべてを測る。複数可")
    c.add_argument("--di", help="DI ライブラリの名前(既定: 合成 DI)")
    c.add_argument("--scenes", help="labelled(名前のあるシーン)/ all / A,C などシーンごとにも測る")
    c.add_argument("--resume", metavar="JOB_ID", help="中断した測定を再開する")
    c.set_defaults(fn=cmd_measure)
    for name, help_text in (("propose", "補正案を表示する(何も書き込まない)"),
                            ("apply", "補正したコピーを sandbox に作る計画(--apply で実行)")):
        c = s.add_parser(name, help=help_text)
        c.add_argument("--job", help="測定ジョブ ID(既定: 最新)")
        c.add_argument("--target", type=float, help="目標 LUFS(既定: 測定値の中央値)")
        c.add_argument("--method", choices=METHODS, default="lane",
                       help="lane: 行の出力レベル(既定)/ gain_block: Gain ブロックを追加 / "
                            "amp_level: アンプの MASTER・VOLUME(音色も変わる)")
        if name == "apply":
            c.add_argument("--to", help="コピー先フォルダ(既定: sandbox。他は --allow-folder が必要)")
            c.add_argument("--apply", action="store_true")
            c.set_defaults(fn=cmd_apply)
        else:
            c.set_defaults(fn=cmd_propose)
    c = s.add_parser("verify", help="コピーをもう一度測り、目標との差を確かめる")
    c.add_argument("--session", help="apply の計画 ID(既定: 最新)")
    c.add_argument("--di")
    c.add_argument("--resume", metavar="JOB_ID")
    c.set_defaults(fn=cmd_verify)
    c = s.add_parser("check-lane", help="行の出力レベルを書き込んで、音量が本当に変わるか確かめる"
                                        "(保存しない・元に戻す)")
    c.add_argument("--preset", metavar="FOLDER:SLOT", help="読み込んで試すプリセット(既定: 今のグリッド)")
    c.add_argument("--di")
    c.add_argument("--delta", type=float, default=-6.0, help="書き込む変化量 dB(既定 -6)")
    c.set_defaults(fn=cmd_check_lane)
