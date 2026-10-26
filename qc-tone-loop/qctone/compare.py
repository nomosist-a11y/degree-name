"""Compare a render's features with the target's and say what differs.

Hints are deliberately device-agnostic ("cut around 2.5 kHz"); turning them
into a concrete block and knob is the job of whoever plans the next patch.
"""

from __future__ import annotations

import numpy as np

from qctone.features import BANDS, THIRD_OCTAVE_CENTRES

# Differences smaller than these are treated as matched.
CURVE_TOLERANCE_DB = 1.5
BAND_TOLERANCE_DB = 1.5

# Bands below 60 Hz and above 12.5 kHz matter less to how a guitar reads.
_WEIGHTS = np.array([
    0.3 if fc < 60 else 0.5 if fc > 12500 else 1.0 for fc in THIRD_OCTAVE_CENTRES
])


def _curve_diff(render, target):
    return np.array(render["third_octave"]["db"]) - np.array(target["third_octave"]["db"])


def score(render, target) -> float:
    """0-100 match score from the weighted RMS of the third-octave difference.

    100 is identical shape; each dB of weighted RMS error costs 5 points,
    so 3 dB of average mismatch scores 85.
    """
    diff = _curve_diff(render, target)
    rms = float(np.sqrt(np.sum(_WEIGHTS * diff ** 2) / _WEIGHTS.sum()))
    return round(max(0.0, 100.0 - 5.0 * rms), 1)


def _regions(diff):
    """Group adjacent third-octave bands that miss in the same direction."""
    regions, current = [], None
    for i, d in enumerate(diff):
        sign = 0 if abs(d) < CURVE_TOLERANCE_DB else int(np.sign(d))
        if sign and current and current["sign"] == sign:
            current["idx"].append(i)
        else:
            if current:
                regions.append(current)
            current = {"sign": sign, "idx": [i]} if sign else None
    if current:
        regions.append(current)
    out = []
    for r in regions:
        values = diff[r["idx"]]
        peak = int(r["idx"][int(np.argmax(np.abs(values)))])
        out.append({
            "from_hz": THIRD_OCTAVE_CENTRES[r["idx"][0]],
            "to_hz": THIRD_OCTAVE_CENTRES[r["idx"][-1]],
            "peak_hz": THIRD_OCTAVE_CENTRES[peak],
            "render_minus_target_db": round(float(diff[peak]), 1),
            "action": "cut" if r["sign"] > 0 else "boost",
        })
    return sorted(out, key=lambda r: -abs(r["render_minus_target_db"]))


def compare(render, target):
    diff = _curve_diff(render, target)
    band_diff = {
        k: round(render["band_balance_db"][k] - target["band_balance_db"][k], 2)
        for k in BANDS
    }
    spectral_diff = {
        k: round(render["spectral"][k] - target["spectral"][k], 4)
        for k in render["spectral"]
    }
    dyn_diff = {
        k: round(render["dynamics"][k] - target["dynamics"][k], 2)
        for k in ("crest_db", "dynamic_range_db")
    }
    hints = []
    for r in _regions(diff)[:5]:
        span = "" if r["from_hz"] == r["to_hz"] else f" ({r['from_hz']}-{r['to_hz']} Hz)"
        hints.append(
            f"{r['action']} around {r['peak_hz']} Hz{span} "
            f"by ~{abs(r['render_minus_target_db'])} dB"
        )
    tilt = spectral_diff["tilt_db_per_oct"]
    if abs(tilt) > 0.75:
        hints.append(
            f"overall tilt is {'brighter' if tilt > 0 else 'darker'} than target by "
            f"{abs(tilt):.1f} dB/oct (treble/presence or cab/mic choice)"
        )
    flat = spectral_diff["flatness_1k_5k"]
    if abs(flat) > 0.05:
        hints.append(
            f"1-5 kHz is {'noisier/fizzier' if flat > 0 else 'more tonal/cleaner'} "
            f"than target (flatness {flat:+.3f}): likely {'less' if flat > 0 else 'more'} gain"
        )
    crest = dyn_diff["crest_db"]
    if abs(crest) > 2.0:
        hints.append(
            f"crest factor {crest:+.1f} dB vs target: render is "
            f"{'more dynamic' if crest > 0 else 'more compressed'} "
            f"({'more gain/compression' if crest > 0 else 'less gain/compression'} would close it)"
        )
    if render.get("stereo_width_db") is not None and target.get("stereo_width_db") is not None:
        w = render["stereo_width_db"] - target["stereo_width_db"]
        if abs(w) > 3.0:
            hints.append(
                f"stereo width {w:+.1f} dB vs target "
                f"({'narrower' if w < 0 else 'wider'} modulation/delay/reverb would close it)"
            )
    return {
        "score": score(render, target),
        "third_octave_diff_db": [round(float(d), 2) for d in diff],
        "band_balance_diff_db": band_diff,
        "spectral_diff": spectral_diff,
        "dynamics_diff": dyn_diff,
        "mismatch_regions": _regions(diff),
        "hints": hints,
    }
