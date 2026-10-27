"""Loudness per ITU-R BS.1770-4: K-weighting, 400 ms blocks, two-stage gating.

Written out here rather than pulled from a package so the one number every
comparison leans on has no hidden dependency. Checked against the standard's
own reference point: a 1 kHz sine at 0 dBFS in one channel reads -3.01 LUFS.

Also gives sample peak and an approximate true peak (4x oversampled), which is
what tells a loudness-matched render whether it would clip.
"""

from __future__ import annotations

import numpy as np
from scipy import signal


def _k_weighting(sr: int):
    """The two K-weighting biquads (high shelf, then RLB high-pass) for ``sr``."""
    # Stage 1: high-frequency shelf (pre-filter), bilinear-transformed design.
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / sr)
    vh = 10 ** (gain_db / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    b1 = [(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0]
    a1 = [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    # Stage 2: RLB high-pass.
    f0, q = 38.13547087602444, 0.5003270373238773
    k = np.tan(np.pi * f0 / sr)
    a0 = 1 + k / q + k * k
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    return (np.array(b1), np.array(a1)), (np.array(b2), np.array(a2))


def _as_2d(data) -> np.ndarray:
    x = np.asarray(data, dtype=np.float64)
    return x[:, None] if x.ndim == 1 else x


def integrated_loudness(data, sr: int) -> float:
    """Integrated loudness in LUFS; ``-inf`` for silence or under 400 ms."""
    x = _as_2d(data)
    (b1, a1), (b2, a2) = _k_weighting(sr)
    y = signal.lfilter(b2, a2, signal.lfilter(b1, a1, x, axis=0), axis=0)
    block = int(round(0.4 * sr))
    hop = int(round(0.1 * sr))
    if len(y) < block:
        return float("-inf")
    starts = np.arange(0, len(y) - block + 1, hop)
    power = np.cumsum(np.vstack([np.zeros((1, y.shape[1])), y ** 2]), axis=0)
    z = (power[starts + block] - power[starts]) / block       # mean square per block/channel
    # Channel weights: 1.0 for L, R and C; surround channels are not used here.
    loudness = -0.691 + 10 * np.log10(np.maximum(z.sum(axis=1), 1e-20))
    gated = z[loudness > -70.0]
    if len(gated) == 0:
        return float("-inf")
    relative = -0.691 + 10 * np.log10(gated.sum(axis=1).mean()) - 10.0
    gated = z[(loudness > -70.0) & (loudness > relative)]
    if len(gated) == 0:
        return float("-inf")
    return float(-0.691 + 10 * np.log10(gated.sum(axis=1).mean()))


def sample_peak_dbfs(data) -> float:
    peak = float(np.max(np.abs(data))) if np.size(data) else 0.0
    return 20 * np.log10(peak) if peak > 0 else float("-inf")


def true_peak_dbtp(data, oversample: int = 4) -> float:
    """Approximate true peak: the sample peak of a 4x oversampled copy."""
    x = _as_2d(data)
    if not len(x):
        return float("-inf")
    up = signal.resample_poly(x, oversample, 1, axis=0)
    return sample_peak_dbfs(up)


def report(data, sr: int) -> dict:
    lufs = integrated_loudness(data, sr)
    return {
        "lufs": None if not np.isfinite(lufs) else round(lufs, 2),
        "sample_peak_dbfs": round(sample_peak_dbfs(data), 2),
        "true_peak_dbtp": round(true_peak_dbtp(data), 2),
    }


def gain_to(data, sr: int, target_lufs: float, ceiling_dbtp: float = -1.0):
    """The gain (dB) that brings ``data`` to ``target_lufs``, and whether the
    true-peak ceiling forced it lower. Returns ``(gain_db, limited)``."""
    lufs = integrated_loudness(data, sr)
    if not np.isfinite(lufs):
        raise ValueError("無音のため音量を測れません")
    gain = target_lufs - lufs
    headroom = ceiling_dbtp - true_peak_dbtp(data)
    if gain > headroom:
        return headroom, True
    return gain, False


def apply_gain(data, gain_db: float) -> np.ndarray:
    return (np.asarray(data, dtype=np.float32) * np.float32(10 ** (gain_db / 20)))
