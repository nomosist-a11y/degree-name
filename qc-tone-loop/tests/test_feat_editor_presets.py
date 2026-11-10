"""Editor: switching presets, following changes made on the unit, and the review fixes
(undo guards, swap staging, switches never written by value)."""

import threading

import pytest

from qcsuite.device import core
from qcsuite.features import editor, modelrepo, panel

MY = "/media/p4/Presets/My Presets"
AMP, CAB, EQ = 1038, 12001, 4001


@pytest.fixture(autouse=True)
def fresh_repo_memory():
    modelrepo.forget()
    yield
    modelrepo.forget()


@pytest.fixture
def s():
    return editor.EditorSession()


def layout(device):
    return {(b["row"], b["col"]): b["model_id"] for b in device.grid()["blocks"]}


# -- presets -------------------------------------------------------------------------------

def test_recall_by_slot_loads_and_stops_the_undo_history(device, fake, s):
    s.layout(device, "delete", {"row": 0, "col": 2, "model_id": EQ})
    fake.dirty = False                       # pretend it was saved: no question asked
    r = s.recall(device, {"folder": MY, "position": 1})
    assert r["loaded"] and r["name"] == "Two Rock Mayer" and r["slot"] == "1B"
    assert fake.loaded_from == (MY, 1)
    with pytest.raises(panel.PanelError):
        panel.undo(device)                   # the old preset's change must not touch this one


def test_recall_asks_first_only_with_unsaved_edits(device, fake, s):
    s.set(device, {"row": 0, "col": 0, "model_id": AMP, "param": "BASS", "real": 7.0})
    assert device.preset_dirty()
    r = s.recall(device, {"folder": MY, "position": 2})
    assert r["needs_confirm"] and "消えます" in r["message"]
    assert fake.loaded_from != (MY, 2)                         # nothing loaded yet
    assert s.recall(device, {"folder": MY, "position": 2, "confirm": True})["loaded"]


def test_step_moves_through_the_setlist_and_wraps(device, fake, s):
    s.recall(device, {"folder": MY, "position": 0, "confirm": True})
    assert s.recall(device, {"step": 1, "confirm": True})["position"] == 1
    assert s.recall(device, {"step": -1, "confirm": True})["position"] == 0
    last = max(p["index"] for p in device.presets(MY))
    assert s.recall(device, {"step": -1, "confirm": True})["position"] == last


def test_step_without_a_known_slot(device, fake, s, monkeypatch):
    fake.loaded_from = None
    monkeypatch.setattr(device, "_slot", None)
    with pytest.raises(editor.EditorError):
        s.recall(device, {"step": 1})


def test_recall_refuses_empty_slots_and_bad_input(device, s):
    with pytest.raises(editor.EditorError):
        s.recall(device, {"folder": MY, "position": 250})
    with pytest.raises(editor.EditorError):
        s.recall(device, {"folder": "", "position": 0})


def test_preset_list(device, s):
    s.recall(device, {"folder": MY, "position": 3, "confirm": True})
    data = editor.preset_list(device)
    assert data["folder"] == MY and data["current"]["slot"] == "1D"
    assert data["presets"][1] == {"position": 1, "slot": "1B", "name": "Two Rock Mayer"}
    assert any(f["kind"] == "factory" for f in data["setlists"])


# -- following the unit --------------------------------------------------------------------

def test_changes_move_when_the_unit_changes(device, fake):
    first = device.changes()
    assert first["mode"] == "push" and device.changes()["seq"] == first["seq"]   # quiet
    fake.set_param(0, 0, param="BASS", model=fake.catalog[AMP], value=0.9)       # a knob on the unit
    assert device.changes()["seq"] > first["seq"]
    seq = device.changes()["seq"]
    fake.switch_scene(3)                                                         # a footswitch
    assert device.changes()["seq"] > seq
    fake.recall_preset(MY, 4)                                                    # a preset on the unit
    c = device.changes()
    assert c["slot"]["slot"] == "1E" and c["slot"]["folder_name"] == "My Presets"
    v = editor.view(device)
    assert v["preset"]["name"] == "YNGWIE MALMSTEEN" and v["scene"] == "A"


def test_view_follows_a_knob_turned_on_the_unit(device, fake):
    before = next(p for p in editor.view(device)["rows"][0]["cells"][0]["params"] if p["name"] == "BASS")
    fake.set_param(0, 0, param="BASS", model=fake.catalog[AMP], value=0.95)
    after = next(p for p in editor.view(device)["rows"][0]["cells"][0]["params"] if p["name"] == "BASS")
    assert after["value"] != before["value"]


def test_a_write_based_on_a_stale_page_is_refused_after_a_unit_edit(device, fake, s):
    shown = next(p for p in editor.view(device)["rows"][0]["cells"][0]["params"] if p["name"] == "BASS")
    fake.set_param(0, 0, param="BASS", model=fake.catalog[AMP], value=0.95)
    with pytest.raises(editor.EditorConflict):
        s.set(device, {"row": 0, "col": 0, "model_id": AMP, "param": "BASS", "real": 2.0,
                       "expect": shown["value"]})


def test_undo_refused_after_the_unit_loaded_another_preset(device, fake, s):
    s.layout(device, "delete", {"row": 0, "col": 2, "model_id": EQ})
    fake.recall_preset(MY, 1)
    with pytest.raises(panel.PanelError, match="違います"):
        panel.undo(device)


def test_undo_refused_in_another_scene(device, fake, s):
    s.layout(device, "delete", {"row": 0, "col": 2, "model_id": EQ})
    s.scene(device, {"scene": 1})
    with pytest.raises(panel.PanelError, match="シーン A"):
        panel.undo(device)
    s.scene(device, {"scene": 0})
    panel.undo(device)
    assert layout(device)[(0, 2)] == EQ


def test_push_tracker_counts_pushes_through_transport_collectors():
    class Grid:                      # stand-ins named like the protobuf classes
        pass

    class Transport:
        def __init__(self):
            self._lock = threading.Lock()
            self._collectors = []

    class QC:
        _t = Transport()

    from pyquadcortex.proto import ProductionAutomation_pb2 as pa
    tracker = core._PushTracker.attach(QC, ["GridMessage", "SetlistPositionMessage", "Nope"])
    assert tracker and len(QC._t._collectors) == 2
    for cls, _match, sink in QC._t._collectors:            # what transport._dispatch does
        if cls is pa.SetlistPositionMessage:
            sink.append(pa.SetlistPositionMessage(folder_key="/media/p4/Presets/Live", position=9))
        else:
            sink.append(pa.GridMessage())
    assert tracker.seq == 2 and tracker.slot == ("/media/p4/Presets/Live", 9)
    assert core._PushTracker.attach(object(), ["GridMessage"]) is False   # no private API: poll


def test_changes_route(client):
    client.get("/api/editor/grid")
    r = client.get("/api/editor/changes").json()
    assert r["mode"] == "push" and isinstance(r["seq"], int)
    r = client.post("/api/editor/recall", json={"folder": MY, "position": 1, "confirm": True})
    assert r.status_code == 200 and r.json()["loaded"]
    assert client.get("/api/editor/presets", params={"folder": MY}).json()["presets"]


# -- review fixes --------------------------------------------------------------------------

def test_swap_of_the_same_model_is_refused(device, s):
    s.layout(device, "duplicate", {"row": 0, "col": 2, "model_id": EQ, "to_row": 0, "to_col": 4})
    with pytest.raises(editor.EditorError, match="同じモデル"):
        s.layout(device, "move", {"row": 0, "col": 2, "model_id": EQ, "to_row": 0, "to_col": 4,
                                  "to_model_id": EQ})


def test_swap_stops_when_the_first_move_is_refused(device, fake, s, monkeypatch):
    sent = []
    monkeypatch.setattr(fake, "move_block", lambda *a, **k: sent.append(a))   # the unit ignores it
    with pytest.raises(editor.EditorError, match="1 手目"):
        s.layout(device, "move", {"row": 0, "col": 0, "model_id": AMP, "to_row": 0, "to_col": 1,
                                  "to_model_id": CAB})
    assert len(sent) == 1 and layout(device) == {(0, 0): AMP, (0, 1): CAB, (0, 2): EQ}


def test_drop_on_a_block_that_vanished_is_a_conflict(device, s):
    with pytest.raises(editor.EditorConflict):
        s.layout(device, "move", {"row": 0, "col": 0, "model_id": AMP, "to_row": 0, "to_col": 6,
                                  "to_model_id": EQ})


def test_paste_skips_switches_that_have_no_option_names(device, fake, s):
    clip = {"model_id": AMP, "model": "x", "bypassed": False,
            "params": [{"name": "CHANNEL", "value": 1.0}, {"name": "BASS", "value": 0.2}]}
    r = s.layout(device, "paste", {"row": 3, "col": 0, "model_id": None, "source": clip})
    assert any("CHANNEL" in w for w in r["warnings"])
    sent = [c for c in fake.calls if c[0] == "set_param"]
    assert all(c[2].get("param") != "CHANNEL" for c in sent)


def test_undo_never_writes_a_switch_by_value(device, fake, s):
    s.layout(device, "delete", {"row": 0, "col": 0, "model_id": AMP})
    fake.calls.clear()
    panel.undo(device)
    assert all(c[2].get("param") != "CHANNEL" for c in fake.calls if c[0] == "set_param")


def test_split_conflict_and_no_op(device, s):
    s.layout(device, "split", {"row": 0, "split": 1, "mix": 5, "expect": None})
    assert s.layout(device, "split", {"row": 0, "split": 1, "mix": 5})["id"] is None
    with pytest.raises(editor.EditorConflict):
        s.layout(device, "split", {"row": 0, "split": 2, "mix": 6, "expect": None})
