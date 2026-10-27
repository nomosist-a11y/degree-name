"""Is this render just the DI coming back? Catch it before it enters a comparison.

Measured on the player's unit: blocks that do not sound from the host
(unowned plugins, host-loaded captures) return the DI untouched, and the
render looks like this - the 1-5 kHz spectral flatness stays under 0.08 (a
clean, tonal guitar) and the features match the DI's. A render that passes
both tests is excluded from scoring, tournaments and sweeps.

The audio test cannot see a passthrough block that sits in a chain with other
blocks that do sound (a cab after an unowned amp still colours the DI), so the
static check in :mod:`qcsuite.device.inert` runs alongside it.
"""

from __future__ import annotations

from qcsuite.audio.score import shape_rms_db

FLATNESS_LIMIT = 0.08
#: How close to the DI the third-octave shape must be, as RMS dB over
#: 100 Hz-5 kHz (the band the player's own scoring used). Above 5 kHz a clean DI
#: has almost nothing, so the unit's noise floor alone would spoil a full-band
#: comparison. A true passthrough is the DI itself (0 dB apart on the fake); a
#: mild clean amp + cab came within 1.9 dB on the fake, so the tolerances stay
#: tight. Both are guesses to be checked against a real passthrough render in
#: the hardware test plan (the self-test prints the numbers).
SHAPE_TOLERANCE_DB = 1.0
CREST_TOLERANCE_DB = 1.5


def check(render_features: dict, di_features: dict) -> dict:
    """Decide from two :func:`qctone.features.analyze_array` results."""
    flatness = render_features["spectral"]["flatness_1k_5k"]
    shape = shape_rms_db(render_features, di_features)
    crest = abs(render_features["dynamics"]["crest_db"] - di_features["dynamics"]["crest_db"])
    passthrough = (flatness < FLATNESS_LIMIT and shape <= SHAPE_TOLERANCE_DB
                   and crest <= CREST_TOLERANCE_DB)
    reason = ""
    if passthrough:
        reason = (f"素通しの疑い: 1〜5 kHz の flatness {flatness:.3f} < {FLATNESS_LIMIT}、"
                  f"DI との形の差 {shape:.1f} dB、クレスト差 {crest:.1f} dB")
    return {"passthrough": bool(passthrough), "flatness_1k_5k": round(flatness, 4),
            "shape_diff_db": round(shape, 2), "crest_diff_db": round(crest, 2),
            "reason": reason}
