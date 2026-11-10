"""Capture blocks and bypass: two behaviours measured on the player's unit (2026-11-10).

1. A bypass written right after a capture block is placed is dropped; rebuild
   reads the grid back and sends it again.
2. Saving from the computer stores bypassed capture blocks as on; qcsuite cannot
   avoid it and tells the player which blocks to re-bypass on the unit.
"""
import pytest

from qcsuite import presets
from qcsuite.device import core
from qcsuite.device import fake as fake_mod
from qcsuite.device.session import open_rig
from qcsuite.rebuild import rebuild_grid

CAPTURE = 14000


@pytest.fixture
def faulty_rig():
    fake = fake_mod.FakeQuadCortex(fake_mod.FakeConfig(
        catalog_timeouts=0, first_read_dropped=False,
        capture_bypass_needs_settling=True, save_unbypasses_captures=True))
    with open_rig(mock=True, fake=fake) as r:
        yield r


def _capture_grid(rig):
    """Live grid: capture on at column 1, capture bypassed at column 2."""
    fake = rig.fake
    for col in (1, 2):
        fake._place(fake.live, 0, col, CAPTURE)
    for col, state in ((1, False), (2, True)):
        for sb in fake.live.bypass[0].colBypass[col].sceneBypass:
            sb.bypass = state
    return presets.normalize(rig.device.current_preset(), rig.device.catalog)


def _bypassed(rig):
    return {b["col"]: b["bypassed"] for b in rig.device.grid()["blocks"] if b["row"] == 0 and b["col"] in (1, 2)}


def test_rebuild_resends_a_bypass_the_unit_dropped(faulty_rig):
    source = _capture_grid(faulty_rig)
    faulty_rig.fake.live.chains[0].models[1].Clear()     # change the grid so rebuild re-places
    faulty_rig.fake.live.chains[0].models[2].Clear()
    warnings = rebuild_grid(faulty_rig.device, source)
    assert _bypassed(faulty_rig) == {1: False, 2: True}
    assert not [w for w in warnings if "バイパスの書き込み" in w]


def test_saving_a_bypassed_capture_warns_which_block(faulty_rig):
    _capture_grid(faulty_rig)
    d = faulty_rig.device
    key = d.ensure_sandbox()
    d.save_current(key, 0, "capture bypass")
    notes = [n.message for n in d.notices if n.kind == core.INERT_WRITE]
    assert notes and "行1 列3" in notes[-1] and "行1 列2" not in notes[-1]
    stored = presets.normalize(d.read_stored(key, 0), d.catalog)
    assert not any(b["bypass"]["scenes"][0] for b in stored["blocks"] if b["model_id"] == CAPTURE)


def test_no_warning_without_bypassed_captures(rig):
    d = rig.device
    key = d.ensure_sandbox()
    d.save_current(key, 0, "plain")
    assert not [n for n in d.notices if n.kind == core.INERT_WRITE and "キャプチャー" in n.message]
