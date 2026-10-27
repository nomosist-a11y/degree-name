"""DIs: the player's own recordings, plus a synthetic one for trying things out.

Real work uses the player's DI files (``qcsuite di import`` / ``record``). The
synthetic DI exists so the mock has something guitar-like to play: plucked
strings with slight inharmonicity, chords (whose intermodulation makes the
fake amp's distortion read as distortion), pick noise, and silence between
phrases. It is not a recording of anything and is labelled as such everywhere
it appears.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

from qcsuite import paths
from qcsuite.audio import export
from qctone import features

SR = 48000
SYNTH_NAME = "synthetic-riff"

_E5, _G5, _A5 = [82.41, 123.62, 164.81], [98.0, 147.0, 196.0], [110.0, 165.0, 220.0]
_RIFF = [  # (start s, notes in Hz, duration s): an E5-G5-A5 riff, a short lick, E5
    (0.0, _E5, 0.45), (0.5, _E5, 0.45), (1.0, _G5, 0.9), (2.0, _A5, 0.45), (2.5, _A5, 0.45),
    (3.0, _E5, 0.9), (4.0, [329.6], 0.3), (4.3, [392.0], 0.3), (4.6, [440.0], 0.8),
    (5.5, _E5, 1.4),
]
#: Tuned so the DI reads like a real one to the analysis: 1-5 kHz flatness about
#: 0.06 (a clean DI is tonal, under the 0.08 passthrough threshold), rising with
#: the fake amp's gain while the crest factor falls.
_ROLLOFF, _PICKUP_HZ = 1.4, 3000.0


def _string(f0, n, sr, rng, inharm=1e-5):
    t = np.arange(n) / sr
    out = np.zeros(n)
    for k in range(1, 60):
        fk = f0 * k * np.sqrt(1 + inharm * k * k)
        if fk > sr * 0.45:
            break
        out += np.sin(2 * np.pi * fk * t + rng.uniform(0, 2 * np.pi)) * \
            np.exp(-(1.5 + 0.3 * k) * t) / k ** _ROLLOFF
    return out


def synth_di(seconds: float = 8.0, sr: int = SR, seed: int = 1) -> np.ndarray:
    """A mono float32 guitar-like DI peaking at -6 dBFS."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    out = np.zeros(n)
    period = 7.5
    attack, release = int(0.003 * sr), int(0.04 * sr)
    for rep in range(int(np.ceil(seconds / period))):
        for start, notes, dur in _RIFF:
            s = int((start + rep * period) * sr)
            if s >= n:
                continue
            length = min(int(dur * sr), n - s)
            env = np.ones(length)
            a, r = min(attack, length), min(release, length)
            env[:a] = np.linspace(0, 1, a)
            env[length - r:] *= np.linspace(1, 0, r) ** 2
            for f0 in notes:
                out[s:s + length] += _string(f0, length, sr, rng) * env
            m = min(240, length)
            out[s:s + m] += 0.02 * rng.standard_normal(m) * np.exp(-np.arange(m) / 40)
    out = signal.sosfilt(signal.butter(2, _PICKUP_HZ, "low", fs=sr, output="sos"), out)
    out += 1e-6 * rng.standard_normal(n)
    return (0.5 * out / np.max(np.abs(out))).astype(np.float32)


# -- the DI library ------------------------------------------------------------

def library_dir(mock: bool) -> Path:
    return paths.sub("di", mock)


def _meta_path(path: Path) -> Path:
    return path.with_suffix(".json")


def save(name: str, audio, sr: int, mock: bool, source: str, note: str = "") -> dict:
    """Store a DI as 48 kHz float WAV with a JSON sidecar describing it."""
    if sr != SR:
        raise ValueError("DI は 48 kHz で保存します(features.load で変換してから渡してください)")
    mono = np.asarray(audio, dtype=np.float32)
    mono = mono.mean(axis=1) if mono.ndim == 2 else mono
    path = library_dir(mock) / f"{export.safe_name(name)}.wav"
    sf.write(str(path), mono, sr, subtype="FLOAT")
    meta = {"name": name, "file": path.name, "seconds": round(len(mono) / sr, 2),
            "peak_dbfs": round(20 * np.log10(float(np.max(np.abs(mono))) + 1e-12), 2),
            "source": source, "note": note, "added": time.strftime("%Y-%m-%d %H:%M:%S")}
    _meta_path(path).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def ensure_synthetic(mock: bool) -> dict:
    path = library_dir(mock) / f"{SYNTH_NAME}.wav"
    if path.exists():
        return json.loads(_meta_path(path).read_text())
    return save(SYNTH_NAME, synth_di(), SR, mock, source="synthetic",
                note="合成した DI(実際の演奏ではありません)")


def import_file(src, name: str | None, mock: bool, start: float = 0.0,
                duration: float | None = None, note: str = "") -> dict:
    data, sr = features.load(src, start, duration)
    return save(name or Path(src).stem, data, sr, mock, source=f"import:{Path(src).name}",
                note=note)


def listing(mock: bool) -> list[dict]:
    out = []
    for meta in sorted(library_dir(mock).glob("*.json")):
        try:
            out.append(json.loads(meta.read_text()))
        except ValueError as exc:
            raise ValueError(f"{meta} が壊れています: {exc}") from exc
    return out


def load(name: str, mock: bool) -> tuple[np.ndarray, int]:
    """A DI from the library by name (or a path to any audio file)."""
    candidate = Path(name)
    if candidate.suffix and candidate.exists():
        data, sr = features.load(candidate)
        return data.mean(axis=1), sr
    if name == SYNTH_NAME:
        ensure_synthetic(mock)
    for meta in listing(mock):
        if meta["name"] == name or meta["file"] == name:
            data, sr = features.load(library_dir(mock) / meta["file"])
            return data.mean(axis=1), sr
    raise FileNotFoundError(f"DI「{name}」がライブラリにありません(qcsuite di list で確認)")


def trim(name: str, mock: bool, start: float, duration: float, new_name: str) -> dict:
    data, sr = load(name, mock)
    piece = data[int(start * sr): int((start + duration) * sr)]
    return save(new_name, piece, sr, mock, source=f"trim:{name}@{start}+{duration}")
