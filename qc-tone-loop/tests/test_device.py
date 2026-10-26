"""Patch validation and grid snapshots against a fake unit (no hardware)."""

import pytest

pq = pytest.importorskip("pyquadcortex")
from pyquadcortex.catalog import Model, ModelCatalog, Parameter  # noqa: E402
from pyquadcortex.proto import Preset_pb2  # noqa: E402

from qctone import device  # noqa: E402

AMP = Model(id=1000, name="Brit 2203", category="Guitar Amplifier", category_id=1,
            parameters=(Parameter(0, "GAIN", 0.0, 10.0, 5.0),
                        Parameter(1, "BASS", 0.0, 10.0, 5.0),
                        Parameter(2, "LEVEL", 0.0, 1.0, 0.5, units="dB")))
DRIVE = Model(id=2000, name="Chief DS1", category="Guitar Overdrive", category_id=2,
              parameters=(Parameter(0, "DIST", 0.0, 10.0, 5.0),))


def _catalog():
    c = ModelCatalog()
    c.models = {AMP.id: AMP, DRIVE.id: DRIVE}
    return c


def _preset():
    p = Preset_pb2.BinaryPreset()
    for row in range(4):
        chain = p.chains.add()
        chain.in_portid = 1 if row == 0 else 0
        chain.out_portid = 1 if row == 0 else 0
        bp = p.bypass.add()
        for col in range(8):
            m = chain.models.add()
            sb = bp.colBypass.add()
            for _ in range(8):
                sb.sceneBypass.add().bypass = False
            if row == 0 and col == 2:
                m.hash = AMP.id
                for value in (0.65, 0.5, 0.8):
                    m.params.add().param_values.add().float_value = value
    return p


class FakeQC:
    def __init__(self):
        self.catalog = _catalog()
        self.sent = []

    def read_current_preset(self):
        return _preset()

    def active_scene(self):
        return pq.Scene.A

    def __getattr__(self, name):
        return lambda *a, **k: self.sent.append((name, a, k))


def test_snapshot_reports_blocks_in_real_units():
    grid = device.snapshot(FakeQC())
    assert grid["rows"][0] == {"row": 0, "in_port": "INPUT_1", "out_port": "XLR_1_2"}
    (amp,) = grid["blocks"]
    assert (amp["row"], amp["col"], amp["model"], amp["bypassed"]) == (0, 2, "Brit 2203", False)
    gain = amp["params"][0]
    assert gain["name"] == "GAIN" and gain["real"] == 6.5 and gain["value"] == 0.65
    level = amp["params"][2]
    assert "real" not in level and level["value"] == 0.8  # placeholder range


def test_validate_accepts_and_resolves():
    qc = FakeQC()
    grid = device.snapshot(qc)
    ops = device.validate({"ops": [
        {"op": "set_block", "row": 0, "col": 1, "model": "chief ds1"},
        {"op": "set_param", "row": 0, "col": 1, "param": "DIST", "real": 3},
        {"op": "set_param", "row": 0, "col": 2, "param": "gain", "real": 4.5},
        {"op": "set_input", "row": 0, "port": "USB_5"},
    ]}, grid, qc.catalog)
    assert ops[0]["model_id"] == DRIVE.id
    assert ops[3]["port_id"] == int(pq.Input.USB_5)
    device.apply(qc, ops)
    assert [s[0] for s in qc.sent] == ["set_block", "set_param", "set_param", "set_chain_input"]


@pytest.mark.parametrize("op, message", [
    ({"op": "set_param", "row": 0, "col": 2, "param": "GAIN", "real": 11}, "outside"),
    ({"op": "set_param", "row": 0, "col": 2, "param": "TREBLE", "real": 5}, "no parameter"),
    ({"op": "set_param", "row": 0, "col": 3, "param": "GAIN", "real": 5}, "no block"),
    ({"op": "set_param", "row": 0, "col": 2, "param": "LEVEL", "real": 0.5}, "use 'value'"),
    ({"op": "set_param", "row": 0, "col": 2, "param": "GAIN"}, "exactly one"),
    ({"op": "set_block", "row": 0, "col": 1, "model": "Nonexistent"}, "no model"),
    ({"op": "set_input", "row": 0, "port": "USB_9"}, "port must be"),
    ({"op": "set_param", "row": 4, "col": 2, "param": "GAIN", "real": 5}, "row must be"),
    ({"op": "frobnicate", "row": 0}, "unknown op"),
])
def test_validate_rejects(op, message):
    qc = FakeQC()
    with pytest.raises(device.PatchError, match=message):
        device.validate({"ops": [op]}, device.snapshot(qc), qc.catalog)


def test_restore_ops_round_trip():
    qc = FakeQC()
    saved = device.snapshot(qc)
    current = {
        "rows": [dict(r) for r in saved["rows"]],
        "blocks": [{"row": 0, "col": 5, "model_id": DRIVE.id, "params": [], "bypassed": False}],
    }
    current["rows"][0]["in_port"] = "USB_5"
    patch = device.restore_ops(saved, current)
    kinds = [o["op"] for o in patch["ops"]]
    assert kinds[:3] == ["set_input", "remove_block", "set_block"]
    assert kinds.count("set_param") == 3
    device.validate(patch, current, qc.catalog)
