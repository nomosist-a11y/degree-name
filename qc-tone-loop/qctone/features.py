"""Acoustic features of a guitar recording, as plain JSON-able numbers.

Claude cannot listen to audio, so every judgement in the loop is made from
these numbers plus the comparison plot. Everything here is level-independent
where it can be, because a reamp and a reference track never sit at the same
loudness.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf
from scipy import signal

SAMPLE_RATE = 48000

# Third-octave centres from 40 Hz to 16 kHz.
THIRD_OCTAVE_CENTRES = [
    40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
    1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000,
]

# Named ranges guitarists talk in, used for the coarse balance and the hints.
BANDS = {
    "sub": (20, 80),
    "low": (80, 250),
    "low_mid": (250, 800),
    "mid": (800, 2000),
    "high_mid": (2000, 5000),
    "presence": (5000, 8000),
    "air": (8000, 16000),
}

# The reference window the third-octave curve is normalized to (0 dB mean).
NORM_RANGE = (200, 5000)


def load(path, start: float = 0.0, duration: float | None = None):
    """Read ``path`` as float32, resampled to 48 kHz. Returns (data, sr).

    ``data`` is (samples, channels), mono files included.
    """
    info = sf.info(str(path))
    first = int(start * info.samplerate)
    frames = -1 if duration is None else int(duration * info.samplerate)
    data, sr = sf.read(str(path), start=first, frames=frames,
                       dtype="float32", always_2d=True)
    if sr != SAMPLE_RATE:
        g = np.gcd(sr, SAMPLE_RATE)
        data = signal.resample_poly(data, SAMPLE_RATE // g, sr // g, axis=0)
        sr = SAMPLE_RATE
    return data.astype(np.float32), sr


def _db(x, floor=1e-12):
    return 10.0 * np.log10(np.maximum(x, floor))


def trim_silence(mono, sr, threshold_db: float = -50.0):
    """Drop leading/trailing stretches quieter than ``threshold_db`` below peak."""
    hop = int(0.01 * sr)
    if len(mono) < hop * 2:
        return mono
    frames = len(mono) // hop
    rms = np.sqrt(np.mean(mono[: frames * hop].reshape(frames, hop) ** 2, axis=1))
    level = 20 * np.log10(np.maximum(rms, 1e-9))
    live = np.where(level > level.max() + threshold_db)[0]
    if len(live) == 0:
        return mono
    return mono[live[0] * hop: (live[-1] + 1) * hop]


def third_octave_curve(mono, sr):
    """Long-term average spectrum in third-octave bands, normalized.

    Returns dB values per :data:`THIRD_OCTAVE_CENTRES`, shifted so the mean of
    the bands inside :data:`NORM_RANGE` is 0 dB. Two curves can then be
    compared by shape alone.
    """
    freqs, psd = signal.welch(mono, sr, nperseg=8192, noverlap=4096)
    values = []
    for fc in THIRD_OCTAVE_CENTRES:
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        sel = (freqs >= lo) & (freqs < hi)
        values.append(psd[sel].mean() if sel.any() else 0.0)
    values = np.array(values)
    # A floor relative to the loudest band keeps the curve level-independent.
    curve = _db(values, floor=values.max() * 1e-12)
    centres = np.array(THIRD_OCTAVE_CENTRES)
    ref = (centres >= NORM_RANGE[0]) & (centres <= NORM_RANGE[1])
    return curve - curve[ref].mean(), freqs, psd


def band_balance(freqs, psd):
    """Share of energy (in dB relative to the total) in each named band."""
    total = psd[(freqs >= 20) & (freqs <= 16000)].sum()
    return {
        name: float(_db(psd[(freqs >= lo) & (freqs < hi)].sum() / total))
        for name, (lo, hi) in BANDS.items()
    }


def spectral_stats(mono, sr):
    """Centroid, 85% rolloff, flatness and tilt over the whole file."""
    freqs, psd = signal.welch(mono, sr, nperseg=4096)
    sel = (freqs >= 40) & (freqs <= 16000)
    f, p = freqs[sel], psd[sel]
    centroid = float((f * p).sum() / p.sum())
    cumulative = np.cumsum(p) / p.sum()
    rolloff = float(f[np.searchsorted(cumulative, 0.85)])
    # Flatness in the 1-5 kHz range: rises with distortion / fizz.
    mid = (f >= 1000) & (f <= 5000)
    flatness = float(np.exp(np.mean(np.log(p[mid] + 1e-20))) / (p[mid].mean() + 1e-20))
    # Tilt: dB per octave across 100 Hz - 10 kHz.
    tsel = (f >= 100) & (f <= 10000)
    slope = np.polyfit(np.log2(f[tsel]), _db(p[tsel]), 1)[0]
    return {
        "centroid_hz": centroid,
        "rolloff85_hz": rolloff,
        "flatness_1k_5k": flatness,
        "tilt_db_per_oct": float(slope),
    }


def dynamics(mono, sr):
    """Level statistics: how hard the tone is compressed / saturated."""
    peak = float(np.max(np.abs(mono)) + 1e-12)
    rms = float(np.sqrt(np.mean(mono ** 2)) + 1e-12)
    hop = int(0.05 * sr)
    frames = len(mono) // hop
    short = np.sqrt(np.mean(mono[: frames * hop].reshape(frames, hop) ** 2, axis=1))
    short_db = 20 * np.log10(np.maximum(short, 1e-9))
    short_db = short_db[short_db > short_db.max() - 40]
    return {
        "rms_dbfs": float(20 * np.log10(rms)),
        "peak_dbfs": float(20 * np.log10(peak)),
        "crest_db": float(20 * np.log10(peak / rms)),
        "dynamic_range_db": float(np.percentile(short_db, 95) - np.percentile(short_db, 10)),
    }


def stereo_width(data):
    """Side/mid energy ratio in dB; very negative means effectively mono."""
    if data.shape[1] < 2:
        return None
    mid = (data[:, 0] + data[:, 1]) / 2
    side = (data[:, 0] - data[:, 1]) / 2
    return float(_db(np.mean(side ** 2) / (np.mean(mid ** 2) + 1e-20)))


def analyze_array(data, sr):
    mono = trim_silence(data.mean(axis=1), sr)
    curve, freqs, psd = third_octave_curve(mono, sr)
    return {
        "duration_s": round(len(mono) / sr, 2),
        "third_octave": {
            "centres_hz": THIRD_OCTAVE_CENTRES,
            "db": [round(float(v), 2) for v in curve],
        },
        "band_balance_db": {k: round(v, 2) for k, v in band_balance(freqs, psd).items()},
        "spectral": {k: round(v, 4) for k, v in spectral_stats(mono, sr).items()},
        "dynamics": {k: round(v, 2) for k, v in dynamics(mono, sr).items()},
        "stereo_width_db": None if (w := stereo_width(data)) is None else round(w, 2),
    }


def analyze(path, start: float = 0.0, duration: float | None = None):
    data, sr = load(path, start, duration)
    return analyze_array(data, sr)
