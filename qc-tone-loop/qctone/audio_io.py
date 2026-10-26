"""Reamp through the Quad Cortex's USB audio interface.

Default channel map, per the Quad Cortex manual (check yours under
Settings > I/O if the recordings come back silent or dry):

* computer -> QC: USB playback 5 feeds the grid's ``USB 5`` input block;
* QC -> computer: USB record 1 is the dry Input 1 (DI), and
  USB record 3/4 carries the processed Output 1/2.

Channel numbers here are 1-based, like the manual and every DAW.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from qctone.features import SAMPLE_RATE, load

DEVICE_HINT = "Quad Cortex"
REAMP_OUT_CHANNEL = 5
WET_IN_CHANNELS = (3, 4)
DRY_IN_CHANNEL = 1
PREROLL_S = 0.5
TAIL_S = 2.0


def _sd():
    try:
        import sounddevice
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise SystemExit("sounddevice is missing: pip install -e '.[device]'") from exc
    return sounddevice


def find_device(hint: str = DEVICE_HINT):
    sd = _sd()
    for index, dev in enumerate(sd.query_devices()):
        if hint.lower() in dev["name"].lower() and dev["max_input_channels"] >= 4:
            return index
    names = [d["name"] for d in sd.query_devices()]
    raise SystemExit(f"no audio device matching {hint!r}; found {names}")


def list_devices():
    return str(_sd().query_devices())


def reamp(di_path, out_path, device_hint: str = DEVICE_HINT,
          out_channel: int = REAMP_OUT_CHANNEL, in_channels=WET_IN_CHANNELS):
    """Play ``di_path`` into the QC and record what comes back to ``out_path``.

    The grid row being reamped must take its input from ``USB 5`` (see the
    ``set_input`` patch op). Pre-roll and the latency it absorbs are trimmed;
    the tail is kept so reverb and delay are part of the analysis.
    """
    sd = _sd()
    device = find_device(device_hint)
    di, sr = load(di_path)
    mono = di.mean(axis=1)
    pre = np.zeros(int(PREROLL_S * sr), dtype=np.float32)
    tail = np.zeros(int(TAIL_S * sr), dtype=np.float32)
    play = np.concatenate([pre, mono, tail])[:, None]
    rec = sd.playrec(play, samplerate=sr, device=device,
                     input_mapping=list(in_channels), output_mapping=[out_channel],
                     dtype="float32", blocking=True)
    rec = rec[int(PREROLL_S * sr * 0.8):]
    peak = float(np.max(np.abs(rec)))
    if peak < 1e-4:
        raise SystemExit(
            "the reamp came back silent: check the grid row's input is USB 5, its "
            "output reaches Out 1/2, and the USB channel map in qctone/audio_io.py"
        )
    sf.write(str(out_path), rec, sr, subtype="FLOAT")
    return {"peak_dbfs": round(20 * np.log10(peak), 2), "clipped": peak >= 0.999}


def record_dry(out_path, seconds: float, device_hint: str = DEVICE_HINT,
               channel: int = DRY_IN_CHANNEL):
    """Record the dry DI from the guitar plugged into QC Input 1."""
    sd = _sd()
    device = find_device(device_hint)
    rec = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, device=device,
                 mapping=[channel], dtype="float32", blocking=True)
    sf.write(str(out_path), rec, SAMPLE_RATE, subtype="FLOAT")
    return {"peak_dbfs": round(20 * np.log10(float(np.max(np.abs(rec))) + 1e-12), 2)}
