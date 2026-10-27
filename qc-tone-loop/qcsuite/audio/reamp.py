"""Render a stored preset, or the live grid, through one DI - the shared recipe.

Every feature that compares presets by ear goes through here, so they all do
the same thing in the same order:

1. recall the preset (the unit's sound changes; a notice says so);
2. re-point its guitar-input rows at USB 5 - unsaved, undone by the next recall;
3. reamp the DI (monitor warning on hardware) and analyze the render;
4. flag the render if it is the DI coming back (audio test) or if the preset
   holds blocks known not to sound from the host (static test).

The caller decides what to do with a flagged render; comparisons drop it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from qcsuite import presets
from qcsuite.audio import export, lufs, passthrough
from qctone import features


def analyze(audio, sr: int, di_features: dict | None = None) -> dict:
    feat = features.analyze_array(np.asarray(audio), sr)
    out = {"features": feat, "loudness": lufs.report(audio, sr)}
    if di_features is not None:
        out["passthrough"] = passthrough.check(feat, di_features)
    return out


def static_issues(normalized: dict) -> list[str]:
    """Blocks in a normalized preset that will not sound when reamped from here."""
    issues = []
    for b in normalized["blocks"]:
        if b.get("inert_plugin"):
            issues.append(f"{presets.block_label(b)}: 未所有のプラグイン(素通し)")
    return issues


def render_current(rig, di, sr: int, di_features: dict | None = None,
                   out_path: Path | None = None, fmt: str = "flac") -> dict:
    """Reamp the live grid as it is (routing included). Returns analysis + file."""
    result = rig.render(di, sr)
    info = {"render": result.summary(), **analyze(result.audio, sr, di_features)}
    if out_path is not None:
        info["file"] = str(export.write(out_path, result.audio, sr, fmt))
    info["_audio"] = result.audio
    return info


def render_preset(rig, folder: str, position: int, di, sr: int,
                  di_features: dict | None = None, out_path: Path | None = None,
                  fmt: str = "flac", scene: int | None = None) -> dict:
    """Recall ``folder``/``position``, route it to USB 5, reamp ``di``, analyze.

    The returned dict carries ``preset`` (normalized), ``render``, ``features``,
    ``loudness``, ``passthrough`` (if ``di_features``), ``static_issues``,
    ``excluded`` (bool + reason) and, when ``out_path`` is given, ``file``.
    ``_audio`` holds the samples for callers that keep working on them.
    """
    device = rig.device
    stored = device.read_stored(folder, position)
    normalized = presets.normalize(stored, device.catalog, folder=folder, position=position)
    rows = device.prepare_reamp(stored)
    already = [r["row"] for r in normalized["rows"] if r["in_port"] in ("USB_5", "USB_5_6")]
    if not rows and not already:
        raise RuntimeError(f"「{normalized['name']}」には Input 1/2 または USB 5 から始まる行が"
                           "ないため、リアンプできません。")
    if scene is not None:
        device.switch_scene(scene)
    info = render_current(rig, di, sr, di_features, out_path, fmt)
    info["preset"] = normalized
    info["static_issues"] = static_issues(normalized)
    reasons = list(info["static_issues"])
    if info.get("passthrough", {}).get("passthrough"):
        reasons.append(info["passthrough"]["reason"])
    info["excluded"] = {"excluded": bool(reasons), "reasons": reasons}
    return info
