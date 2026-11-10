"""Reamping: play a DI into the unit, record what comes back, line it up.

Two interchangeable renderers sit behind :func:`render`:

* :class:`ReampRenderer` - the real path, through qctone's ``playrec``: the
  computer plays the DI on USB 5 into the grid row whose input is ``USB 5``, and
  records Out 1/2 back on USB 3/4 (the channel map qctone documents and the
  ``QCTONE_*`` variables override). **The unit makes sound while this runs** -
  callers must warn the player to turn monitors down first
  (:data:`MONITOR_WARNING`).
* :class:`FakeRenderer` - the fake unit's grid through :mod:`qcsuite.audio.dsp`,
  delayed by the round trip measured on the player's unit (about 312 ms) and
  given a noise floor, so the alignment code below runs on both paths.

Playing takes (:func:`record_take`) are the other direction: the player plays,
and one input stream records USB 1 (Input 1, dry) and USB 3/4 (Out 1/2, wet)
together - confirmed on the player's unit with ``mapping=[1, 3, 4]``. In that
case dry and wet are only apart by the unit's internal processing (a few ms);
the ~312 ms round trip applies to reamping alone. For the wet side to carry
the playing, the row must take its input from INPUT_1 - during a reamp it is
USB 5 - so a take refuses to start when no row is fed from Input 1.

Alignment: the raw recording starts with qctone's 0.5 s pre-roll plus the round
trip. :func:`measure_latency` finds the round trip from the first arrival of
sound (the first sample within 35 dB of each signal's peak), sharpens it with a
waveform correlation a few ms either side, and falls back to correlating the
envelopes when there is no clear onset. The result is trimmed so sample 0
lines up with the DI's sample 0 - which is what stems imported into a DAW need.
(Correlating envelopes alone drifted by 30-80 ms on high-gain chains with a
delay block on the fake, because the repeats pull the peak late.)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
from scipy import signal

from qcsuite.audio import dsp, lufs

SR = 48000
PREROLL_S = 0.5
TAIL_S = 2.0
#: Round trip measured on the player's unit: computer -> USB 5 -> grid ->
#: Out 1/2 -> USB 3/4 -> computer.
MEASURED_LATENCY_MS = 312.0
MONITOR_WARNING = ("リアンプ中は Quad Cortex から音が出ます。始める前に、モニターと"
                   "ヘッドホンの音量を下げてください。")


@dataclass
class RenderResult:
    audio: np.ndarray            # (samples, 2) float32, aligned to the DI
    sr: int
    latency_ms: float
    peak_dbfs: float
    clipped: bool
    mock: bool

    def summary(self) -> dict:
        return {"latency_ms": round(float(self.latency_ms), 1),
                "peak_dbfs": round(float(self.peak_dbfs), 2),
                "clipped": bool(self.clipped), "mock": self.mock,
                "seconds": round(len(self.audio) / self.sr, 2)}


class FakeRenderer:
    """Renders the fake unit's live grid. Deterministic for a given grid and DI."""

    mock = True

    def __init__(self, fake, latency_ms: float = MEASURED_LATENCY_MS, noise_dbfs: float = -96.0,
                 input_noise_dbfs: float | None = None):
        self.fake = fake
        self.latency_ms = latency_ms
        self.noise_dbfs = noise_dbfs
        #: Noise entering the grid with the DI (pickup and USB noise). A high-gain
        #: amp raises it far above ``noise_dbfs``, as on the unit, where it reached
        #: the onset threshold before the first note arrived (2026-10-28).
        self.input_noise_dbfs = input_noise_dbfs
        self.renders = 0

    def playrec(self, mono, sr: int = SR) -> np.ndarray:
        pre = np.zeros(int(PREROLL_S * sr))
        tail = np.zeros(int(TAIL_S * sr))
        play = np.concatenate([pre, np.asarray(mono, dtype=np.float64), tail])
        source = play
        if self.input_noise_dbfs is not None:
            hiss = np.random.default_rng(7).standard_normal(len(play))
            source = play + hiss * 10 ** (self.input_noise_dbfs / 20)
        wet = dsp.process(self.fake.audible_chain(), source, sr, self.fake._catalog_obj)
        delay = int(self.latency_ms / 1000 * sr)
        wet = np.vstack([np.zeros((delay, 2)), wet])[: len(play)]
        rng = np.random.default_rng(len(play))
        wet = wet + rng.standard_normal(wet.shape) * 10 ** (self.noise_dbfs / 20)
        self.renders += 1
        return np.clip(wet, -1.0, 1.0).astype(np.float32)


class ReampRenderer:
    """The real unit, through qctone's USB audio path."""

    mock = False

    def __init__(self, **playrec_kwargs):
        self.kwargs = playrec_kwargs
        self.renders = 0

    def playrec(self, mono, sr: int = SR) -> np.ndarray:
        from qctone import audio_io
        rec = audio_io.playrec(np.asarray(mono, dtype=np.float32), sr, **self.kwargs)
        self.renders += 1
        return np.asarray(rec, dtype=np.float32)


def _envelope(x, sr, smooth_ms=5.0):
    mono = np.abs(x if x.ndim == 1 else x.mean(axis=1))
    n = max(1, int(smooth_ms / 1000 * sr))
    return signal.lfilter(np.ones(n) / n, [1.0], mono)


ONSET_THRESHOLD_DB = -35.0
#: How far above the recording's own noise the arrival threshold must sit (x2 = +6 dB).
NOISE_MARGIN = 2.0


def first_arrival_ms(played, recorded, sr: int, noise_until: int | None = None) -> float | None:
    """Delay between the first sample of each signal within 35 dB of its peak.

    ``noise_until`` is a sample index of ``recorded`` before which nothing that
    was played can have arrived (the pre-roll: playback has not started yet).
    The recording's peak there is its noise floor, and the arrival threshold is
    raised to sit above it. Without this, a high-gain amp's hiss crosses the
    -35 dB line before the first note: on the player's unit that put the
    "arrival" ~70 ms before the physical 313.5 ms round trip (2026-10-28).
    """
    played = np.asarray(played, dtype=np.float64)
    rec = np.asarray(recorded.mean(axis=1) if np.ndim(recorded) == 2 else recorded, dtype=np.float64)
    a = _onset_above(played, 0.0, 0)
    floor, start = 0.0, 0
    if noise_until:
        start = min(int(noise_until), len(rec))
        floor = float(np.max(np.abs(rec[:start]))) if start else 0.0
    b = _onset_above(rec, floor * NOISE_MARGIN, start)
    if a is None or b is None or b < a:
        return None
    return (b - a) / sr * 1000


def _onset_above(x, floor: float, start: int) -> int | None:
    """First sample at or after ``start`` within 35 dB of the peak and above ``floor``."""
    level = np.abs(x[start:])
    peak = float(level.max()) if len(level) else 0.0
    if peak < 1e-6:
        return None
    threshold = max(peak * 10 ** (ONSET_THRESHOLD_DB / 20), floor)
    if threshold >= peak:
        return None                       # nothing stands out of the noise
    return start + int(np.argmax(level >= threshold))


def measure_latency(played, recorded, sr: int, max_ms: float = 1500.0,
                    noise_until: int | None = None) -> float | None:
    """Round trip in ms between ``played`` and ``recorded`` (both start together).

    The first arrival (see :func:`first_arrival_ms`, noise-aware when
    ``noise_until`` is given) sharpened by a short waveform correlation; the
    envelope cross-correlation is only the fallback, because distortion pulls
    it early (~50 ms on a high-gain amp on the player's unit).
    """
    arrival = first_arrival_ms(played, recorded, sr, noise_until=noise_until)
    if arrival is not None and arrival <= max_ms:
        lag = _refine(np.asarray(played, dtype=np.float64), np.asarray(recorded, dtype=np.float64),
                      int(round(arrival / 1000 * sr)), sr, window_ms=3.0)
        return lag / sr * 1000
    return _envelope_latency(played, recorded, sr, max_ms)


def _envelope_latency(played, recorded, sr: int, max_ms: float) -> float | None:
    a = _envelope(np.asarray(played, dtype=np.float64), sr)
    b = _envelope(np.asarray(recorded, dtype=np.float64), sr)
    if b.max() < 1e-5 or a.max() < 1e-5:
        return None
    hop = 4                                   # 12 kHz resolution is plenty for envelopes
    a, b = a[::hop] - a[::hop].mean(), b[::hop] - b[::hop].mean()
    corr = signal.correlate(b, a, mode="full", method="fft")
    lags = signal.correlation_lags(len(b), len(a), mode="full")
    keep = (lags >= 0) & (lags <= max_ms / 1000 * sr / hop)
    if not keep.any():
        return None
    coarse = int(lags[keep][np.argmax(corr[keep])] * hop)
    return _refine(np.asarray(played, dtype=np.float64), np.asarray(recorded, dtype=np.float64),
                   coarse, sr) / sr * 1000


def _refine(played, recorded, coarse: int, sr: int, window_ms: float = 20.0) -> int:
    """Sharpen an envelope lag with a waveform correlation around it.

    Envelopes are robust to distortion but smear by a few ms; the waveforms
    themselves still correlate at the fundamental, which pins the lag to the
    sample. The search stays within ``window_ms`` of the coarse answer.
    """
    rec = recorded if recorded.ndim == 1 else recorded.mean(axis=1)
    w = int(window_ms / 1000 * sr)
    n = min(len(played), len(rec) - coarse - w) - w
    if n < sr // 10:
        return coarse
    a = played[w:w + n]
    best, best_lag = -np.inf, coarse
    seg = rec[coarse:coarse + n + 2 * w]
    corr = signal.correlate(seg, a, mode="valid", method="fft")
    if len(corr):
        best_lag = coarse - w + int(np.argmax(np.abs(corr)))
        best = corr.max()
    return best_lag if np.isfinite(best) else coarse


def fixed_latency_ms() -> float | None:
    """``QCSUITE_LATENCY_MS`` overrides measurement (e.g. after a calibration)."""
    raw = os.environ.get("QCSUITE_LATENCY_MS")
    return float(raw) if raw else None


#: Calibration burst: short, loud and abrupt, so its arrival is unambiguous
#: through any grid - distortion, a gate, hiss - unlike the DI's soft attack.
CAL_BURST_MS = 5.0
CAL_BURST_DBFS = -12.0
CAL_REPEATS = 3
CAL_SPREAD_MS = 1.0


def calibrate(renderer, sr: int = SR, device=None) -> float | None:
    """Measure the round trip with a test burst; cached on the renderer.

    Within one session the round trip is constant to the sample, but it is
    not a fixed number: 313.5 ms on 2026-10-28, 265.35 ms on 2026-11-10 (same
    unit, clean path both times). And it cannot be read off the DI: a
    high-gain amp lifts the quiet start of the DI (and hiss) above the onset
    threshold, which put DI-based estimates 50-70 ms early. So each process
    calibrates once - ``CAL_REPEATS`` bursts, median, refused if they spread
    more than ``CAL_SPREAD_MS`` - and aligns every render with that.

    With ``device``, the bursts go through a CLEAN grid: every active block is
    bypassed for the measurement and put back afterwards (read back, resent if
    the unit dropped it). Through the real grid an amp and a noise gate delay
    the burst's onset by 5-7 ms and make it jitter (measured 2026-11-10).
    Unsaved, like every grid edit. Plays a short beep.
    """
    cached = getattr(renderer, "calibrated_latency_ms", None)
    if cached is not None:
        return cached
    if device is not None:
        return _calibrate_clean(renderer, sr, device)
    return _calibrate_bursts(renderer, sr)


def _calibrate_clean(renderer, sr: int, device) -> float | None:
    from qcsuite.rebuild import _settle_bypass
    active = [b for b in device.grid()["blocks"] if not b.get("bypassed")]
    off = [{"op": "bypass", "row": b["row"], "col": b["col"], "bypassed": True} for b in active]
    back = [{"op": "bypass", "row": b["row"], "col": b["col"], "bypassed": False} for b in active]
    if off:
        device.apply_patch({"note": "latency calibration: clean path", "ops": off})
        _settle_bypass(device, off)
    try:
        value = _calibrate_bursts(renderer, sr)
    finally:
        if back:
            device.apply_patch({"note": "latency calibration: put the grid back", "ops": back})
            left = _settle_bypass(device, back)
            for w in left:
                device.notice("info", f"遅延の較正のあと、バイパスを元に戻せませんでした: {w}")
    return value


def _calibrate_bursts(renderer, sr: int) -> float | None:
    t = np.arange(int(CAL_BURST_MS / 1000 * sr)) / sr
    burst = (10 ** (CAL_BURST_DBFS / 20) * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    padded = np.concatenate([np.zeros(int(0.05 * sr), dtype=np.float32), burst,
                             np.zeros(int(LATENCY_MARGIN_S * sr), dtype=np.float32)])
    pre = int(PREROLL_S * sr)
    played = np.concatenate([np.zeros(pre), padded, np.zeros(int(TAIL_S * sr))])
    found = []
    for _ in range(CAL_REPEATS):
        rec = renderer.playrec(padded, sr)
        ms = first_arrival_ms(played, rec, sr, noise_until=pre)
        if ms is None:
            return None
        found.append(ms)
    if max(found) - min(found) > CAL_SPREAD_MS:
        return None
    value = float(np.median(found))
    renderer.calibrated_latency_ms = value
    return value


#: Extra silence played after the DI so the recording still holds the full tail
#: once the round trip has been trimmed off the front.
LATENCY_MARGIN_S = 1.0


def render(renderer, di, sr: int = SR, latency_ms: float | None = None,
           device=None) -> RenderResult:
    """Reamp ``di`` (mono or (n, ch)) and return audio aligned to it.

    The result is ``len(di) + TAIL_S`` long and starts at the DI's sample 0.
    """
    di = np.asarray(di, dtype=np.float32)
    mono = di.mean(axis=1) if di.ndim == 2 else di
    padded = np.concatenate([mono, np.zeros(int(LATENCY_MARGIN_S * sr), dtype=np.float32)])
    if latency_ms is None and fixed_latency_ms() is None:
        calibrate(renderer, sr, device=device)
    rec = renderer.playrec(padded, sr)
    if rec.ndim == 1:
        rec = np.stack([rec, rec], axis=1)
    if rec.shape[1] == 1:
        rec = np.repeat(rec, 2, axis=1)
    peak = float(np.max(np.abs(rec))) if rec.size else 0.0
    if peak < 1e-4:
        raise RuntimeError(
            "リアンプの戻りが無音でした。グリッドの行の入力が USB 5 になっているか、"
            "出力が Out 1/2 に届いているかを確認してください(qctone doctor --reamp-test)。")
    pre = int(PREROLL_S * sr)
    played = np.concatenate([np.zeros(pre), padded, np.zeros(int(TAIL_S * sr))])
    latency = latency_ms if latency_ms is not None else fixed_latency_ms()
    if latency is None:
        latency = getattr(renderer, "calibrated_latency_ms", None)
    if latency is None:
        measured = measure_latency(played, rec, sr, noise_until=pre)
        latency = measured if measured is not None else MEASURED_LATENCY_MS
    start = pre + int(round(latency / 1000 * sr))
    aligned = rec[start: start + len(mono) + int(TAIL_S * sr)]
    return RenderResult(audio=aligned.astype(np.float32), sr=sr, latency_ms=latency,
                        peak_dbfs=lufs.sample_peak_dbfs(rec), clipped=peak >= 0.999,
                        mock=renderer.mock)


# -- playing takes: dry and wet in one stream ------------------------------------

#: Dry-to-wet offset inside the unit. Only "a few ms" is known; the fake uses
#: this guess, the real path measures it per take.
FAKE_INTERNAL_LATENCY_MS = 3.0
GUITAR_INPUTS = ("INPUT_1", "INPUT_1_2")


@dataclass
class Take:
    dry: np.ndarray             # (samples,) Input 1 as played
    wet: np.ndarray             # (samples, 2) Out 1/2
    sr: int
    offset_ms: float | None     # measured wet-after-dry delay
    mock: bool

    def summary(self) -> dict:
        return {"seconds": round(len(self.dry) / self.sr, 2), "mock": self.mock,
                "offset_ms": None if self.offset_ms is None else round(float(self.offset_ms), 1),
                "dry_peak_dbfs": round(float(lufs.sample_peak_dbfs(self.dry)), 2),
                "wet_peak_dbfs": round(float(lufs.sample_peak_dbfs(self.wet)), 2)}


def _fake_take(fake, seconds: float, sr: int, performance=None):
    from qcsuite.audio import di as di_mod
    dry = np.asarray(performance if performance is not None else di_mod.synth_di(seconds, sr),
                     dtype=np.float64)[: int(seconds * sr)]
    wet = dsp.process(fake.audible_chain(), dry, sr, fake._catalog_obj, input_ports=GUITAR_INPUTS)
    delay = int(FAKE_INTERNAL_LATENCY_MS / 1000 * sr)
    wet = np.vstack([np.zeros((delay, 2)), wet])[: len(dry)]
    return dry.astype(np.float32), np.clip(wet, -1, 1).astype(np.float32)


def _real_take(seconds: float, sr: int):
    from qctone import audio_io
    sd = audio_io._sd()
    device = audio_io.find_device()
    mapping = [audio_io.DRY_IN_CHANNEL, *audio_io.WET_IN_CHANNELS]
    rec = sd.rec(int(seconds * sr), samplerate=sr, device=device, mapping=mapping,
                 dtype="float32", blocking=True)
    return rec[:, 0].copy(), rec[:, 1:3].copy()


def record_take(rig, seconds: float, sr: int = SR, performance=None) -> Take:
    """Record the player: Input 1 dry and Out 1/2 wet, in one stream.

    ``performance`` (mock only) is the DI the fake "plays"; it defaults to the
    synthetic riff. Raises when no grid row is fed from Input 1, because the
    wet side would then be silent or carry something else.
    """
    grid = rig.device.grid()
    if not any(r["in_port"] in GUITAR_INPUTS for r in grid["rows"]):
        raise RuntimeError(
            "弾いた音をウェットとして録るには、行の入力が INPUT_1 である必要があります"
            "(リアンプの後は USB_5 のままのことがあります)。プリセットを呼び出し直すか、"
            "行の入力を INPUT_1 に戻してください。")
    if rig.mock:
        dry, wet = _fake_take(rig.fake, seconds, sr, performance)
    else:
        dry, wet = _real_take(seconds, sr)
    offset = measure_latency(dry, wet, sr, max_ms=50.0)
    return Take(dry=dry, wet=wet, sr=sr, offset_ms=offset, mock=rig.mock)
