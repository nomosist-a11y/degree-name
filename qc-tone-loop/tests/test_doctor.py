import importlib

import numpy as np
import pytest

from qctone import audio_io, doctor


def _grid(*rows):
    return {"rows": [{"row": i, "in_port": i_, "out_port": o} for i, (i_, o) in enumerate(rows)],
            "blocks": []}


def test_routing_ok_through_next_row():
    grid = _grid(("USB_5", "NEXT_ROW_3"), ("EMPTY", "EMPTY"), ("EMPTY", "XLR_1_2"))
    (check,) = doctor.routing_checks(grid)
    assert check.status == "ok" and "row 2" in check.detail


def test_routing_warns_without_usb_input():
    (check,) = doctor.routing_checks(_grid(("INPUT_1", "XLR_1_2")))
    assert check.status == "warn" and "USB_5" in check.fix


def test_routing_warns_without_output():
    (check,) = doctor.routing_checks(_grid(("USB_5", "EMPTY")))
    assert check.status == "warn" and "set_output" in check.fix


def test_onset_and_burst():
    sr = 48000
    burst = doctor.test_burst(sr)
    assert abs(20 * np.log10(np.max(np.abs(burst))) + 20) < 0.01
    x = np.concatenate([np.zeros(1000, np.float32), burst])
    assert audio_io.onset(x) == pytest.approx(1000, abs=5)
    assert audio_io.onset(np.zeros(100)) is None


def test_channel_env_overrides(monkeypatch):
    monkeypatch.setenv("QCTONE_WET_IN", "1,2")
    monkeypatch.setenv("QCTONE_REAMP_OUT", "7")
    reloaded = importlib.reload(audio_io)
    try:
        assert reloaded.WET_IN_CHANNELS == (1, 2)
        assert reloaded.REAMP_OUT_CHANNEL == 7
        monkeypatch.setenv("QCTONE_DRY_IN", "0")
        with pytest.raises(SystemExit, match="1-based"):
            importlib.reload(audio_io)
    finally:
        monkeypatch.delenv("QCTONE_WET_IN")
        monkeypatch.delenv("QCTONE_REAMP_OUT")
        monkeypatch.delenv("QCTONE_DRY_IN", raising=False)
        importlib.reload(audio_io)


def test_firmware_check_accepts_reported_id():
    assert doctor.firmware_check("d14e").status == "ok"
    assert doctor.firmware_check("4.0.1").status == "ok"
    assert doctor.firmware_check("e20a").status == "warn"


def test_load_patch_inline_and_file(tmp_path):
    from qctone.cli import load_patch
    inline = '{"ops":[{"op":"set_input","row":0,"port":"USB_5"}]}'
    assert load_patch(inline)["ops"][0]["port"] == "USB_5"
    f = tmp_path / "p.json"
    f.write_text(inline)
    assert load_patch(str(f)) == load_patch(inline)
    with pytest.raises(SystemExit, match="could not read"):
        load_patch("{not json")
    with pytest.raises(SystemExit, match="could not read"):
        load_patch(str(tmp_path / "missing.json"))
