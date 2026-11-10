"""Editor grid layout: move / swap / delete / paste / duplicate / reset / ports / splits /
scenes, and move-aware undo (qcsuite/features/editor.py, qctone/device.py).

The fake unit starts with row 1 = Brit TM45 Normal (1038), a cab (12001) and
Parametric 3 (4001) in columns 1-3.
"""

import pytest

from qcsuite.features import editor, modelrepo, panel
from qctone import device as qdevice

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


def calls(fake, name):
    return [c for c in fake.calls if c[0] == name]


# -- move and swap ---------------------------------------------------------------------

def test_move_to_an_empty_cell_keeps_the_block(device, fake, s):
    before = device.grid()
    amp_before = next(b for b in before["blocks"] if b["col"] == 0)
    r = s.layout(device, "move", {"row": 0, "col": 0, "model_id": AMP, "to_row": 0, "to_col": 5})
    assert r["id"] and r["note"] == editor.NOT_SAVED
    assert layout(device) == {(0, 5): AMP, (0, 1): CAB, (0, 2): EQ}
    moved = next(b for b in device.grid()["blocks"] if b["col"] == 5)
    assert [p.get("value") for p in moved["params"]] == [p.get("value") for p in amp_before["params"]]
    assert calls(fake, "move_block") and not calls(fake, "remove_block")
    assert panel.history(device.mock)[-1]["engine"] == "editor"


def test_drop_on_a_block_swaps(device, s):
    s.layout(device, "move", {"row": 0, "col": 0, "model_id": AMP, "to_row": 0, "to_col": 2,
                              "to_model_id": EQ})
    assert layout(device) == {(0, 2): AMP, (0, 1): CAB, (0, 0): EQ}


def test_cross_row_move_warns_about_the_parallel_path(device, s):
    r = s.layout(device, "move", {"row": 0, "col": 2, "model_id": EQ, "to_row": 1, "to_col": 2})
    assert layout(device)[(1, 2)] == EQ
    assert any("並列" in w for w in r["warnings"])


def test_move_conflict_writes_nothing(device, fake, s):
    with pytest.raises(editor.EditorConflict):
        s.layout(device, "move", {"row": 0, "col": 0, "model_id": EQ, "to_row": 0, "to_col": 5})
    with pytest.raises(editor.EditorConflict):          # the target changed under us
        s.layout(device, "move", {"row": 0, "col": 0, "model_id": AMP, "to_row": 0, "to_col": 1,
                                  "to_model_id": EQ})
    assert not calls(fake, "move_block")


def test_undo_of_a_move_moves_back(device, fake, s):
    s.layout(device, "move", {"row": 0, "col": 0, "model_id": AMP, "to_row": 0, "to_col": 6})
    n = len(calls(fake, "move_block"))
    fake.calls.clear()
    panel.undo(device)
    assert layout(device) == {(0, 0): AMP, (0, 1): CAB, (0, 2): EQ}
    assert calls(fake, "move_block") and not calls(fake, "set_block") and n == 1
    panel.redo(device)
    assert layout(device)[(0, 6)] == AMP


def test_undo_of_a_swap_goes_through_a_free_cell(device, fake, s):
    s.layout(device, "move", {"row": 0, "col": 0, "model_id": AMP, "to_row": 0, "to_col": 1,
                              "to_model_id": CAB})
    fake.calls.clear()
    panel.undo(device)
    assert layout(device) == {(0, 0): AMP, (0, 1): CAB, (0, 2): EQ}
    assert not calls(fake, "set_block")


def test_restore_ops_moves_a_cycle_of_three():
    rows = [{"row": r, "in_port": "EMPTY", "out_port": "EMPTY"} for r in range(4)]

    def grid(*cells):
        return {"rows": rows, "blocks": [{"row": 0, "col": c, "model_id": m, "model": str(m),
                                          "bypassed": False, "params": []} for c, m in cells]}
    saved, current = grid((0, 1), (1, 2), (2, 3)), grid((0, 3), (1, 1), (2, 2))
    ops = qdevice.restore_ops(saved, current)["ops"]
    assert {o["op"] for o in ops} <= {"move_block", "bypass"}
    cells = {(0, b["col"]): b["model_id"] for b in current["blocks"]}
    for o in ops:
        if o["op"] == "move_block":
            assert (o["to_row"], o["to_col"]) not in cells
            cells[(o["to_row"], o["to_col"])] = cells.pop((o["row"], o["col"]))
    assert cells == {(0, 0): 1, (0, 1): 2, (0, 2): 3}


# -- delete ------------------------------------------------------------------------------

def test_delete_and_undo(device, s):
    s.layout(device, "delete", {"row": 0, "col": 2, "model_id": EQ})
    assert (0, 2) not in layout(device)
    panel.undo(device)
    assert layout(device)[(0, 2)] == EQ


def test_delete_of_a_capture_says_undo_will_not_bring_the_capture_back(device, s):
    device.apply_patch({"ops": [{"op": "set_block", "row": 1, "col": 0, "model": 14000}]})
    r = s.layout(device, "delete", {"row": 1, "col": 0, "model_id": 14000})
    assert any("戻りません" in w for w in r["warnings"])


# -- copy / paste / duplicate / reset ------------------------------------------------------

def test_copy_then_paste_into_an_empty_cell(device, s):
    s.set(device, {"row": 0, "col": 0, "model_id": AMP, "param": "BASS", "real": 7.0})
    clip = editor.block_copy(device, 0, 0)
    assert clip["model_id"] == AMP and not clip["lossy"]
    s.layout(device, "paste", {"row": 2, "col": 3, "model_id": None, "source": clip})
    g = device.grid()
    a = next(b for b in g["blocks"] if (b["row"], b["col"]) == (0, 0))
    b = next(b for b in g["blocks"] if (b["row"], b["col"]) == (2, 3))
    assert {p["name"]: p.get("value") for p in a["params"]} == \
        {p["name"]: p.get("value") for p in b["params"]}


def test_paste_replaces_a_block_and_is_one_undo_step(device, s):
    clip = editor.block_copy(device, 0, 0)
    s.layout(device, "paste", {"row": 0, "col": 2, "model_id": EQ, "source": clip})
    assert layout(device)[(0, 2)] == AMP
    panel.undo(device)
    assert layout(device)[(0, 2)] == EQ


def test_paste_conflict_and_bad_sources(device, s):
    clip = editor.block_copy(device, 0, 0)
    with pytest.raises(editor.EditorConflict):
        s.layout(device, "paste", {"row": 0, "col": 2, "model_id": None, "source": clip})
    with pytest.raises(editor.EditorError):
        s.layout(device, "paste", {"row": 3, "col": 0, "model_id": None,
                                   "source": {"model_id": 999999, "params": []}})
    with pytest.raises(editor.EditorError):
        s.layout(device, "paste", {"row": 3, "col": 0, "model_id": None,
                                   "source": {"model_id": 14000, "params": []}})


def test_paste_never_writes_a_switch_by_value(device, fake, s):
    clip = {"model_id": AMP, "model": "x", "bypassed": False,
            "params": [{"name": "CHANNEL", "value": 1.0, "option": "?"}]}
    r = s.layout(device, "paste", {"row": 3, "col": 0, "model_id": None, "source": clip})
    assert any("CHANNEL" in w for w in r["warnings"])


def test_duplicate_into_an_empty_cell(device, s):
    s.layout(device, "duplicate", {"row": 0, "col": 2, "model_id": EQ, "to_row": 0, "to_col": 3})
    assert layout(device)[(0, 3)] == EQ and layout(device)[(0, 2)] == EQ
    with pytest.raises(editor.EditorConflict):
        s.layout(device, "duplicate", {"row": 0, "col": 2, "model_id": EQ, "to_row": 0,
                                       "to_col": 0})


def test_reset_puts_defaults_back_and_keeps_bypass(device, s):
    s.set(device, {"row": 0, "col": 0, "model_id": AMP, "param": "BASS", "real": 9.0})
    s.set(device, {"row": 0, "col": 0, "model_id": AMP, "param": None, "bypassed": True})
    s.layout(device, "reset", {"row": 0, "col": 0, "model_id": AMP})
    amp = next(b for b in device.grid()["blocks"] if b["col"] == 0)
    gain = next(p for p in amp["params"] if p["name"] == "BASS")
    assert abs(gain["real"] - 9.0) > 0.5 and amp["bypassed"] is True


# -- ports and splits ---------------------------------------------------------------------

def test_port_change_and_undo(device, s):
    s.layout(device, "port", {"row": 1, "which": "in", "port": "INPUT_2", "expect": "EMPTY"})
    s.layout(device, "port", {"row": 1, "which": "out", "port": "MULTIPLE", "expect": "EMPTY"})
    rows = device.grid()["rows"]
    assert rows[1]["in_port"] == "INPUT_2" and rows[1]["out_port"] == "MULTIPLE"
    panel.undo(device)
    assert device.grid()["rows"][1]["out_port"] == "EMPTY"
    with pytest.raises(editor.EditorError):
        s.layout(device, "port", {"row": 1, "which": "in", "port": "SIDECHAIN_BUFFER"})
    with pytest.raises(editor.EditorConflict):
        s.layout(device, "port", {"row": 1, "which": "in", "port": "INPUT_1", "expect": "USB_5"})


def test_split_set_clear_and_undo(device, s):
    s.layout(device, "split", {"row": 0, "split": 1, "mix": 5})
    assert device.grid()["rows"][0]["split"] == [1, 5]
    assert editor.view(device)["rows"][0]["split"] == [1, 5]
    s.layout(device, "split", {"row": 0, "clear": True})
    assert device.grid()["rows"][0]["split"] is None
    panel.undo(device)
    assert device.grid()["rows"][0]["split"] == [1, 5]
    with pytest.raises(editor.EditorError):
        s.layout(device, "split", {"row": 1, "split": 1, "mix": 5})


def test_view_has_port_lists_labels_and_lossy_flags(device):
    v = editor.view(device)
    assert v["rows"][0]["in_label"] == "In 1" and v["rows"][0]["out_label"] == "Out 1/2"
    assert {"id": "MULTIPLE", "label": "Multi Out"} in v["ports"]["out"]
    assert v["rows"][0]["cells"][0]["lossy"] is False


# -- scenes --------------------------------------------------------------------------------

def test_scene_switch_and_label(device, s):
    assert s.scene(device, {"scene": 2})["active"] == "C"
    s.scene(device, {"scene": 2, "label": "Lead"})
    assert editor.view(device)["scene_labels"][2] == "Lead"
    with pytest.raises(editor.EditorError):
        s.scene(device, {"scene": 9})


def test_unknown_action(device, s):
    with pytest.raises(editor.EditorError):
        s.layout(device, "explode", {})


# -- routes ---------------------------------------------------------------------------------

def test_layout_routes(client):
    r = client.post("/api/editor/layout/move", json={"row": 0, "col": 0, "model_id": AMP,
                                                     "to_row": 0, "to_col": 4})
    assert r.status_code == 200 and r.json()["id"]
    clip = client.get("/api/editor/block", params={"row": 0, "col": 4}).json()
    assert clip["model_id"] == AMP
    r = client.post("/api/editor/layout/paste", json={"row": 3, "col": 7, "model_id": None,
                                                      "source": clip})
    assert r.status_code == 200
    stale = client.post("/api/editor/layout/delete", json={"row": 0, "col": 0, "model_id": AMP})
    assert stale.status_code == 409 and stale.json()["conflict"] is True
    assert client.post("/api/editor/scene", json={"scene": 1}).json()["active"] == "B"
