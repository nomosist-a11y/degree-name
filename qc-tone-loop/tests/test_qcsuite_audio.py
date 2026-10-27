"""Loudness, the fake reamp, alignment, passthrough detection, export."""

import numpy as np
import pytest
import soundfile as sf

from qcsuite.audio import di as di_mod
from qcsuite.audio import export, lufs, passthrough, render
from qctone import features

from helpers import route_usb

SR = 48000


def test_k_weighting_matches_the_standard_at_48k():
    (b1, a1), (b2, a2) = lufs._k_weighting(48000)
    np.testing.assert_allclose(b1, [1.53512485958697, -2.69169618940638, 1.19839281085285],
                               atol=1e-9)
    np.testing.assert_allclose(a1, [1.0, -1.69065929318241, 0.73248077421585], atol=1e-9)
    np.testing.assert_allclose(a2, [1.0, -1.99004745483398, 0.99007225036621], atol=1e-9)


def test_reference_sine_reads_minus_3_01_lufs():
    t = np.arange(SR * 5) / SR
    sine = np.sin(2 * np.pi * 997 * t)
    assert lufs.integrated_loudness(sine, SR) == pytest.approx(-3.01, abs=0.02)
    stereo = np.stack([sine, sine], axis=1) * 10 ** (-20 / 20)
    assert lufs.integrated_loudness(stereo, SR) == pytest.approx(-20.0, abs=0.05)


def test_gating_ignores_silence_and_short_input():
    t = np.arange(SR * 4) / SR
    tone = 0.1 * np.sin(2 * np.pi * 997 * t)
    padded = np.concatenate([tone, np.zeros(SR * 8)])
    # Blocks straddling the end of the tone pass the relative gate, so the
    # padded reading sits a little lower - by design of BS.1770.
    assert lufs.integrated_loudness(padded, SR) == pytest.approx(
        lufs.integrated_loudness(tone, SR), abs=0.3)
    assert lufs.integrated_loudness(np.zeros(SR), SR) == float("-inf")
    assert lufs.integrated_loudness(tone[:1000], SR) == float("-inf")


def test_match_loudness_respects_the_ceiling(di):
    loud = np.clip(di * 1.9, -0.99, 0.99)
    quiet = di * 0.05
    matched, target, gains = export.match_loudness([loud, quiet], SR)
    levels = [lufs.integrated_loudness(x, SR) for x in matched]
    assert abs(levels[0] - levels[1]) < 0.1
    assert all(lufs.true_peak_dbtp(x) <= -0.99 for x in matched)


def test_synthetic_di_reads_like_a_clean_di(di):
    f = features.analyze_array(di[:, None], SR)
    assert f["spectral"]["flatness_1k_5k"] < passthrough.FLATNESS_LIMIT
    assert f["dynamics"]["peak_dbfs"] == pytest.approx(-6.0, abs=0.1)


def test_fake_reamp_is_aligned_to_the_di(rig, device, di):
    device.read_stored("/media/p4/Presets/My Presets", 0)
    route_usb(device)
    result = rig.render(di)
    assert result.mock
    assert result.latency_ms == pytest.approx(render.MEASURED_LATENCY_MS, abs=3.0)
    assert len(result.audio) == len(di) + int(render.TAIL_S * SR)
    onset_di = np.argmax(np.abs(di) > 0.05)
    onset_out = np.argmax(np.abs(result.audio.mean(axis=1)) > 0.05 * np.abs(result.audio).max())
    assert abs(int(onset_out) - int(onset_di)) < SR * 0.01


def test_silent_reamp_says_how_to_fix_routing(rig, device, di):
    device.read_stored("/media/p4/Presets/My Presets", 0)   # row 0 is on INPUT_1
    with pytest.raises(RuntimeError, match="USB 5"):
        rig.render(di)


def test_more_gain_reads_as_more_distortion(rig, device, di):
    device.read_stored("/media/p4/Presets/My Presets", 0)
    route_usb(device)
    grid = device.grid()
    amp = next(b for b in grid["blocks"] if b["category"] == "Guitar Amplifier")
    knob = next(p["name"] for p in amp["params"] if p["name"] in ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1"))
    readings = []
    for value in (0.05, 0.95):
        device.apply_patch({"ops": [{"op": "set_param", "row": 0, "col": amp["col"],
                                     "param": knob, "value": value}]})
        f = features.analyze_array(rig.render(di).audio, SR)
        readings.append((f["spectral"]["flatness_1k_5k"], f["dynamics"]["crest_db"]))
    assert readings[1][0] > readings[0][0]
    assert readings[1][1] < readings[0][1]


def test_unowned_plugin_render_is_flagged_as_passthrough(rig, device, di):
    route_usb(device)
    clear = [{"op": "remove_block", "row": b["row"], "col": b["col"]} for b in device.grid()["blocks"]]
    plini = device.catalog.find("Plini Lead").id
    device.apply_patch({"ops": clear + [{"op": "set_block", "row": 0, "col": 0, "model": plini}]})
    out = features.analyze_array(rig.render(di).audio, SR)
    dry = features.analyze_array(di[:, None], SR)
    assert passthrough.check(out, dry)["passthrough"]
    amp = device.catalog.find("Brit 2203").id
    device.apply_patch({"ops": [{"op": "set_block", "row": 0, "col": 0, "model": amp},
                                {"op": "set_param", "row": 0, "col": 0, "param": "DRIVE",
                                 "value": 0.8}]})
    out = features.analyze_array(rig.render(di).audio, SR)
    assert not passthrough.check(out, dry)["passthrough"]


def test_latency_measurement_on_a_known_delay(di):
    delay = int(0.2 * SR)
    played = np.concatenate([np.zeros(SR // 2), di])
    rec = np.concatenate([np.zeros(delay), np.tanh(4 * played)])[: len(played)]
    assert render.measure_latency(played, rec, SR) == pytest.approx(200.0, abs=0.5)


def test_export_names_and_formats(tmp_path, di):
    assert export.safe_name('a/b:c*"d"  e ') == "a_b_c_d_ e"
    assert export.safe_name("ズトマヨ M&M") == "ズトマヨ M&M"
    stereo = np.stack([di, di], axis=1)
    wav = export.write(tmp_path / "x", stereo, SR, "wav")
    flac = export.write(tmp_path / "y", stereo, SR, "flac")
    assert sf.info(str(wav)).subtype == "PCM_24" and sf.info(str(flac)).format == "FLAC"
    assert sf.info(str(wav)).samplerate == SR and sf.info(str(wav)).channels == 2


def test_di_library_round_trip(tmp_path):
    meta = di_mod.ensure_synthetic(mock=True)
    assert meta["source"] == "synthetic"
    src = tmp_path / "take.wav"
    sf.write(str(src), di_mod.synth_di(3.0, sr=44100), 44100)
    imported = di_mod.import_file(src, "take", mock=True)
    assert imported["seconds"] == pytest.approx(3.0, abs=0.01)
    cut = di_mod.trim("take", True, 0.5, 1.0, "take-cut")
    assert cut["seconds"] == pytest.approx(1.0, abs=0.01)
    names = [m["name"] for m in di_mod.listing(True)]
    assert {"synthetic-riff", "take", "take-cut"} <= set(names)
    data, sr = di_mod.load("take-cut", True)
    assert sr == SR and data.ndim == 1
    with pytest.raises(FileNotFoundError):
        di_mod.load("missing", True)
