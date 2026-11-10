"""Reamp through the Quad Cortex's USB audio interface.

Default channel map, per the Quad Cortex manual (check yours under
Settings > I/O if the recordings come back silent or dry):

* computer -> QC: USB playback 5 feeds the grid's ``USB 5`` input block;
* QC -> computer: USB record 1 is the dry Input 1 (DI), and
  USB record 3/4 carries the processed Output 1/2.

Channel numbers here are 1-based, like the manual and every DAW. Override
them without editing code through environment variables::

    QCTONE_AUDIO_DEVICE="Quad Cortex"  QCTONE_REAMP_OUT=5
    QCTONE_WET_IN=3,4                  QCTONE_DRY_IN=1
"""

from __future__ import annotations

import os

import numpy as np
import soundfile as sf

from qctone.features import SAMPLE_RATE, load


def _channels(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        values = tuple(int(c) for c in raw.split(","))
    except ValueError:
        raise SystemExit(f"{name}={raw!r}: expected channel numbers like 3,4") from None
    if not values or min(values) < 1:
        raise SystemExit(f"{name}={raw!r}: channels are 1-based")
    return values if isinstance(default, tuple) else values[0]


DEVICE_HINT = os.environ.get("QCTONE_AUDIO_DEVICE", "Quad Cortex")
REAMP_OUT_CHANNEL = _channels("QCTONE_REAMP_OUT", 5)
WET_IN_CHANNELS = _channels("QCTONE_WET_IN", (3, 4))
DRY_IN_CHANNEL = _channels("QCTONE_DRY_IN", 1)
PREROLL_S = 0.5
TAIL_S = 2.0


def _sd():
    try:
        import sounddevice
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise SystemExit("sounddevice is missing: pip install -e '.[device]'") from exc
    except OSError as exc:  # pragma: no cover - PortAudio missing (e.g. CI, Linux)
        raise SystemExit(f"sounddevice cannot load PortAudio: {exc}. On a Mac this "
                         "comes with the sounddevice wheel; elsewhere install PortAudio.") from exc
    return sounddevice


def find_device(hint: str = DEVICE_HINT):
    sd = _sd()
    needed_in = max(*WET_IN_CHANNELS, DRY_IN_CHANNEL)
    for index, dev in enumerate(sd.query_devices()):
        if (hint.lower() in dev["name"].lower()
                and dev["max_input_channels"] >= needed_in
                and dev["max_output_channels"] >= REAMP_OUT_CHANNEL):
            return index
    names = [d["name"] for d in sd.query_devices()]
    raise SystemExit(
        f"no audio device matching {hint!r} with {needed_in}+ inputs and "
        f"{REAMP_OUT_CHANNEL}+ outputs; found {names}")


def list_devices():
    return str(_sd().query_devices())


def onset(x, threshold_db: float = -30.0) -> int | None:
    """First sample within ``threshold_db`` of the peak, or None if silent."""
    level = np.abs(x)
    peak = float(level.max()) if len(level) else 0.0
    if peak < 1e-6:
        return None
    return int(np.argmax(level >= peak * 10 ** (threshold_db / 20)))


def playrec(mono, sr=SAMPLE_RATE, device_hint: str = DEVICE_HINT,
            out_channel: int = REAMP_OUT_CHANNEL, in_channels=WET_IN_CHANNELS):
    """Play ``mono`` into the QC with pre-roll and tail; return what came back."""
    sd = _sd()
    device = find_device(device_hint)
    pre = np.zeros(int(PREROLL_S * sr), dtype=np.float32)
    tail = np.zeros(int(TAIL_S * sr), dtype=np.float32)
    play = np.concatenate([pre, np.asarray(mono, dtype=np.float32), tail])[:, None]
    return sd.playrec(play, samplerate=sr, device=device,
                      input_mapping=list(in_channels), output_mapping=[out_channel],
                      dtype="float32", blocking=True)


def reamp(di_path, out_path, **kwargs):
    """Play ``di_path`` into the QC and record what comes back to ``out_path``.

    The grid row being reamped must take its input from ``USB 5`` (see the
    ``set_input`` patch op). Pre-roll and the latency it absorbs are trimmed;
    the tail is kept so reverb and delay are part of the analysis.
    """
    di, sr = load(di_path)
    rec = playrec(di.mean(axis=1), sr, **kwargs)
    rec = rec[int(PREROLL_S * sr * 0.8):]
    peak = float(np.max(np.abs(rec)))
    if peak < 1e-4:
        raise SystemExit(
            "the reamp came back silent: run `qctone doctor --reamp-test` to "
            "check the grid routing and the USB channel map"
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
