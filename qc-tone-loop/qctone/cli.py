"""``qctone``: the commands the tone loop is driven with."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import soundfile as sf

from qctone import device, features, session


def _print(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _summary(it: Path, result: dict):
    return {
        "iteration": it.name,
        "score": result["score"],
        "hints": result["hints"],
        "band_balance_diff_db": result["band_balance_diff_db"],
        "plot": str(it / "compare.png"),
        "best_so_far": (lambda b: b and {"score": b[0], "iteration": b[1].name})(
            session.best(it.parent.name)),
    }


def cmd_init(a):
    s = session.init(a.name, a.ref, a.desc, a.start, a.dur, a.di)
    _print({"session": str(s), "target": session.read_json(s / "target" / "features.json")})


def cmd_record_di(a):
    from qctone import audio_io
    if not session.path(a.name).exists():
        raise SystemExit(f"no session {a.name!r}: run `qctone init` first")
    out = session.path(a.name) / "di.wav"
    print(f"recording {a.seconds}s of dry Input 1 - play now", file=sys.stderr)
    _print({"di": str(out), **audio_io.record_dry(out, a.seconds)})


def cmd_devices(a):
    from qctone import audio_io
    print(audio_io.list_devices())


def cmd_grid(a):
    with device.connect() as qc:
        grid = device.snapshot(qc)
    if a.out:
        session.write_json(Path(a.out), grid)
    _print(grid)


def cmd_catalog(a):
    with device.connect() as qc:
        _print(device.catalog_listing(qc.catalog, a.search, a.category))


def _run_step(name, patch, note, render=True):
    from qctone import audio_io
    s = session.path(name)
    if not (s / "di.wav").exists():
        raise SystemExit(f"{s / 'di.wav'} is missing: run `qctone record-di {name}` or pass --di to init")
    with device.connect() as qc:
        before = device.snapshot(qc)
        if not (s / "baseline_grid.json").exists():
            session.write_json(s / "baseline_grid.json", before)
        ops = device.validate(patch, before, qc.catalog) if patch else []
        it = session.new_iteration(name)
        session.write_json(it / "patch.json", patch or {"note": note, "ops": []})
        session.write_json(it / "grid_before.json", before)
        device.apply(qc, ops)
        time.sleep(1.0)  # let the unit settle and its state reads catch up
        session.write_json(it / "grid_after.json", device.snapshot(qc))
    if not render:
        return it, None
    level = audio_io.reamp(s / "di.wav", it / "render.wav")
    result = session.evaluate(name, it, note or (patch or {}).get("note", ""))
    return it, {**_summary(it, result), "render_level": level}


def cmd_step(a):
    patch = json.loads(Path(a.patch).read_text()) if a.patch else None
    it, summary = _run_step(a.name, patch, a.note or "")
    _print(summary)


def cmd_import(a):
    it = session.new_iteration(a.name)
    data, sr = features.load(a.wav, a.start, a.dur)
    sf.write(str(it / "render.wav"), data, sr, subtype="FLOAT")
    if a.grid:
        shutil.copy(a.grid, it / "grid_after.json")
    _print(_summary(it, session.evaluate(a.name, it, a.note or f"imported {Path(a.wav).name}")))


def cmd_feedback(a):
    its = session.iterations(a.name)
    if not its:
        raise SystemExit("no iterations yet")
    target = its[-1] if a.iter is None else session.path(a.name) / f"iter_{a.iter:03d}"
    with open(target / "feedback.md", "a") as f:
        f.write(a.text.strip() + "\n")
    print(f"noted on {target.name}")


def cmd_restore(a):
    s = session.path(a.name)
    source = s / "baseline_grid.json" if a.iter is None else \
        s / f"iter_{a.iter:03d}" / "grid_after.json"
    saved = session.read_json(source)
    with device.connect() as qc:
        current = device.snapshot(qc)
        patch = device.restore_ops(saved, current)
        patch["note"] = f"restore {source.relative_to(s)}"
        ops = device.validate(patch, current, qc.catalog)
        device.apply(qc, ops)
    print(f"restored {source.relative_to(s)} ({len(ops)} ops); the grid is unsaved")


def cmd_save(a):
    with device.connect() as qc:
        stored = device.save(qc, a.slot, a.title)
    print(f"saved to User {a.slot} as {stored!r}")


def cmd_status(a):
    s = session.path(a.name)
    print((s / "target" / "description.md").read_text())
    print((s / "history.md").read_text())
    b = session.best(a.name)
    if b:
        print(f"best: {b[1].name} score {b[0]}")


def cmd_doctor(a):
    from qctone import doctor
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    checks = doctor.run(a.reamp_test)
    for c in checks:
        print(f"[{marks[c.status]}] {c.name}: {c.detail}")
        if c.fix:
            print(f"       fix: {c.fix}")
    if any(c.status == "fail" for c in checks):
        raise SystemExit(1)


def cmd_analyze(a):
    _print(features.analyze(a.wav, a.start, a.dur))


def main(argv=None):
    p = argparse.ArgumentParser(prog="qctone", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("init", help="start a session from a reference recording")
    c.add_argument("name")
    c.add_argument("--ref", required=True, help="reference WAV/AIFF/FLAC (ideally isolated guitar)")
    c.add_argument("--desc", required=True, help="the target tone in words")
    c.add_argument("--start", type=float, default=0.0, help="seconds into --ref to start")
    c.add_argument("--dur", type=float, help="seconds of --ref to use")
    c.add_argument("--di", help="existing dry DI file to reamp")
    c.set_defaults(fn=cmd_init)

    c = sub.add_parser("record-di", help="record the dry DI from QC Input 1 over USB")
    c.add_argument("name")
    c.add_argument("--seconds", type=float, default=20.0)
    c.set_defaults(fn=cmd_record_di)

    c = sub.add_parser("doctor", help="check USB control, routing and audio before a session")
    c.add_argument("--reamp-test", action="store_true",
                   help="also play a short test note through the grid and check it comes back")
    c.set_defaults(fn=cmd_doctor)

    c = sub.add_parser("devices", help="list audio devices")
    c.set_defaults(fn=cmd_devices)

    c = sub.add_parser("grid", help="dump the live grid as JSON")
    c.add_argument("--out")
    c.set_defaults(fn=cmd_grid)

    c = sub.add_parser("catalog", help="list block models and their parameters")
    c.add_argument("--search")
    c.add_argument("--category")
    c.set_defaults(fn=cmd_catalog)

    c = sub.add_parser("step", help="apply a patch, reamp, analyze (one iteration)")
    c.add_argument("name")
    c.add_argument("--patch", help="patch JSON; omit to just re-render the current grid")
    c.add_argument("--note")
    c.set_defaults(fn=cmd_step)

    c = sub.add_parser("import", help="analyze a WAV bounced elsewhere (e.g. Logic Pro) as an iteration")
    c.add_argument("name")
    c.add_argument("wav")
    c.add_argument("--start", type=float, default=0.0)
    c.add_argument("--dur", type=float)
    c.add_argument("--grid", help="grid JSON to attach, from `qctone grid --out`")
    c.add_argument("--note")
    c.set_defaults(fn=cmd_import)

    c = sub.add_parser("feedback", help="record what the player heard")
    c.add_argument("name")
    c.add_argument("text")
    c.add_argument("--iter", type=int)
    c.set_defaults(fn=cmd_feedback)

    c = sub.add_parser("restore", help="put the grid back to the baseline or an iteration")
    c.add_argument("name")
    c.add_argument("--iter", type=int, help="iteration number; omit for the baseline")
    c.set_defaults(fn=cmd_restore)

    c = sub.add_parser("save", help="save the live grid to a User slot (overwrites)")
    c.add_argument("name")
    c.add_argument("--slot", required=True, help='slot as shown on the unit, e.g. "30A"')
    c.add_argument("--title", required=True)
    c.set_defaults(fn=cmd_save)

    c = sub.add_parser("status", help="target and history of a session")
    c.add_argument("name")
    c.set_defaults(fn=cmd_status)

    c = sub.add_parser("analyze", help="print the features of any audio file")
    c.add_argument("wav")
    c.add_argument("--start", type=float, default=0.0)
    c.add_argument("--dur", type=float)
    c.set_defaults(fn=cmd_analyze)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except device.PatchError as exc:
        raise SystemExit(f"patch rejected, nothing was sent: {exc}") from None


if __name__ == "__main__":
    main()
