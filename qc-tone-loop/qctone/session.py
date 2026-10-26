"""Session folders: one target, a DI, and numbered iterations.

    sessions/<name>/
      target/reference.wav, description.md, features.json
      di.wav
      baseline_grid.json          grid when the session started
      iter_001/
        patch.json  grid_before.json  grid_after.json
        render.wav  features.json  compare.json  compare.png
        feedback.md               what the player heard (optional)
      history.md                  one line per iteration
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import soundfile as sf

from qctone import compare as cmp_mod
from qctone import features, plot

ROOT = Path(os.environ.get("QCTONE_SESSIONS",
                           Path(__file__).resolve().parent.parent / "sessions"))


def path(name: str) -> Path:
    return ROOT / name


def write_json(p: Path, data):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(p: Path):
    return json.loads(p.read_text())


def init(name: str, reference, description: str, start=0.0, duration=None, di=None):
    s = path(name)
    if s.exists():
        raise SystemExit(f"session {name!r} already exists at {s}")
    (s / "target").mkdir(parents=True)
    data, sr = features.load(reference, start, duration)
    sf.write(str(s / "target" / "reference.wav"), data, sr, subtype="FLOAT")
    (s / "target" / "description.md").write_text(description.strip() + "\n")
    write_json(s / "target" / "features.json", features.analyze_array(data, sr))
    if di:
        shutil.copy(di, s / "di.wav")
    (s / "history.md").write_text(
        "| iter | score | note |\n|---:|---:|---|\n")
    return s


def iterations(name: str) -> list[Path]:
    return sorted(path(name).glob("iter_[0-9][0-9][0-9]"))


def new_iteration(name: str) -> Path:
    existing = iterations(name)
    number = int(existing[-1].name[5:]) + 1 if existing else 1
    it = path(name) / f"iter_{number:03d}"
    it.mkdir()
    return it


def evaluate(name: str, it: Path, note: str = "") -> dict:
    """Analyze ``it/render.wav`` against the target and write the report files."""
    s = path(name)
    target = read_json(s / "target" / "features.json")
    render_audio, sr = features.load(it / "render.wav")
    feat = features.analyze_array(render_audio, sr)
    result = cmp_mod.compare(feat, target)
    write_json(it / "features.json", feat)
    write_json(it / "compare.json", result)
    target_audio, _ = features.load(s / "target" / "reference.wav")
    plot.comparison_png(it / "compare.png", feat, target, result,
                        render_audio=render_audio, target_audio=target_audio,
                        title=f"{name} {it.name}")
    with open(s / "history.md", "a") as h:
        h.write(f"| {it.name[5:]} | {result['score']} | {note.replace('|', '/')} |\n")
    return result


def best(name: str):
    scored = [(read_json(i / "compare.json")["score"], i)
              for i in iterations(name) if (i / "compare.json").exists()]
    return max(scored) if scored else None
