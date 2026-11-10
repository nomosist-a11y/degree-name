"""Writing audio for people and DAWs: formats, names, matched loudness.

Stems for Logic Pro are 48 kHz, 24-bit, interleaved stereo WAV (or FLAC), all
starting at the DI's sample 0 so they line up when dropped at bar 1. Names are
``<NN>_<DI>__<preset>.wav`` with characters Logic and Finder dislike replaced.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf

from qcsuite.audio import lufs

FORMATS = {"wav": ("WAV", "PCM_24"), "flac": ("FLAC", "PCM_24"), "wav32": ("WAV", "FLOAT")}


def safe_name(text: str, limit: int = 60) -> str:
    """A filename-safe version of ``text`` (keeps Japanese, drops / : etc.)."""
    text = unicodedata.normalize("NFC", text).strip()
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or "untitled")[:limit]


def write(path: Path, audio, sr: int, fmt: str = "wav") -> Path:
    container, subtype = FORMATS[fmt]
    path = Path(path)
    if fmt.startswith("wav") and path.suffix.lower() != ".wav":
        path = path.with_suffix(".wav")
    if fmt == "flac" and path.suffix.lower() != ".flac":
        path = path.with_suffix(".flac")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    sf.write(str(path), data, sr, format=container, subtype=subtype)
    return path


def match_loudness(items: list[np.ndarray], sr: int, target_lufs: float | None = None,
                   ceiling_dbtp: float = -1.0):
    """Bring every item to one loudness without any of them clipping.

    With no ``target_lufs`` the target is the quietest item's loudness, so
    nothing is boosted into the ceiling. If the ceiling still forces an item
    lower, the target drops for everyone - equal loudness wins over level.
    Returns ``(matched items, target, per-item gains in dB)``.
    """
    measured = [lufs.integrated_loudness(x, sr) for x in items]
    finite = [m for m in measured if np.isfinite(m)]
    if not finite:
        raise ValueError("どの音源も無音です")
    target = min(finite) if target_lufs is None else target_lufs
    for x, m in zip(items, measured):
        if np.isfinite(m):
            headroom = ceiling_dbtp - lufs.true_peak_dbtp(x)
            target = min(target, m + headroom)
    gains = [target - m if np.isfinite(m) else 0.0 for m in measured]
    return [lufs.apply_gain(x, g) for x, g in zip(items, gains)], target, gains
