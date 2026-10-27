"""A small, deterministic stand-in for the Quad Cortex's audio engine.

Only used by the fake unit. It turns :meth:`FakeQuadCortex.audible_chain` - the
blocks as they would SOUND, which is not always what reads back - into audio,
so every feature that reamps can be exercised without hardware and the results
behave plausibly: more gain flattens the 1-5 kHz spectrum and lowers the crest
factor, TREBLE tilts the curve, a cab rolls off the top, reverb widens the
stereo image, a muted lane is silent, and a passthrough block leaves the DI
untouched.

It is NOT a model of any real amp. Numbers that come out of it say whether the
plumbing works, never what a preset sounds like.
"""

from __future__ import annotations

import hashlib

import numpy as np
from scipy import signal

GAIN_NAMES = ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1")


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _biquad(kind: str, f0: float, sr: int, gain_db: float = 0.0, q: float = 0.707):
    """RBJ cookbook biquads as second-order sections."""
    f0 = float(np.clip(f0, 10.0, sr * 0.45))
    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2 * q)
    cos = np.cos(w0)
    if kind == "peak":
        b = [1 + alpha * a, -2 * cos, 1 - alpha * a]
        den = [1 + alpha / a, -2 * cos, 1 - alpha / a]
    elif kind == "lowshelf":
        sq = 2 * np.sqrt(a) * alpha
        b = [a * ((a + 1) - (a - 1) * cos + sq), 2 * a * ((a - 1) - (a + 1) * cos),
             a * ((a + 1) - (a - 1) * cos - sq)]
        den = [(a + 1) + (a - 1) * cos + sq, -2 * ((a - 1) + (a + 1) * cos),
               (a + 1) + (a - 1) * cos - sq]
    elif kind == "highshelf":
        sq = 2 * np.sqrt(a) * alpha
        b = [a * ((a + 1) + (a - 1) * cos + sq), -2 * a * ((a - 1) + (a + 1) * cos),
             a * ((a + 1) + (a - 1) * cos - sq)]
        den = [(a + 1) - (a - 1) * cos + sq, 2 * ((a - 1) - (a + 1) * cos),
               (a + 1) - (a - 1) * cos - sq]
    elif kind == "lowpass":
        b = [(1 - cos) / 2, 1 - cos, (1 - cos) / 2]
        den = [1 + alpha, -2 * cos, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2]
        den = [1 + alpha, -2 * cos, 1 - alpha]
    else:
        raise ValueError(kind)
    b = np.array(b) / den[0]
    den = np.array(den) / den[0]
    return np.concatenate([b, den])[None, :]


def _filt(x, *sections):
    sos = np.vstack(sections)
    return signal.sosfilt(sos, x, axis=0)


class Params:
    """Normalized knob values with real-unit conversion through the catalog."""

    def __init__(self, block: dict, catalog):
        self.values = block.get("params", {})
        self.model = catalog.get(block["model_id"]) if catalog is not None else None

    def norm(self, name, default=0.5) -> float:
        v = self.values.get(name)
        return default if v is None or v != v else float(v)

    def real(self, name, default):
        v = self.values.get(name)
        if v is None or v != v or self.model is None:
            return default
        try:
            spec = self.model.parameter(name)
        except KeyError:
            return default
        if spec.range_is_placeholder or spec.option_count:
            return default
        return spec.to_real(v)

    def first(self, names, default=0.5):
        for n in names:
            if n in self.values:
                return self.norm(n)
        return default


def _amp(x, block, catalog, sr, fixed_gain=None, character=0):
    p = Params(block, catalog)
    gain = p.first(GAIN_NAMES) if fixed_gain is None else fixed_gain
    if "GAIN 2" in p.values:
        gain = min(1.0, gain * (0.6 + 0.8 * p.norm("GAIN 2")))
    rng = np.random.default_rng(character)
    mid_f = 500 + rng.uniform(0, 600)
    x = _filt(x, _biquad("highpass", 90, sr), _biquad("peak", mid_f, sr, 4.0, 0.7))
    drive = 10 ** ((4 + 40 * gain) / 20)
    bias = 0.08 * (character % 3) / 2
    x = np.tanh(drive * x + bias) - np.tanh(bias)
    x /= max(np.tanh(drive * 0.25), 1e-3)
    tone = [
        _biquad("lowshelf", 120, sr, 20 * (p.first(("BASS",)) - 0.5)),
        _biquad("peak", 700, sr, 16 * (p.first(("MID", "MIDDLE")) - 0.5), 0.8),
        _biquad("highshelf", 3000, sr, 20 * (p.first(("TREBLE",)) - 0.5)),
        _biquad("peak", 5000, sr, 12 * (p.first(("PRESENCE",)) - 0.5), 0.9),
        _biquad("peak", 90, sr, 12 * (p.first(("DEPTH", "RESONANCE")) - 0.5), 1.0),
        _biquad("lowpass", 9000, sr),
    ]
    if p.norm("BRIGHT", 0.0) >= 0.5:
        tone.append(_biquad("highshelf", 2000, sr, 3.0))
    x = _filt(x, *tone)
    level_db = 24 * (p.first(("MASTER", "VOLUME"), 0.5) - 0.5) - 12
    return x * 10 ** (level_db / 20)


def _drive(x, block, catalog, sr):
    p = Params(block, catalog)
    gain = p.first(("DRIVE", "GAIN"))
    x = np.tanh(x * 10 ** ((30 * gain) / 20)) * 0.5
    x = _filt(x, _biquad("highshelf", 1500, sr, 12 * (p.norm("TONE") - 0.5)))
    return x * 10 ** ((12 * (p.norm("LEVEL") - 0.5)) / 20)


def _cab(x, block, catalog, sr):
    p = Params(block, catalog)
    character = _seed(block["model_id"])
    lp = 4200 + character % 2500
    mic = block.get("texts", {}).get("ir selector", "")
    mic_peak = 2000 + _seed(mic) % 2500
    return _filt(
        x,
        _biquad("highpass", p.real("LOW CUT", 80.0), sr, q=0.8),
        _biquad("peak", 110 + character % 40, sr, 4.0, 1.2),
        _biquad("peak", mic_peak, sr, 3.0 if "Condenser" in mic else -2.0, 1.4),
        _biquad("lowpass", min(lp, p.real("HIGH CUT", 9000.0)), sr, q=0.9),
        _biquad("lowpass", lp * 1.1, sr, q=0.6),
    ) * 10 ** (p.real("LEVEL", 0.0) / 20)


def _eq(x, block, catalog, sr):
    p = Params(block, catalog)
    names = set(p.values)
    if "LOW FREQ" in names:
        x = _filt(x,
                  _biquad("lowshelf", p.real("LOW FREQ", 100), sr, p.real("LOW GAIN", 0)),
                  _biquad("peak", p.real("MID FREQ", 800), sr, p.real("MID GAIN", 0),
                          p.real("MID Q", 1.0)),
                  _biquad("highshelf", p.real("HIGH FREQ", 4000), sr, p.real("HIGH GAIN", 0)))
        return x * 10 ** (p.real("LEVEL", 0.0) / 20)
    if "LOW CUT" in names:
        return _filt(x, _biquad("highpass", p.real("LOW CUT", 20), sr),
                     _biquad("lowpass", p.real("HIGH CUT", 20000), sr))
    bands = [(63, "63 HZ"), (125, "125 HZ"), (250, "250 HZ"), (500, "500 HZ"),
             (1000, "1K HZ"), (2000, "2K HZ"), (4000, "4K HZ"), (8000, "8K HZ"),
             (16000, "16K HZ")]
    sections = [_biquad("peak", f, sr, p.real(n, 0.0), 1.4) for f, n in bands if n in names]
    if sections:
        x = _filt(x, *sections)
    return x * 10 ** (p.real("LEVEL", 0.0) / 20)


def _delay(x, block, catalog, sr):
    p = Params(block, catalog)
    d = max(1, int(p.real("TIME", 350.0) / 1000 * sr))
    fb = min(0.9, p.real("FEEDBACK", 30.0) / 100)
    mix = p.real("MIX", 25.0) / 100
    a = np.zeros(d + 1)
    a[0], a[d] = 1.0, -fb
    b = np.zeros(d + 1)
    b[d] = 1.0
    wet = signal.lfilter(b, a, x, axis=0)
    return x * (1 - mix * 0.5) + wet * mix


def _reverb(x, block, catalog, sr):
    p = Params(block, catalog)
    decay = float(np.clip(p.real("DECAY", 2.0), 0.1, 4.0))
    mix = p.real("MIX", 20.0) / 100
    pre = int(p.real("PREDELAY", 20.0) / 1000 * sr)
    n = int(decay * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(_seed(block["model_id"], "ir"))
    env = np.exp(-6.9 * t / decay)
    ir = rng.standard_normal((n, 2)) * env[:, None]
    ir = _filt(ir, _biquad("lowpass", p.real("HIGH CUT", 8000.0), sr))
    ir /= np.sqrt(np.sum(ir ** 2, axis=0, keepdims=True))
    ir = np.vstack([np.zeros((pre, 2)), ir])
    mono = x.mean(axis=1) if x.ndim == 2 else x
    wet = np.stack([signal.fftconvolve(mono, ir[:, c])[: len(mono)] for c in range(2)], axis=1)
    dry = x if x.ndim == 2 else np.stack([x, x], axis=1)
    return dry * (1 - mix * 0.5) + wet * mix


def _compressor(x, block, catalog, sr):
    p = Params(block, catalog)
    threshold = p.real("THRESHOLD", -20.0)
    ratio = max(1.0, p.real("RATIO", 4.0))
    mono = np.abs(x if x.ndim == 1 else x.mean(axis=1))
    env = signal.lfilter([0.002], [1, -0.998], mono)
    level = 20 * np.log10(np.maximum(env, 1e-9))
    over = np.maximum(level - threshold, 0.0)
    gain = 10 ** ((-over * (1 - 1 / ratio) + p.real("LEVEL", 0.0)) / 20)
    return x * (gain if x.ndim == 1 else gain[:, None])


def _modulation(x, block, catalog, sr):
    p = Params(block, catalog)
    mix = p.real("MIX", 50.0) / 100
    if block["model_id"] in (7004, 7036, 7040):         # tremolos
        rate = p.real("RATE", 4.0)
        depth = p.real("DEPTH", 50.0) / 100
        t = np.arange(len(x)) / sr
        lfo = 1 - depth * 0.5 * (1 + np.sin(2 * np.pi * rate * t))
        return x * (lfo if x.ndim == 1 else lfo[:, None])
    d = int(0.007 * sr)
    shifted = np.concatenate([np.zeros((d,) + x.shape[1:]), x[:-d]])
    return x * (1 - mix * 0.5) + shifted * mix * 0.5


def _utility(x, block, catalog, sr):
    p = Params(block, catalog)
    if "GAIN" in p.values:
        return x * 10 ** (p.real("GAIN", 0.0) / 20)
    if "VOLUME" in p.values:
        return x * p.real("VOLUME", 100.0) / 100
    return x


def _block(x, block, catalog, sr):
    if block.get("bypassed") or block.get("passthrough"):
        return x
    category = block.get("category", "")
    stereo = x.ndim == 2
    if category in ("Reverb",):
        return _reverb(x, block, catalog, sr)
    mono_in = x.mean(axis=1) if stereo else x
    if category in ("Guitar Amplifier", "Bass Amplifier"):
        y = _amp(mono_in, block, catalog, sr, character=_seed(block["model_id"]))
    elif category in ("Neural Capture", "Neural Capture V2"):
        name = block.get("texts", {}).get("file_name", "")[64:]
        y = _amp(mono_in, block, catalog, sr, fixed_gain=0.2 + (_seed(name) % 70) / 100,
                 character=_seed(name))
    elif category in ("Guitar Overdrive", "Bass Overdrive"):
        y = _drive(mono_in, block, catalog, sr)
    elif category.startswith("Cabsim") or category == "IRLoaders":
        y = _cab(mono_in, block, catalog, sr)
    elif category == "Equalizer":
        return _eq(x, block, catalog, sr)
    elif category == "Delay":
        return _delay(x, block, catalog, sr)
    elif category == "Compressor":
        return _compressor(x, block, catalog, sr)
    elif category == "Modulation":
        return _modulation(x, block, catalog, sr)
    elif category == "Utility":
        return _utility(x, block, catalog, sr)
    else:
        return x
    return np.stack([y, y], axis=1) if stereo else y


_FEEDS_NEXT = {"NEXT_ROW_3": [2], "NEXT_ROW_4": [3], "NEXT_ROW_3_4": [2, 3]}
_REACHES_OUT = {"XLR_1_2", "XLR_1", "XLR_2", "MULTIPLE"}


def process(chain: dict, mono, sr: int, catalog, input_ports=("USB_5", "USB_5_6")):
    """Run ``mono`` through the rows fed from ``input_ports``; return (n, 2)."""
    mono = np.asarray(mono, dtype=np.float64)
    rows = {r["row"]: r for r in chain["rows"]}
    out = np.zeros((len(mono), 2))

    def run_row(row, x, depth=0):
        r = rows[row]
        for block in r["blocks"]:
            x = _block(x, block, catalog, sr)
        lane = r.get("lane", {})
        if lane.get("MUTE", 0.0) >= 0.5:
            return
        volume = lane.get("VOLUME")
        if volume is not None:
            x = x * (0.0 if volume <= 0 else 10 ** ((-40 + 52 * volume) / 20))
        if r["out_port"] in _REACHES_OUT:
            nonlocal out
            out = out + (x if x.ndim == 2 else np.stack([x, x], axis=1))
        elif r["out_port"] in _FEEDS_NEXT and depth < 4:
            for nxt in _FEEDS_NEXT[r["out_port"]]:
                if nxt in rows:
                    run_row(nxt, x, depth + 1)

    for r in chain["rows"]:
        if r["in_port"] in input_ports:
            run_row(r["row"], mono.copy())
    return out
