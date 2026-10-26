import numpy as np
import pytest
import soundfile as sf
from scipy import signal

from qctone import compare, features, session

SR = features.SAMPLE_RATE


def _guitarish(seconds=4.0, gain=1.0, seed=0):
    """Plucked-ish harmonic notes, optionally clipped, so features have something to see."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    out = np.zeros_like(t)
    for start, f0 in zip(np.arange(0, seconds, 0.5), [82, 110, 147, 196, 247, 330, 110, 82]):
        env = np.where(t >= start, np.exp(-(t - start) * 3), 0)
        note = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
        out += env * note
    out += 0.001 * rng.standard_normal(len(t))
    out = np.tanh(gain * out) / np.tanh(gain)
    return (0.5 * out / np.max(np.abs(out))).astype(np.float32)


def _lowpass(x, hz):
    sos = signal.butter(4, hz, "low", fs=SR, output="sos")
    return signal.sosfilt(sos, x).astype(np.float32)


def test_curve_is_normalized_and_level_independent():
    x = _guitarish()
    a = features.analyze_array(x[:, None], SR)
    b = features.analyze_array(0.1 * x[:, None], SR)
    np.testing.assert_allclose(a["third_octave"]["db"], b["third_octave"]["db"], atol=0.05)
    centres = np.array(features.THIRD_OCTAVE_CENTRES)
    ref = (centres >= 200) & (centres <= 5000)
    assert abs(np.mean(np.array(a["third_octave"]["db"])[ref])) < 0.01


def test_identical_audio_scores_100():
    f = features.analyze_array(_guitarish()[:, None], SR)
    result = compare.compare(f, f)
    assert result["score"] == 100.0
    assert result["hints"] == []


def test_darker_render_gets_boost_hint_in_highs():
    x = _guitarish(gain=8.0)
    target = features.analyze_array(x[:, None], SR)
    render = features.analyze_array(_lowpass(x, 2500)[:, None], SR)
    result = compare.compare(render, target)
    assert result["score"] < 90
    top = result["mismatch_regions"][0]
    assert top["action"] == "boost" and top["peak_hz"] >= 4000
    assert result["spectral_diff"]["tilt_db_per_oct"] < 0


def test_more_gain_reads_as_more_compressed():
    clean = features.analyze_array(_guitarish(gain=1.0)[:, None], SR)
    driven = features.analyze_array(_guitarish(gain=20.0)[:, None], SR)
    assert driven["dynamics"]["crest_db"] < clean["dynamics"]["crest_db"]


def test_session_init_import_and_evaluate(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "ROOT", tmp_path)
    x = _guitarish(gain=8.0)
    ref = tmp_path / "ref.wav"
    sf.write(ref, np.stack([x, x], axis=1), 44100)  # resampled on load
    session.init("t", ref, "tight high-gain rhythm")
    it = session.new_iteration("t")
    sf.write(it / "render.wav", _lowpass(x, 3000), SR)
    result = session.evaluate("t", it, "first try")
    assert (it / "compare.png").stat().st_size > 10_000
    assert (it / "compare.json").exists()
    assert "| 001 |" in (tmp_path / "t" / "history.md").read_text()
    assert session.best("t")[1] == it
    assert 0 <= result["score"] < 100

    with pytest.raises(SystemExit):
        session.init("t", ref, "again")
