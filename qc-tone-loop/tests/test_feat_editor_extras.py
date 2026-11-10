"""エディターの追加の設定: view fields, writes, conflicts, read-back, undo / redo, routes.

Mock only: these say how qcsuite drives the fake unit, not what the unit does.
"""

import pyquadcortex as pq
import pytest
from pyquadcortex.catalog import Model, Parameter

from qcsuite.device import fake_data
from qcsuite.errors import ForbiddenCall
from qcsuite.features import editor, editor_extras as ex, panel
from qcsuite.web.app import GRID_CHANGING


def _blocks(v):
    return [c for r in v["rows"] for c in r["cells"] if c]


def _knob(block):
    return next(p for p in block["params"] if p.get("kind") == "knob" and p.get("writable"))


# -- the view -----------------------------------------------------------------------

def test_view_has_row_settings_tempo_colours_and_param_extras(device):
    v = editor.view(device)
    r0 = v["rows"][0]
    assert set(r0["lane"]) >= {"volume_db", "volume", "pan", "mute", "solo"}
    assert r0["lane"]["pan"] == 0.5 and r0["lane"]["mute"] is False
    assert abs(r0["lane"]["volume_db"] - pq.lane_level_db(r0["lane"]["volume"])) < 0.06
    assert set(r0["gate"]) == {"noise_reduction", "input_gain_db", "bypassed"}
    assert r0["gate"]["input_gain_db"] == 0.0
    assert r0["splitter"] is None and r0["mixer"] is None and r0["split_muted"] is None
    assert v["rows"][1]["splitter"] is None
    assert v["tempo"] == 120.0 and v["tempo_writable"] is True
    assert len(v["scene_colors"]) == 8 and v["scene_colors"][0].startswith("#")
    assert v["stomp"] == {}
    b = _blocks(v)[0]
    assert b["stomp"] is None
    p = b["params"][0]
    assert {"scene_mode": False, "expression": 0, "exp_min": None, "exp_max": None}.items() \
        <= p.items()


def test_view_splitter_and_mixer_on_a_split_row(device, fake):
    fake.set_split(0, 2, 5)
    r0 = editor.view(device)["rows"][0]
    names = [p["name"] for p in r0["splitter"]["params"]]
    assert names == ["TYPE", "STEREO", "BALANCE", "LEVEL TO A", "LEVEL TO B", "FREQUENCY", "MODE"]
    typ = r0["splitter"]["params"][0]
    assert typ["kind"] == "select" and typ["options"] == ["A/B", "Balance", "Crossover"]
    assert typ["text"] == "A/B" and typ["writable"]
    level = next(p for p in r0["splitter"]["params"] if p["name"] == "LEVEL TO A")
    assert level["units"] == "dB" and level["real"] == 0.0 and level["text"] == "+0.0 dB"
    assert [p["name"] for p in r0["mixer"]["params"]][0] == "LEVEL A"
    assert r0["split_muted"] is False


# -- row output and gate -----------------------------------------------------------

def test_lane_volume_in_db_reads_back_and_undoes(device):
    lane = editor.view(device)["rows"][0]["lane"]
    out = ex.write(device, "lane", {"row": 0, "param": "VOLUME", "db": -6.0,
                                    "expect": lane["volume"]})
    assert out["id"] and out["lane"]["volume_db"] == -6.0 and out["warnings"] == []
    assert editor.view(device)["rows"][0]["lane"]["volume_db"] == -6.0
    panel.undo(device)
    assert editor.view(device)["rows"][0]["lane"]["volume"] == lane["volume"]
    panel.redo(device)
    assert editor.view(device)["rows"][0]["lane"]["volume_db"] == -6.0


def test_lane_conflict_writes_nothing_and_expect_is_required(device, fake):
    with pytest.raises(editor.EditorConflict):
        ex.write(device, "lane", {"row": 0, "param": "PAN", "value": 0.2, "expect": 0.9})
    with pytest.raises(editor.EditorError):
        ex.write(device, "lane", {"row": 0, "param": "PAN", "value": 0.2})
    assert editor.view(device)["rows"][0]["lane"]["pan"] == 0.5
    assert not [c for c in fake.calls if c[0] == "set_lane_output"]
    assert panel.history_view(True) == []


def test_lane_mute_by_bool(device):
    out = ex.write(device, "lane", {"row": 0, "param": "MUTE", "on": True, "expect": False})
    assert out["lane"]["mute"] is True
    with pytest.raises(editor.EditorError):
        ex.write(device, "lane", {"row": 0, "param": "MUTE", "on": 1, "expect": True})


def test_gate_noise_reduction_and_bypass(device):
    out = ex.write(device, "gate", {"row": 0, "param": "NOISE REDUCTION", "real": 40,
                                    "expect": 0.0})
    assert out["gate"]["noise_reduction"] == 40.0
    out = ex.write(device, "gate", {"row": 0, "param": "BYPASS", "on": False, "expect": True})
    assert out["gate"]["bypassed"] is False
    panel.undo(device)
    assert editor.view(device)["rows"][0]["gate"]["bypassed"] is True


# -- splitter, mixer, split mute -------------------------------------------------------

def test_splitter_needs_a_split_and_an_even_row(device):
    with pytest.raises(editor.EditorConflict):
        ex.write(device, "splitter", {"row": 0, "param": "BALANCE", "value": 0.3})
    with pytest.raises(editor.EditorError):
        ex.write(device, "splitter", {"row": 1, "param": "BALANCE", "value": 0.3})


def test_splitter_switch_by_option_name_only(device, fake):
    fake.set_split(0, 2, 5)
    with pytest.raises(editor.EditorError):
        ex.write(device, "splitter", {"row": 0, "param": "TYPE", "value": 0.5})
    with pytest.raises(editor.EditorConflict):
        ex.write(device, "splitter", {"row": 0, "param": "TYPE", "option": "Balance",
                                      "expect": "Crossover"})
    out = ex.write(device, "splitter", {"row": 0, "param": "TYPE", "option": "Balance",
                                        "expect": "A/B"})
    typ = out["splitter"]["params"][0]
    assert typ["text"] == "Balance" and out["id"]
    panel.undo(device)
    assert editor.view(device)["rows"][0]["splitter"]["params"][0]["text"] == "A/B"


def test_mixer_level_in_db_and_split_mute(device, fake):
    fake.set_split(2, 1, 6)
    out = ex.write(device, "mixer", {"row": 2, "param": "LEVEL B", "db": -10.0})
    level = next(p for p in out["mixer"]["params"] if p["name"] == "LEVEL B")
    assert level["real"] == -10.0
    out = ex.write(device, "split_mute", {"row": 2, "muted": True, "expect": False})
    assert out["split_muted"] is True
    assert all(sb.bypass for sb in fake.live.chains[2].mixBypass)
    panel.undo(device)
    assert editor.view(device)["rows"][2]["split_muted"] is False


# -- scenes --------------------------------------------------------------------------

def test_scene_color_and_undo(device):
    before = editor.view(device)["scene_colors"][2]
    out = ex.write(device, "scene_color", {"scene": 2, "color": "#123abc"})
    assert out["scene_colors"][2] == "#123abc"
    panel.undo(device)
    assert editor.view(device)["scene_colors"][2] == before
    with pytest.raises(editor.EditorError):
        ex.write(device, "scene_color", {"scene": 2, "color": "red"})


def test_scene_copy_cannot_be_undone_but_swap_can(device, fake):
    device.set_scene_label(0, "Clean")
    device.set_scene_label(3, "Lead")
    out = ex.write(device, "scene_copy", {"from": 3, "to": 1, "swap": False})
    assert out["id"] is None and "取り消せません" in out["note"]
    assert out["scene_labels"][1] == "Lead"
    out = ex.write(device, "scene_copy", {"from": 0, "to": 3, "swap": True})
    assert out["id"] and out["scene_labels"][0] == "Lead" and out["scene_labels"][3] == "Clean"
    panel.undo(device)
    assert list(fake.live.scene_labels)[:4] == ["Clean", "Lead", " ", "Lead"]


def test_scene_mode_toggle_undo_and_refused_in_another_scene(device, fake):
    b = _blocks(editor.view(device))[0]
    p = _knob(b)
    req = {"row": b["row"], "col": b["col"], "model_id": b["model_id"], "param": p["name"]}
    out = ex.write(device, "scene_mode", {**req, "enabled": True, "expect": False})
    assert out["param"]["scene_mode"] is True and out["id"]
    view_p = next(x for x in _blocks(editor.view(device))[0]["params"] if x["name"] == p["name"])
    assert view_p["scene_mode"] is True
    device.switch_scene(1)
    with pytest.raises(panel.PanelError):
        panel.undo(device)
    device.switch_scene(0)
    panel.undo(device)
    assert fake.live.chains[b["row"]].models[b["col"]].params[p["index"]].scene_mode is False


def test_block_keyed_write_checks_the_model(device):
    b = _blocks(editor.view(device))[0]
    with pytest.raises(editor.EditorConflict):
        ex.write(device, "scene_mode", {"row": b["row"], "col": b["col"], "model_id": 1,
                                        "param": "X", "enabled": True})


# -- tempo ------------------------------------------------------------------------------

def test_tempo_is_clamped_reads_back_and_undoes(device):
    out = ex.write(device, "tempo", {"bpm": 400, "expect": 120.0})
    assert out["tempo"] == 250.0
    with pytest.raises(editor.EditorConflict):
        ex.write(device, "tempo", {"bpm": 90, "expect": 120.0})
    panel.undo(device)
    assert editor.view(device)["tempo"] == 120.0


def test_tempo_with_a_placeholder_catalog_range_is_refused(device, fake):
    old = fake._catalog_obj.models[fake_data.TEMPO_CONTROL]
    fake._catalog_obj.models[fake_data.TEMPO_CONTROL] = Model(
        id=old.id, name=old.name, category=old.category, category_id=old.category_id,
        internal=True, parameters=(Parameter(0, "TEMPO", 0.0, 1.0, 0.5, units="BPM"),)
        + old.parameters[1:])
    v = editor.view(device)
    assert v["tempo_writable"] is False
    with pytest.raises(editor.EditorError):
        ex.write(device, "tempo", {"bpm": 100})
    assert not [c for c in fake.calls if c[0] == "set_tempo_param"]


# -- footswitches -----------------------------------------------------------------------

def _req(b, **kw):
    return {"row": b["row"], "col": b["col"], "model_id": b["model_id"], **kw}


def test_stomp_assign_label_momentary(device):
    blocks = _blocks(editor.view(device))
    a = blocks[0]
    out = ex.write(device, "stomp", _req(a, footswitch="C", expect=None))
    assert out["stomp"] == "C" and out["id"]
    v = editor.view(device)
    assert v["stomp"]["C"]["blocks"] == [{"row": a["row"], "col": a["col"]}]
    assert _blocks(v)[0]["stomp"] == "C"
    out = ex.write(device, "stomp_label", {"footswitch": "C", "label": "  Boost ", "expect": ""})
    assert out["label"] == "Boost"
    out = ex.write(device, "stomp_momentary", {"footswitch": "C", "momentary": True})
    assert out["momentary"] is True and out["warnings"] == []
    assert editor.view(device)["stomp"]["C"]["momentary"] is True
    with pytest.raises(editor.EditorError):
        ex.write(device, "stomp_label", {"footswitch": "H", "label": "x"})


def test_momentary_on_a_two_block_switch_warns_and_records_nothing(device):
    a, b = _blocks(editor.view(device))[:2]
    ex.write(device, "stomp", _req(a, footswitch="D"))
    ex.write(device, "stomp", _req(b, footswitch="D"))
    n = len(panel.history_view(True))
    out = ex.write(device, "stomp_momentary", {"footswitch": "D", "momentary": True})
    assert out["momentary"] is False and out["id"] is None
    assert any("2 つ以上" in w for w in out["warnings"])
    assert len(panel.history_view(True)) == n


def test_unassigning_restores_label_on_undo(device):
    a = _blocks(editor.view(device))[0]
    ex.write(device, "stomp", _req(a, footswitch="E"))
    ex.write(device, "stomp_label", {"footswitch": "E", "label": "Solo"})
    out = ex.write(device, "stomp", _req(a, footswitch=None, expect="E"))
    assert out["stomp"] is None and editor.view(device)["stomp"] == {}
    panel.undo(device)
    v = editor.view(device)
    assert v["stomp"]["E"]["label"] == "Solo"
    with pytest.raises(editor.EditorConflict):
        ex.write(device, "stomp", _req(a, footswitch="F", expect="A"))


# -- expression ---------------------------------------------------------------------------

def test_expression_assign_warns_about_scenes_and_undoes(device):
    b = _blocks(editor.view(device))[0]
    p = _knob(b)
    out = ex.write(device, "expression", _req(b, param=p["name"], pedal=2, min=0.1, max=0.9,
                                              expect=0))
    assert out["param"]["expression"] == 2 and out["param"]["exp_max"] == pytest.approx(0.9)
    assert ex.EXPRESSION_WARNING in out["warnings"]
    vp = next(x for x in _blocks(editor.view(device))[0]["params"] if x["name"] == p["name"])
    assert vp["expression"] == 2 and vp["exp_min"] == pytest.approx(0.1)
    panel.undo(device)
    vp = next(x for x in _blocks(editor.view(device))[0]["params"] if x["name"] == p["name"])
    assert vp["expression"] == 0
    with pytest.raises(editor.EditorError):
        ex.write(device, "expression", _req(b, param=p["name"], pedal=3, min=0, max=1))


# -- gig view, undo replay conflicts, routes ------------------------------------------------

def test_gig_view_is_refused_by_the_safety_rules(device, fake):
    with pytest.raises(ForbiddenCall):
        ex.write(device, "gig_view", {"shown": True})


def test_undo_stops_when_the_unit_changed_the_setting(device, fake):
    lane = editor.view(device)["rows"][0]["lane"]
    ex.write(device, "lane", {"row": 0, "param": "PAN", "value": 0.2, "expect": lane["pan"]})
    fake.set_lane_output(0, "PAN", value=0.8)          # turned on the unit's screen
    with pytest.raises(panel.PanelError):
        panel.undo(device)
    assert editor.view(device)["rows"][0]["lane"]["pan"] == pytest.approx(0.8)


def test_routes(client, fake):
    v = client.get("/api/editor/grid").json()
    assert "lane" in v["rows"][0] and "stomp" in v
    r = client.post("/api/editor/extra/lane", json={"row": 0, "param": "PAN", "value": 0.25,
                                                     "expect": v["rows"][0]["lane"]["pan"]})
    assert r.status_code == 200 and r.json()["lane"]["pan"] == 0.25 and r.json()["id"]
    r = client.post("/api/editor/extra/lane", json={"row": 0, "param": "PAN", "value": 0.3,
                                                     "expect": 0.9})
    assert r.status_code == 409 and r.json()["conflict"] is True
    assert client.post("/api/editor/extra/nope", json={}).status_code == 400
    assert client.post("/api/editor/extra/gig_view", json={"shown": True}).status_code == 400
    assert client.post("/api/panel/undo", json={}).status_code == 200
    assert client.get("/api/editor/grid").json()["rows"][0]["lane"]["pan"] == 0.5
    assert GRID_CHANGING.match("/api/editor/extra/scene_copy")
