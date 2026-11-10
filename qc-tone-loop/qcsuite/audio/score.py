"""Distance between two renders' features - for sorting, never for deciding.

The overnight sweep on the player's unit (about 430 configurations) scored each
render against a reference by combining three things: the RMS error of the
third-octave shape between 100 Hz and 5 kHz, the difference in 1-5 kHz
spectral flatness, and the difference in crest factor. This module computes
those three and combines them. **The weights are a guess** - the previous
session's exact weighting was not recorded - so the combined number is only
good for ordering candidates for a person to listen to, not for picking one.

Without a reference there is nothing to score: sweeps then just record the
features, and the blind tournament (people listening) does the ranking.
"""

from __future__ import annotations

import numpy as np

from qctone.features import THIRD_OCTAVE_CENTRES

SHAPE_RANGE = (100, 5000)
W_FLATNESS = 20.0          # 0.05 of flatness counts like 1 dB of shape error
W_CREST = 0.5              # 2 dB of crest difference counts like 1 dB of shape error


def shape_rms_db(a: dict, b: dict, lo: float = SHAPE_RANGE[0], hi: float = SHAPE_RANGE[1]) -> float:
    """RMS difference (dB) of two third-octave curves, re-centred over lo..hi."""
    centres = np.array(THIRD_OCTAVE_CENTRES)
    sel = (centres >= lo) & (centres <= hi)
    da = np.array(a["third_octave"]["db"])[sel]
    db = np.array(b["third_octave"]["db"])[sel]
    diff = (da - da.mean()) - (db - db.mean())
    return float(np.sqrt(np.mean(diff ** 2)))


def distance(render: dict, reference: dict) -> dict:
    shape = shape_rms_db(render, reference)
    flat = abs(render["spectral"]["flatness_1k_5k"] - reference["spectral"]["flatness_1k_5k"])
    crest = abs(render["dynamics"]["crest_db"] - reference["dynamics"]["crest_db"])
    combined = shape + W_FLATNESS * flat + W_CREST * crest
    return {"shape_rms_db": round(shape, 2), "flatness_diff": round(flat, 4),
            "crest_diff_db": round(crest, 2), "distance": round(combined, 2),
            "score": round(max(0.0, 100.0 - 5.0 * combined), 1)}
