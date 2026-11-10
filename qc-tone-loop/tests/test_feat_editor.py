"""エディター: raw ModelRepo parser, display rules, the view, writes, gestures, web routes."""

import gzip
import io
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from qcsuite.device import fake_data
from qcsuite.features import editor, modelrepo, panel

ROOT = Path(__file__).resolve().parents[1]

# Hand-written in the shape of the unit's file (see prompts/visual-editor.md). Not real data.
SAMPLE = b"""<?xml version="1.0" ?>
<Models>
 <Category id="0" name="Guitar Overdrive">
  <Model id="2" name="Obsessive Drive" tm="Based on an overdrive">
   <Padding cpu="0.6"/>
   <Parameter defaultValue="5" max="10" min="0" name="DRIVE" type="float" units=""/>
   <Parameter defaultValue="0" max="1" min="0" name="PEAK" stepNames="LP,HP" steps="2" type="switch" units=""/>
   <Parameter defaultValue="5" max="10" min="0" name="TONE" type="float" units=""/>
   <Parameter defaultValue="5" max="10" min="0" name="VOLUME" type="float" units=""/>
  </Model>
 </Category>
 <Category id="1" name="Guitar Amplifier">
  <Model id="1001" name="Brit 2203" tm="Based on a 100 W head">
   <Parameter defaultValue="5" displayPos="0" max="10" min="0" name="GAIN" type="float" units=""/>
   <Parameter defaultValue="5" displayPos="1" max="10" min="0" name="BASS" type="float" units=""/>
   <Parameter defaultValue="5" displayPos="2" max="10" min="0" name="MID" type="float" units=""/>
   <Parameter defaultValue="5" displayPos="3" max="10" min="0" name="TREBLE" type="float" units=""/>
   <Parameter defaultValue="3" displayPos="5" max="10" min="0" name="MASTER" type="float" units=""/>
   <Parameter defaultValue="5" displayPos="4" max="10" min="0" name="PRESENCE" type="float" units=""/>
   <Parameter defaultValue="0.0" displayPos="6" max="12" min="-60" min_string="OFF" name="OUTPUT" skew="3.8018" type="float" units="dB"/>
  </Model>
  <Model id="40010" name="Plugin Amp" plugin_id="Plini" sku="X-1" tm="">
   <Parameter defaultValue="5" max="10" min="0" name="GAIN" type="float" units=""/>
  </Model>
 </Category>
 <Category id="5" name="Compressor">
  <Model id="5001" name="Legendary 87 (M)" tm="Based on a limiter">
   <Parameter defaultValue="-15.0" max="0.0" min="-48.0" name="INPUT" skew="LIN_SKEW" steps="481" type="float" units="dB"/>
   <Parameter defaultValue="2" max="4" min="0" name="RATIO" stepNames="4,8,12,20,All" steps="5" type="rotarySwitch" units=""/>
   <Parameter defaultValue="0.8" max="0.8" min="0.016" name="ATTACK" skew="0.6" steps="300" type="float" units="ms"/>
   <Parameter defaultValue="1.0" max="1.1" min="0.056" name="RELEASE" skew="0.6" steps="300" type="float" units="s"/>
   <Parameter defaultValue="4.0" max="48.0" min="-48.0" name="MAKEUP" skew="LIN_SKEW" steps="481" type="float" units="dB"/>
   <Parameter defaultValue="100" max="100" min="0" name="MIX" skew="LIN_SKEW" steps="1001" type="float" units="%"/>
   <Parameter defaultValue="0" max="0" min="-50" name="GAIN REDUCTION" steps="180" type="grMeter" units="dB"/>
  </Model>
 </Category>
 <Category id="12" name="Cabsim Guitar (M)">
  <Model hidden="true" id="12000" name="Default Cabsim" tm="">
   <Parameter defaultValue="0" max="1" min="0" name="bypass" steps="2" type="switch" units=""/>
   <Parameter defaultValue="" max="999" min="0" name="ir selector" steps="1000" type="string" units=""/>
   <Parameter defaultValue="0.0" displayPos="2" max="MAX_CABSIM_DB" min="MIN_CABSIM_DB" min_string="OFF" name="LEVEL" skew="4.95" type="float" units="dB"/>
   <Parameter defaultValue="5" displayPos="3" max="10" max_string="R" min="0" min_string="L" name="PAN" type="float" units=""/>
   <Parameter defaultValue="0.0" displayPos="1" max="10" min="0" name="DISTANCE" type="float" units=""/>
   <Parameter defaultValue="4.0" displayPos="0" max="10" min="0" name="POSITION" type="float" units=""/>
   <Parameter defaultValue="0" max="1" min="0" name="phi" steps="2" type="switch" units=""/>
   <Parameter defaultValue="0" max="1" min="0" name="bypass" steps="2" type="switch" units=""/>
   <Parameter defaultValue="" max="999" min="0" name="ir selector" steps="1000" type="string" units=""/>
   <Parameter defaultValue="0.0" displayPos="6" max="MAX_CABSIM_DB" min="MIN_CABSIM_DB" name="LEVEL" type="float" units="dB"/>
   <Parameter defaultValue="20.0" max="500" min="20" min_string="OFF" name="HPF" showAsInteger="true" skew="0.3" type="float" units="Hz"/>
   <Parameter defaultValue="" hidden="true" max="999" min="0" name="IR NAME" steps="1000" type="string" units=""/>
  </Model>
  <Model clones="12000" id="12006" name="412 Brit 60B GB 71 (M)" tm="Based on a 4x12">
   <Parameter defaultValue="ML_412 Brit 60B GB 71_Dynamic 57" max="999" min="0" name="ir selector" replaces="1" steps="1000" type="string" units=""/>
   <Parameter defaultValue="ML_412 Brit 60B GB 71_Ribbon 160" max="999" min="0" name="ir selector" replaces="8" steps="1000" type="string" units=""/>
  </Model>
 </Category>
 <Category hidden="true" id="23" name="Lane output control">
  <Model id="23000" name="Lane" internal="true">
   <Parameter defaultValue="0" max="1" min="0" name="VOLUME" type="float" units="dB"/>
  </Model>
 </Category>
</Models>
"""


def targz(xml: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("ModelRepo.xml")
        info.size = len(xml)
        tf.addfile(info, io.BytesIO(xml))
    return gzip.compress(buf.getvalue())


@pytest.fixture
def repo():
    return modelrepo.parse(SAMPLE)


@pytest.fixture(autouse=True)
def fresh_repo_memory():
    modelrepo.forget()
    yield
    modelrepo.forget()


# -- the raw parser -----------------------------------------------------------------------

def names(params):
    return [p.name for p in params]


def test_display_pos_orders_the_screen_not_the_wire(repo):
    wire = repo.wire_params(1001)
    assert names(wire) == ["GAIN", "BASS", "MID", "TREBLE", "MASTER", "PRESENCE", "OUTPUT"]
    shown = repo.display_params(1001)
    assert names(shown) == ["GAIN", "BASS", "MID", "TREBLE", "PRESENCE", "MASTER", "OUTPUT"]
    assert [p.index for p in shown] == [0, 1, 2, 3, 5, 4, 6]      # wire index kept for sending
    out = shown[-1]
    assert (out.minimum, out.maximum, out.default, out.units) == (-60.0, 12.0, 0.0, "dB")
    assert out.skew == pytest.approx(3.8018) and out.min_string == "OFF"
    assert repo.get(1001).based_on.startswith("Based on")


def test_payload_forms_all_parse():
    for payload in (SAMPLE, gzip.compress(SAMPLE), targz(SAMPLE)):
        assert len(modelrepo.parse(payload)) == 7
    with pytest.raises(ValueError):
        modelrepo.extract_xml(gzip.compress(b"not a tar and not xml"))


def test_padding_elements_do_not_shift_wire_indexes(repo):
    assert [p.index for p in repo.wire_params(2)] == [0, 1, 2, 3]


def test_step_names_skew_and_hidden_types(repo):
    peak = repo.wire_params(2)[1]
    assert peak.kind == "select" and peak.labels() == ["LP", "HP"]
    comp = {p.name: p for p in repo.wire_params(5001)}
    assert comp["INPUT"].skew == 1.0 and comp["INPUT"].steps == 481     # LIN_SKEW
    assert comp["ATTACK"].skew == pytest.approx(0.6)
    assert comp["RATIO"].labels() == ["4", "8", "12", "20", "All"]
    assert comp["GAIN REDUCTION"].kind is None
    assert "GAIN REDUCTION" not in names(repo.display_params(5001))
    assert names(repo.display_params(5001))[:2] == ["INPUT", "RATIO"]   # no displayPos: wire order


def test_toggle_labels_fallback():
    p = modelrepo.RepoParam(0, "BRIGHT", type="switch", toggle_on="BRIGHT")
    assert p.labels() == ["OFF", "BRIGHT"]
    assert modelrepo.RepoParam(0, "X", type="switch").labels() == ["OFF", "ON"]


def test_clone_takes_the_source_list_with_replaced_indexes(repo):
    wire = repo.wire_params(12006)
    assert len(wire) == len(repo.get(12000).params)
    assert wire[1].default_text.endswith("_Dynamic 57") and wire[1].index == 1
    assert wire[8].default_text.endswith("_Ribbon 160") and wire[8].index == 8
    shown = repo.display_params(12006)
    # internal names gone, the second LEVEL (mic B's set) shown once, hidden IR NAME gone
    assert names(shown) == ["POSITION", "DISTANCE", "LEVEL", "PAN", "HPF"]
    level = shown[2]
    assert level.index == 2 and not level.range_known        # symbolic MIN/MAX_CABSIM_DB
    assert [p.index for p in repo.mic_params(12006)] == [1, 8]
    hpf = shown[-1]
    assert hpf.show_as_integer and hpf.min_string == "OFF" and hpf.skew == pytest.approx(0.3)


def test_hidden_and_internal_flags(repo):
    assert repo.get(12000).hidden and repo.get(23000).internal and repo.get(23000).category_hidden
    assert repo.get(40010).plugin_id == "Plini" and repo.get(40010).sku == "X-1"
    assert repo.get(12006).clones == 12000


def test_mock_xml_mirrors_the_fake_catalog():
    xml = (ROOT / "qcsuite/device/fake_model_repo.xml").read_bytes()
    mock = modelrepo.parse(xml)
    catalog = fake_data.build_catalog()
    assert len(mock) >= 10
    for model in mock.models.values():
        fake = catalog[model.id]
        assert model.name == fake.name and model.category == fake.category, model.name
        assert [p.name for p in model.params] == [p.name for p in fake.parameters], model.name
        for p, spec in zip(model.params, fake.parameters):
            if p.kind == "knob" and p.range_known:
                assert (p.minimum, p.maximum) == (spec.minimum, spec.maximum), (model.name, p.name)


# -- caching and fallback ---------------------------------------------------------------

def test_load_fetches_once_and_caches_per_firmware(device, fake):
    repo = modelrepo.load(device)
    assert repo.source == "device" and repo.get(1038) is not None
    assert modelrepo.cache_path(True, repo.firmware).exists()
    modelrepo.forget()
    again = modelrepo.load(device)
    assert again.source == "cache"
    assert [c for c in fake.calls if c[0] == "_fetch_model_repo"] == [("_fetch_model_repo", (), {})]
    modelrepo.forget()
    assert modelrepo.load(device, firmware="9.9.9").source == "device"    # a new firmware fetches


def test_fetch_failure_falls_back_to_the_catalog(device, fake, monkeypatch):
    def broken(timeout=25.0):
        raise TimeoutError("no ModelRepoMessage")
    monkeypatch.setattr(fake, "_fetch_model_repo", broken, raising=False)
    assert modelrepo.load(device) is None
    v = editor.view(device)
    assert v["repo"]["source"] == "none"
    amp = v["rows"][0]["cells"][0]
    assert [p["name"] for p in amp["params"]][:8] == [
        "GAIN 1", "GAIN 2", "BASS", "MIDDLE", "TREBLE", "PRESENCE", "RESONANCE", "MASTER"]


def test_missing_private_api_falls_back(device, monkeypatch):
    monkeypatch.setattr(device, "model_repo_payload", lambda: None)
    assert modelrepo.load(device) is None


# -- display rules ----------------------------------------------------------------------

def knob(**kw):
    base = {"min": 0.0, "max": 10.0, "skew": 1.0, "units": "", "steps": None, "int": False,
            "min_string": None, "max_string": None, "placeholder": False}
    base.update(kw)
    return base


@pytest.mark.parametrize("p,v,text", [
    (knob(min=-60, max=12, skew=3.8018, units="dB", min_string="OFF"), 0.0, "0.0 dB"),
    (knob(min=-60, max=12, skew=3.8018, units="dB", min_string="OFF"), -60.0, "OFF"),
    (knob(min=2000, max=20000, units="Hz", max_string="OFF"), 20000.0, "OFF"),
    (knob(min=20, max=500, units="Hz"), 80.0, "80 Hz"),
    (knob(min=0.1, max=10, units="Hz"), 1.5, "1.5 Hz"),
    (knob(min=0.016, max=0.8, units="ms"), 0.8, "0.80 ms"),
    (knob(min=1, max=2000, units="ms"), 350.4, "350 ms"),
    (knob(min=0, max=100, units="%"), 17.4, "17 %"),
    (knob(min=40, max=300, units="BPM"), 120.6, "121 BPM"),
    (knob(int=True), 3.0, "3"),
    (knob(), 5.0, "5.0"),
    (knob(), 0.0, "0.0"),
    (knob(min=-12, max=12, units="dB"), -0.25, "-0.25 dB"),
    (knob(placeholder=True, min=0.0, max=1.0), 0.76923, "0.77"),
])
def test_value_text(p, v, text):
    assert editor.value_text(p, v) == text


def test_knob_angle_is_270_degrees_with_skew():
    p = knob()
    assert editor.knob_angle(p, 0) == -135 and editor.knob_angle(p, 10) == 135
    assert editor.knob_angle(p, 5) == pytest.approx(0)
    out = knob(min=-60, max=12, skew=3.8018)
    expected = -135 + 270 * ((60 / 72) ** 3.8018)
    assert editor.knob_angle(out, 0.0) == pytest.approx(expected)
    assert editor.knob_angle(out, 99) == 135 and editor.knob_angle(out, -99) == -135


def test_value_from_proportion_inverts_skew_and_snaps_to_steps():
    attack = knob(min=0.016, max=0.8, skew=0.6, steps=300, units="ms")
    for v in (0.016, 0.2, 0.5, 0.8):
        prop = editor.proportion(attack, v)
        back = editor.value_from_proportion(attack, prop)
        assert back == pytest.approx(v, abs=(0.8 - 0.016) / 299)
    inp = knob(min=-48, max=0, steps=481, units="dB")
    assert editor.value_from_proportion(inp, 0.501) == pytest.approx(-24.0)
    assert editor.value_from_proportion(knob(int=True), 0.43) == 4


# -- the view on the mock ---------------------------------------------------------------

def cell(view, row, col):
    return view["rows"][row]["cells"][col]


def test_view_uses_screen_order_mics_and_colours(device):
    v = editor.view(device)
    assert v["note"] == editor.NOT_SAVED and v["repo"]["source"] == "device"
    amp = cell(v, 0, 0)
    assert amp["color"] == "amp" and amp["category_label"] == "Guitar Amplifier"
    order = [p["name"] for p in amp["params"]]
    assert order.index("MASTER") < order.index("PRESENCE")        # mock displayPos
    channel = next(p for p in amp["params"] if p["name"] == "CHANNEL")
    assert channel["kind"] == "select" and channel["writable"] and channel["options"]
    level = next(p for p in amp["params"] if p["name"] == "LEVEL")
    assert level["placeholder"] and level["writable"] and 0 <= level["value"] <= 1
    cab = cell(v, 0, 1)
    assert cab["color"] == "cab" and cab["category_label"] == "Cab"
    mic_a, mic_b = cab["params"][:2]
    assert (mic_a["name"], mic_a["text"], mic_a["kind"], mic_a["writable"]) == \
        ("MIC A", "Dynamic 57", "text", False)
    assert mic_b["name"] == "MIC B"
    low = next(p for p in cab["params"] if p["name"] == "LOW CUT")
    assert low["min_string"] == "OFF" and low["int"] and low["skew"] == pytest.approx(0.3)
    eq = cell(v, 0, 2)
    assert eq["color"] == "cmp"


def place(device, *ops):
    device.apply_patch({"ops": list(ops)})


def test_view_marks_blocks_that_do_not_sound(device):
    plini = device.catalog.find("Plini Clean").id
    place(device, {"op": "set_block", "row": 1, "col": 0, "model": 14000},
          {"op": "set_block", "row": 2, "col": 0, "model": plini})
    v = editor.view(device)
    capture, plugin = cell(v, 1, 0), cell(v, 2, 0)
    assert capture["color"] == "cap" and not capture["responds"]
    assert all(p["inert"] for p in capture["params"] if p["kind"] == "knob")
    assert "file_name" not in [p["name"] for p in capture["params"]]
    assert not plugin["responds"] and any("未所有" in f for f in plugin["flags"])
    bright = next(p for p in plugin["params"] if p["name"] == "BRIGHT")
    assert bright["kind"] == "select"
    assert "読み戻せるが音は変わらない" in editor.view_text(v)


def test_capture_title_is_the_capture_name():
    assert editor._capture_name("a" * 64 + "My Amp Capture") == "My Amp Capture"
    assert editor._capture_name("plain name") == "plain name"


def test_switch_without_option_names_is_not_writable(device, fake):
    """A switch whose names the preset does not carry is shown, never written by value."""
    v = editor.view(device)
    channel = next(p for p in cell(v, 0, 0)["params"] if p["name"] == "CHANNEL")
    assert channel["writable"]
    live = fake.live
    del live.chains[0].models[0].params[8].dynamic_steps[:]
    v = editor.view(device)
    channel = next(p for p in cell(v, 0, 0)["params"] if p["name"] == "CHANNEL")
    assert not channel["writable"] and channel["options"] is None
    assert channel["labels"] == ["CLEAN", "DRIVE"]                   # stepNames, display only
    assert "数値では書きません" in channel["why_not"]


# -- one-shot writes ----------------------------------------------------------------------

def amp_param(device, name):
    grid = device.grid()
    block = next(b for b in grid["blocks"] if (b["row"], b["col"]) == (0, 0))
    return next(p for p in block["params"] if p["name"] == name)


def test_set_writes_one_knob_and_records_history(device, fake):
    before = device.grid()
    s = editor.EditorSession()
    old = amp_param(device, "BASS")["real"]
    r = s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS", "real": 7.0,
                       "expect": old})
    assert r["param"] == {"value": 7.0} and r["id"]
    after = device.grid()
    diff = panel.grid_diff(before, after)
    assert diff == [f"行1 列1 Brit TM45 Normal BASS: {old} → 7.0"]   # one value only
    (entry,) = panel.history_view(True)
    assert entry["engine"] == "editor" and "BASS" in entry["instruction"]
    panel.undo(device)
    assert amp_param(device, "BASS")["real"] == pytest.approx(old, abs=1e-3)
    panel.redo(device)
    assert amp_param(device, "BASS")["real"] == pytest.approx(7.0)


def test_switch_moves_by_option_name_only(device, fake):
    s = editor.EditorSession()
    r = s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "CHANNEL",
                       "option": "Drive", "expect": "Clean"})
    assert r["param"]["option"] == "Drive"
    with pytest.raises(editor.EditorError, match="スイッチ"):
        s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "CHANNEL", "real": 0.0})
    with pytest.raises(editor.EditorError, match="選択肢はありません"):
        s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "CHANNEL", "option": "Lead"})


def test_string_parameters_are_display_only(device):
    s = editor.EditorSession()
    with pytest.raises(editor.EditorError, match="文字列"):
        s.set(device, {"row": 0, "col": 1, "model_id": 12001, "param": "ir selector",
                       "real": 0.5})


def test_bypass_toggle_and_conflict_when_the_unit_changed(device):
    s = editor.EditorSession()
    r = s.set(device, {"row": 0, "col": 2, "model_id": 4001, "bypassed": True, "expect": False})
    assert r["param"] == {"bypassed": True}
    with pytest.raises(editor.EditorConflict):
        s.set(device, {"row": 0, "col": 2, "model_id": 4001, "bypassed": False, "expect": False})


def test_conflict_writes_nothing(device, fake):
    s = editor.EditorSession()
    old = amp_param(device, "BASS")["real"]
    since = len(fake.calls)
    with pytest.raises(editor.EditorConflict, match="本体側で変わって"):
        s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS", "real": 9.0,
                       "expect": old + 2})
    with pytest.raises(editor.EditorConflict, match="ブロックが画面と違います"):
        s.set(device, {"row": 0, "col": 0, "model_id": 1001, "param": "BASS", "real": 9.0})
    assert not [c for c in fake.calls[since:] if c[0] == "set_param"]
    assert panel.history_view(True) == []


def test_placeholder_knob_writes_normalized_value(device):
    s = editor.EditorSession()
    r = s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "LEVEL", "value": 0.5})
    assert r["param"]["value"] == pytest.approx(0.5)
    with pytest.raises(editor.EditorError, match="value"):
        s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "LEVEL", "real": -3.0})


def test_same_value_records_nothing(device):
    s = editor.EditorSession()
    old = amp_param(device, "GAIN 2")["real"]
    r = s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "GAIN 2", "real": old})
    assert r["id"] is None and panel.history_view(True) == []


def test_readonly_policy_refuses(device, monkeypatch):
    monkeypatch.setattr(device.policy, "check_grid", lambda what: (_ for _ in ()).throw(
        editor.EditorError("読み取り専用")))
    s = editor.EditorSession()
    with pytest.raises(editor.EditorError, match="読み取り専用"):
        s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS", "real": 2.0})


# -- gestures (drag = one undo unit) -----------------------------------------------------

def set_params(fake, since, name):
    return [c for c in fake.calls[since:] if c[0] == "set_param" and c[2].get("param") == name]


def test_drag_streams_moves_and_ends_as_one_history_entry(device, fake):
    s = editor.EditorSession()
    old = amp_param(device, "TREBLE")["real"]
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "TREBLE", "expect": old})
    since = len(fake.calls)
    for seq, v in enumerate([4.0, 4.5, 5.0, 5.5], 1):
        assert s.move(device, g["gesture"], {"seq": seq, "real": v}) == {"sent": True}
    assert amp_param(device, "TREBLE")["real"] == pytest.approx(5.5)
    r = s.end(device, g["gesture"], {"real": 6.25})
    assert r["param"]["value"] == pytest.approx(6.25)
    assert [c[2]["real"] for c in set_params(fake, since, "TREBLE")] == [4.0, 4.5, 5.0, 5.5, 6.25]
    (entry,) = panel.history_view(True)
    assert "TREBLE" in entry["instruction"] and entry["engine"] == "editor"
    panel.undo(device)                                    # back to before the drag, not 5.5
    assert amp_param(device, "TREBLE")["real"] == pytest.approx(old, abs=1e-3)
    assert s.pending() == 0


def test_stale_moves_are_dropped_and_the_final_value_is_always_sent(device, fake):
    s = editor.EditorSession()
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "GAIN 1"})
    assert s.move(device, g["gesture"], {"seq": 5, "real": 6.0})["sent"]
    assert s.move(device, g["gesture"], {"seq": 3, "real": 2.0}) == {"sent": False}   # older
    assert s.move(device, g["gesture"], {"seq": 5, "real": 2.0}) == {"sent": False}   # repeat
    since = len(fake.calls)
    s.end(device, g["gesture"], {"real": 7.5})
    assert [c[2]["real"] for c in set_params(fake, since, "GAIN 1")] == [7.5]
    with pytest.raises(editor.EditorError, match="もう終わって"):
        s.move(device, g["gesture"], {"seq": 9, "real": 1.0})


def test_a_move_waiting_behind_a_newer_one_is_dropped(device, fake):
    """While a write is in flight, only the newest queued value is sent afterwards."""
    s = editor.EditorSession()
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "MIDDLE"})
    gesture = s._gestures[g["gesture"]]
    release = threading.Event()
    original = device.set_knob
    calls = []

    def slow(*a, **kw):
        calls.append(kw)
        if len(calls) == 1:
            release.wait(5)
        return original(*a, **kw)

    device.set_knob = slow
    results = {}
    t1 = threading.Thread(target=lambda: results.setdefault(1, s.move(
        device, g["gesture"], {"seq": 1, "real": 3.0})))
    t1.start()
    while not calls:
        pass
    t2 = threading.Thread(target=lambda: results.setdefault(2, s.move(
        device, g["gesture"], {"seq": 2, "real": 4.0})))
    t3 = threading.Thread(target=lambda: results.setdefault(3, s.move(
        device, g["gesture"], {"seq": 3, "real": 5.0})))
    t2.start()
    while gesture.newest < 2:
        pass
    t3.start()
    while gesture.newest < 3:
        pass
    release.set()
    for t in (t1, t2, t3):
        t.join(5)
    assert results[1]["sent"] and results[3]["sent"] and not results[2]["sent"]
    assert [c["real"] for c in calls] == [3.0, 5.0]


def test_drag_back_to_the_start_records_nothing(device):
    s = editor.EditorSession()
    old = amp_param(device, "GAIN 2")["real"]
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "GAIN 2", "expect": old})
    s.move(device, g["gesture"], {"seq": 1, "real": 9.0})
    r = s.end(device, g["gesture"], {"real": old})
    assert r["id"] is None and panel.history_view(True) == []
    assert amp_param(device, "GAIN 2")["real"] == pytest.approx(old)


def test_switches_cannot_be_dragged(device):
    s = editor.EditorSession()
    with pytest.raises(editor.EditorError, match="つまみではありません"):
        s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "CHANNEL"})
    with pytest.raises(Exception, match="option name"):
        device.set_knob(0, 0, 1038, "CHANNEL", value=1.0)


def test_abandoned_drag_is_recorded_so_undo_can_revert_it(device):
    s = editor.EditorSession(stale_seconds=0.0)
    old = amp_param(device, "PRESENCE")["real"]
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "PRESENCE"})
    s.move(device, g["gesture"], {"seq": 1, "real": 9.0})
    s._gestures[g["gesture"]].touched -= 1
    ids = s.finish_stale(device)
    assert len(ids) == 1 and s.pending() == 0
    (entry,) = panel.history_view(True)
    assert "途中で終わりました" in entry["instruction"]
    panel.undo(device)
    assert amp_param(device, "PRESENCE")["real"] == pytest.approx(old, abs=1e-3)


# -- panel <-> editor share one history -------------------------------------------------

def test_panel_and_editor_see_each_others_changes(device):
    s = editor.EditorSession()
    s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS", "real": 2.0})
    view = panel.grid_view(device)
    knobs = {k["name"]: k["display"] for k in view["rows"][0]["cells"][0]["knobs"]}
    assert knobs["BASS"].startswith("2.0")
    panel.apply(device, {"ops": [{"op": "set_param", "row": 0, "col": 0, "param": "BASS",
                                  "real": 8.0}]}, instruction="低音を上げて")
    v = editor.view(device)
    bass = next(p for p in cell(v, 0, 0)["params"] if p["name"] == "BASS")
    assert bass["value"] == pytest.approx(8.0)
    assert [h["engine"] for h in panel.history_view(True)] == ["editor", "rules"]
    panel.ab_from_last(device)
    panel.ab_switch(device, "A")
    v = editor.view(device)
    assert next(p for p in cell(v, 0, 0)["params"] if p["name"] == "BASS")["value"] == \
        pytest.approx(2.0)


# -- web routes ---------------------------------------------------------------------------

def test_routes_set_readback_history_undo(client):
    v = client.get("/api/editor/grid").json()
    amp = v["rows"][0]["cells"][0]
    bass = next(p for p in amp["params"] if p["name"] == "BASS")
    r = client.post("/api/editor/set", json={"row": 0, "col": 0, "model_id": amp["model_id"],
                                             "param": "BASS", "real": 6.5,
                                             "expect": bass["value"]})
    assert r.status_code == 200 and r.json()["param"]["value"] == pytest.approx(6.5)
    hist = client.get("/api/panel/history").json()
    assert hist[-1]["engine"] == "editor"
    assert client.post("/api/panel/undo").status_code == 200
    v = client.get("/api/editor/grid").json()
    bass2 = next(p for p in v["rows"][0]["cells"][0]["params"] if p["name"] == "BASS")
    assert bass2["value"] == pytest.approx(bass["value"], abs=1e-3)
    assert client.post("/api/panel/redo").status_code == 200


def test_routes_conflict_is_409(client):
    r = client.post("/api/editor/set", json={"row": 0, "col": 0, "model_id": 1038,
                                             "param": "BASS", "real": 6.5, "expect": 9.99})
    assert r.status_code == 409 and r.json()["conflict"] is True
    r = client.post("/api/editor/begin", json={"row": 0, "col": 0, "model_id": 999,
                                               "param": "BASS"})
    assert r.status_code == 409


def test_routes_gesture(client):
    g = client.post("/api/editor/begin", json={"row": 0, "col": 0, "model_id": 1038,
                                               "param": "GAIN 1"}).json()["gesture"]
    assert client.post("/api/editor/move", json={"gesture": g, "seq": 1, "real": 3.0}).json() == \
        {"sent": True}
    assert client.post("/api/editor/move", json={"gesture": g, "seq": 1, "real": 3.1}).json() == \
        {"sent": False}
    r = client.post("/api/editor/end", json={"gesture": g, "real": 3.3}).json()
    assert r["param"]["value"] == pytest.approx(3.3) and r["id"]
    assert client.post("/api/editor/end", json={"gesture": g, "real": 3.3}).status_code == 400


def test_routes_refused_while_a_job_runs(client):
    client.app.state.qc.jobs.busy = lambda: "job123"
    r = client.post("/api/editor/set", json={"row": 0, "col": 0, "model_id": 1038,
                                             "param": "BASS", "real": 6.5})
    assert r.status_code == 409 and "ジョブ" in r.json()["error"]


def test_routes_need_the_session_cookie(fake):
    from fastapi.testclient import TestClient
    from qcsuite.web.app import create_app
    with TestClient(create_app(mock=True, fake=fake)) as c:
        assert c.post("/api/editor/set", json={}).status_code == 403


def test_editor_page_is_served_and_listed(client):
    page = client.get("/static/editor.html")
    assert page.status_code == 200 and "editor.js" in page.text and "editor_core.js" in page.text
    assert '["editor", "エディター"]' in client.get("/static/common.js").text


def test_cli_editor_show(monkeypatch, capsys, tmp_path):
    from qcsuite import cli
    monkeypatch.setenv("QCSUITE_FAKE_SPEED", "0")
    try:
        cli.main(["--mock", "editor", "show"])
    except SystemExit as exc:
        assert not exc.code
    out = capsys.readouterr()
    assert "保存していません" in out.out + out.err


# -- the JavaScript display rules ---------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_editor_core_js():
    test = ROOT / "tests/js/editor_core.test.js"
    if not test.exists():
        pytest.skip("JS test not present")
    done = subprocess.run(["node", str(test)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_python_and_js_agree_on_display_rules(tmp_path):
    """Run the same cases through editor_core.js and editor.py."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    core = ROOT / "qcsuite/web/static/editor_core.js"
    if not core.exists():
        pytest.skip("editor_core.js not present")
    import json
    cases = [
        (knob(min=-60, max=12, skew=3.8018, units="dB", min_string="OFF"), v)
        for v in (-60, -20, 0, 12)] + [
        (knob(min=20, max=500, units="Hz", int=True, skew=0.3), v) for v in (20, 80.4, 499)] + [
        (knob(min=0.016, max=0.8, units="ms", skew=0.6, steps=300), v) for v in (0.016, 0.3, 0.8)] + [
        (knob(min=0, max=100, units="%"), 17.4), (knob(), 0.0), (knob(), 0.5),
        (knob(placeholder=True, min=0.0, max=1.0), 0.7692)]
    script = tmp_path / "agree.js"
    script.write_text(
        f"const C = require({json.dumps(str(core))});\n"
        f"const cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map(([p, v]) => {\n"
        "  const f = C.formatValue(p, v);\n"
        "  return [f.unit ? f.text + ' ' + f.unit : f.text, C.angle(p, v)];\n"
        "})));\n")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    js = json.loads(done.stdout)
    for (p, v), (text, angle) in zip(cases, js):
        assert text == editor.value_text(p, v), (p, v)
        assert angle == pytest.approx(editor.knob_angle(p, v), abs=1e-6), (p, v)




def test_switch_without_names_is_refused_by_value_on_every_route(device, fake):
    del fake.live.chains[0].models[0].params[8].dynamic_steps[:]
    s = editor.EditorSession()
    with pytest.raises(editor.EditorError, match="スイッチ"):
        s.set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "CHANNEL", "real": 1.0})
    with pytest.raises(editor.EditorError, match="スイッチ"):
        s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "CHANNEL"})
    assert panel.history_view(True) == []


def test_block_swapped_mid_drag_is_a_conflict_at_end(device):
    s = editor.EditorSession()
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS"})
    device.apply_patch({"ops": [{"op": "set_block", "row": 0, "col": 0, "model": 1001}]})
    with pytest.raises(editor.EditorConflict):
        s.end(device, g["gesture"], {"real": 9.0})
    assert panel.history_view(True) == []


# -- round 2: header, device list, replace, serialized edits ------------------------------

def test_view_header_has_preset_name_dirty_and_tempo(device, fake):
    v = editor.view(device)
    assert v["preset"]["name"] == fake.live.name and v["preset"]["dirty"] is False
    assert len(v["scene_labels"]) <= 8
    editor.EditorSession().set(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS",
                                        "real": 2.0})
    fake.dirty = True
    assert editor.view(device)["preset"]["dirty"] is True


def test_catalog_listing_groups_and_greys(device):
    groups = {g["key"]: g for g in editor.catalog_listing(device)["groups"]}
    assert list(groups)[:3] == ["plugins", "amp", "capture"]
    amp = groups["amp"]
    assert amp["has_bass"] and any(m["bass"] for m in amp["models"])
    names = [m["name"] for m in amp["models"]]
    assert names == sorted(names, key=str.lower)
    assert not any(m["plugin"] for m in amp["models"])
    plugins = {m["name"]: m for m in groups["plugins"]["models"]}
    assert not plugins["Plini Clean"]["usable"] and "未所有" in plugins["Plini Clean"]["why_not"]
    assert plugins["The Amp Snob"]["usable"]
    assert not any(m["usable"] for m in groups["capture"]["models"])
    every = [m["id"] for g in groups.values() for m in g["models"]]
    assert 23000 not in every and len(every) == len(set(every))     # internal / no duplicates


def test_replace_reads_back_and_is_one_undo_unit(device):
    s = editor.EditorSession()
    r = s.replace(device, {"row": 0, "col": 0, "model_id": 1038, "new_model_id": 1001})
    assert r["id"] and r["model_id"] == 1001
    grid = device.grid()
    assert next(b for b in grid["blocks"] if (b["row"], b["col"]) == (0, 0))["model_id"] == 1001
    (entry,) = panel.history_view(True)
    assert "差し替え" in entry["instruction"]
    panel.undo(device)
    grid = device.grid()
    assert next(b for b in grid["blocks"] if (b["row"], b["col"]) == (0, 0))["model_id"] == 1038


def test_replace_into_an_empty_cell_and_refusals(device):
    s = editor.EditorSession()
    s.replace(device, {"row": 2, "col": 3, "model_id": None, "new_model_id": 2})
    with pytest.raises(editor.EditorConflict, match="空いていません"):
        s.replace(device, {"row": 2, "col": 3, "model_id": None, "new_model_id": 1})
    with pytest.raises(editor.EditorConflict):
        s.replace(device, {"row": 2, "col": 3, "model_id": 999, "new_model_id": 1})
    plini = device.catalog.find("Plini Clean").id
    with pytest.raises(editor.EditorError, match="未所有"):
        s.replace(device, {"row": 2, "col": 3, "model_id": 2, "new_model_id": plini})
    with pytest.raises(editor.EditorError, match="キャプチャー"):
        s.replace(device, {"row": 2, "col": 3, "model_id": 2, "new_model_id": 14000})
    with pytest.raises(editor.EditorError, match="一覧にありません"):
        s.replace(device, {"row": 2, "col": 3, "model_id": 2, "new_model_id": 23000})


def test_replace_refused_for_dsp_changes_nothing(device, monkeypatch):
    import pyquadcortex as pq

    def refuse(patch):
        raise pq.BlockRefused("no DSP")
    monkeypatch.setattr(device, "apply_patch", refuse)
    s = editor.EditorSession()
    with pytest.raises(editor.EditorError, match="DSP"):
        s.replace(device, {"row": 0, "col": 0, "model_id": 1038, "new_model_id": 1001})
    assert panel.history_view(True) == []


def test_edits_wait_for_the_drag_in_progress(device):
    s = editor.EditorSession()
    s.IDLE_WAIT = 0.2
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "GAIN 1"})
    with pytest.raises(editor.EditorError, match="まだ終わっていません"):
        s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS"})
    s.move(device, g["gesture"], {"seq": 1, "real": 7.0})
    done = {}
    t = threading.Thread(target=lambda: done.setdefault("r", s.begin(
        device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS"})))
    s.IDLE_WAIT = 5.0
    t.start()
    s.end(device, g["gesture"], {"real": 9.0})
    t.join(5)
    assert "gesture" in done["r"]
    second = s._gestures[done["r"]["gesture"]]
    gain = next(p for p in second.block["params"] if p["name"] == "GAIN 1")
    assert gain["real"] == pytest.approx(9.0)          # its "before" holds the first drag's end
    s.end(device, done["r"]["gesture"], {"real": 8.0})
    panel.undo(device)                                  # reverts BASS only
    assert amp_param(device, "GAIN 1")["real"] == pytest.approx(9.0)


def test_block_swapped_mid_drag_stops_the_moves(device, fake):
    s = editor.EditorSession()
    s.CHECK_EVERY = 2
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS"})
    s.move(device, g["gesture"], {"seq": 1, "real": 4.0})
    s.move(device, g["gesture"], {"seq": 2, "real": 4.5})
    device.apply_patch({"ops": [{"op": "set_block", "row": 0, "col": 0, "model": 1001}]})
    since = len(fake.calls)
    with pytest.raises(editor.EditorConflict):
        s.move(device, g["gesture"], {"seq": 3, "real": 5.0})
    assert not [c for c in fake.calls[since:] if c[0] == "set_param"]
    assert s.pending() == 0


def test_stale_sweep_skips_a_gesture_with_a_write_in_flight(device):
    s = editor.EditorSession(stale_seconds=0.0)
    g = s.begin(device, {"row": 0, "col": 0, "model_id": 1038, "param": "BASS"})
    gesture = s._gestures[g["gesture"]]
    gesture.touched -= 1
    gesture.send_lock.acquire()
    try:
        assert s.finish_stale(device) == [] and s.pending() == 1
    finally:
        gesture.send_lock.release()


def test_routes_catalog_replace_and_end_conflict(client):
    groups = client.get("/api/editor/catalog").json()["groups"]
    assert groups[1]["key"] == "amp"
    r = client.post("/api/editor/replace", json={"row": 0, "col": 0, "model_id": 999,
                                                 "new_model_id": 1001})
    assert r.status_code == 409 and r.json()["conflict"]
    g = client.post("/api/editor/begin", json={"row": 0, "col": 0, "model_id": 1038,
                                               "param": "BASS"}).json()["gesture"]
    client.app.state.qc.device().apply_patch(
        {"ops": [{"op": "set_block", "row": 0, "col": 0, "model": 1001}]})
    r = client.post("/api/editor/end", json={"gesture": g, "real": 5.0})
    assert r.status_code == 409 and r.json()["conflict"]
    r = client.post("/api/editor/replace", json={"row": 0, "col": 0, "model_id": 1001,
                                                 "new_model_id": 1038})
    assert r.status_code == 200 and r.json()["id"]


# -- round 2 review fixes ---------------------------------------------------------------

def test_concurrent_one_shot_edits_get_separate_befores(device):
    original = device.apply_patch

    def slow(patch):
        import time as _t
        _t.sleep(0.1)
        return original(patch)
    device.apply_patch = slow
    s = editor.EditorSession()
    threads = [threading.Thread(target=s.set, args=(device, {
        "row": 0, "col": c, "model_id": m, "bypassed": True})) for c, m in ((0, 1038), (2, 4001))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    device.apply_patch = original
    panel.undo(device)                                   # only the later toggle comes back
    blocks = {(b["row"], b["col"]): b["bypassed"] for b in device.grid()["blocks"]}
    assert sorted([blocks[(0, 0)], blocks[(0, 2)]]) == [False, True]


def test_truncated_model_repo_falls_back_and_is_not_refetched(device, fake, monkeypatch):
    payload = fake._fetch_model_repo()
    calls = []

    def cut(timeout=25.0):
        calls.append(1)
        return payload[: len(payload) // 2]
    monkeypatch.setattr(fake, "_fetch_model_repo", cut, raising=False)
    assert modelrepo.load(device) is None
    assert modelrepo.load(device) is None
    assert editor.view(device)["repo"]["source"] == "none"
    assert len(calls) == 1


def test_corrupt_cache_is_refetched(device):
    fw = device.version()["firmware"]
    modelrepo.cache_path(True, fw).write_bytes(b"\x1f\x8bnot really gzip")
    assert modelrepo.load(device).source == "device"


def test_placeholder_knob_has_no_default_to_reset_to(device):
    level = next(p for p in editor.view(device)["rows"][0]["cells"][0]["params"]
                 if p["name"] == "LEVEL")
    assert level["placeholder"] and level["default"] is None


# -- skew on the wire (measured on the unit, 2026-11-10) ---------------------------------

def test_fix_skew_reads_the_wire_as_a_skewed_position(repo):
    grid = {"blocks": [{"row": 0, "col": 0, "model_id": 12006, "params": [
        {"name": "HPF", "index": 10, "value": 0.0, "real": 20.0, "range": [20, 500]},
        {"name": "PAN", "index": 3, "value": 0.5, "real": 5.0, "range": [0, 10]}]}]}
    editor.fix_skew(grid, repo)
    hpf, pan = grid["blocks"][0]["params"]
    assert hpf["real"] == 20.0 and hpf["skew"] == pytest.approx(0.3)
    assert pan["real"] == 5.0 and "skew" not in pan                   # skew 1: untouched
    grid["blocks"][0]["params"][0].update(value=0.5, real=260.0)
    editor.fix_skew(grid, repo)
    assert grid["blocks"][0]["params"][0]["real"] == pytest.approx(20 + 480 * 0.5 ** (1 / 0.3),
                                                                    abs=1e-3)


def test_skewed_knob_writes_the_skewed_wire_value_and_reads_back(device, fake):
    s = editor.EditorSession()
    since = len(fake.calls)
    r = s.set(device, {"row": 0, "col": 1, "model_id": 12001, "param": "LOW CUT", "real": 80.0})
    (call,) = [c for c in fake.calls[since:] if c[0] == "set_param"]
    assert call[2]["value"] == pytest.approx(((80 - 20) / 480) ** 0.3, abs=1e-6)
    assert call[2]["real"] is None
    assert r["param"]["value"] == pytest.approx(80.0, abs=0.01)
    cab = cell(editor.view(device), 0, 1)
    low = next(p for p in cab["params"] if p["name"] == "LOW CUT")
    assert low["value"] == pytest.approx(80.0, abs=0.01)
    g = s.begin(device, {"row": 0, "col": 1, "model_id": 12001, "param": "LOW CUT",
                         "expect": 80.0})                          # no false conflict
    since = len(fake.calls)
    s.move(device, g["gesture"], {"seq": 1, "real": 200.0})
    (call,) = [c for c in fake.calls[since:] if c[0] == "set_param"]
    assert call[2]["value"] == pytest.approx((180 / 480) ** 0.3, abs=1e-6)
    s.end(device, g["gesture"], {"real": 200.0})


def test_row_fed_by_another_row_shows_prev_row(device):
    device.apply_patch({"ops": [{"op": "set_output", "row": 0, "port": "NEXT_ROW_3"},
                                {"op": "set_block", "row": 2, "col": 0, "model": 2}]})
    rows = editor.view(device)["rows"]
    assert rows[2]["in_port"] == "PREV_ROW" and rows[3]["in_port"] == "EMPTY"
    assert editor._feeds("NEXT_ROW_3_4", 3) and not editor._feeds("XLR_1_2", 2)
