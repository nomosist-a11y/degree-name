"""言葉で操作するパネル: rules, preview/apply, undo, A/B, Claude engine, CLI, web."""

import json
from types import SimpleNamespace

import pytest

from qcsuite.device import fake_data
from helpers import wait_job
from qcsuite.features import panel, panel_claude, panel_rules

GAIN_KNOBS = ["GAIN", "DRIVE", "GAIN 1", "INPUT GAIN"]
SETTERS = {"set_param", "set_block", "remove_block", "set_bypass", "set_chain_input",
           "set_chain_output", "set_lane_output"}


def amp_model(catalog, first_param: str):
    return next(m for m in catalog.factory_models()
                if m.category == "Guitar Amplifier" and not m.superseded
                and m.parameters[0].name == first_param)


def setup_grid(device, first_param: str = "GAIN", free_before_amp: bool = True):
    """Row 0: [comp?] _ amp cab _ Hall, fed from Input 1. Returns the amp model."""
    catalog = device.catalog
    amp = amp_model(catalog, first_param)
    ops = [{"op": "remove_block", "row": b["row"], "col": b["col"]}
           for b in device.grid()["blocks"]]
    ops += [{"op": "set_input", "row": 0, "port": "INPUT_1"},
            {"op": "set_output", "row": 0, "port": "XLR_1_2"},
            {"op": "set_block", "row": 0, "col": 2, "model": amp.id},
            {"op": "set_block", "row": 0, "col": 3,
             "model": fake_data.factory_models.CabsimGuitarM.N412_BRIT_60B_GB_71_M},
            {"op": "set_block", "row": 0, "col": 5, "model": fake_data.factory_models.Reverb.HALL}]
    if not free_before_amp:
        ops += [{"op": "set_block", "row": 0, "col": 0,
                 "model": fake_data.factory_models.Compressor.OPTO_COMP_M},
                {"op": "set_block", "row": 0, "col": 1,
                 "model": fake_data.factory_models.Equalizer.PARAMETRIC_3}]
    device.apply_patch({"ops": ops})
    return amp


def param(grid, row, col, name):
    block = next(b for b in grid["blocks"] if b["row"] == row and b["col"] == col)
    return next(p for p in block["params"] if p["name"] == name)


def setter_calls(fake, since):
    return [c for c in fake.calls[since:] if c[0] in SETTERS]


# -- the grid view ---------------------------------------------------------------------

def test_grid_view_flags_blocks_that_do_not_respond(device):
    setup_grid(device)
    plini = device.catalog.find("Plini Lead").id
    device.apply_patch({"ops": [{"op": "set_block", "row": 1, "col": 0, "model": 14000},
                                {"op": "set_block", "row": 2, "col": 0, "model": plini}]})
    view = panel.grid_view(device)
    cells = {(c["row"], c["col"]): c for r in view["rows"] for c in r["cells"] if c}
    assert not cells[(1, 0)]["responds"] and "キャプチャー" in cells[(1, 0)]["flags"][0]
    assert not cells[(2, 0)]["responds"] and "未所有" in " ".join(cells[(2, 0)]["flags"])
    cab = cells[(0, 3)]
    assert cab["responds"] and any("文字列" in f for f in cab["flags"])
    amp = cells[(0, 2)]
    assert amp["responds"] and not amp["flags"]
    assert view["rows"][0]["is_input"] and view["rows"][0]["reaches_output"]
    assert "行1: [INPUT_1]" in panel.grid_text(view)


# -- rules ------------------------------------------------------------------------------------

@pytest.mark.parametrize("knob", GAIN_KNOBS)
def test_gain_words_find_each_amps_own_gain_knob(device, knob):
    setup_grid(device, knob)
    up = panel.preview(device, "歪みを増やして")
    assert up["can_apply"]
    (op,) = up["patch"]["ops"]
    assert (op["param"], op["col"]) == (knob, 2)
    assert op["value"] == pytest.approx(0.6)          # default 5/10 + one step
    numeric = panel.preview(device, "ゲインを 6 に")
    assert numeric["patch"]["ops"] == [{"op": "set_param", "row": 0, "col": 2, "param": knob,
                                        "real": 6.0}]


def test_tone_words_and_intensity(device):
    setup_grid(device, "GAIN")
    def treble_step(text):
        ops = panel.preview(device, text)["patch"]["ops"]
        return {o["param"]: round(o["value"] - 0.5, 4) for o in ops}
    assert treble_step("暗く") == {"TREBLE": -0.1, "PRESENCE": -0.1}
    assert treble_step("少しだけ暗く") == {"TREBLE": -0.05, "PRESENCE": -0.05}
    assert treble_step("もう少し暗く") == {"TREBLE": -0.075, "PRESENCE": -0.075}
    assert treble_step("かなり明るく") == {"TREBLE": 0.2, "PRESENCE": 0.2}
    assert treble_step("低音を減らして") == {"BASS": -0.1}
    assert treble_step("中域を出して") == {"MIDDLE": 0.1}
    assert treble_step("make it darker") == {"TREBLE": -0.1, "PRESENCE": -0.1}
    both = panel.preview(device, "低音を減らして、中域を出して")
    assert [o["param"] for o in both["patch"]["ops"]] == ["BASS", "MIDDLE"]


def test_mid_resolves_to_mid_or_middle(device):
    setup_grid(device, "DRIVE")                       # this layout calls it MID
    ops = panel.preview(device, "中域を出して")["patch"]["ops"]
    assert [o["param"] for o in ops] == ["MID"]


def test_targets_by_column_category_and_model_name(device):
    setup_grid(device, "GAIN")
    ops = panel.preview(device, "列4の LOW CUT を 120 に")["patch"]["ops"]
    assert ops == [{"op": "set_param", "row": 0, "col": 3, "param": "LOW CUT", "real": 120.0}]
    ops = panel.preview(device, "リバーブを深く")["patch"]["ops"]
    assert ops[0]["col"] == 5 and ops[0]["param"] == "MIX"
    ops = panel.preview(device, "Hall の DECAY を 3 に")["patch"]["ops"]
    assert ops == [{"op": "set_param", "row": 0, "col": 5, "param": "DECAY", "real": 3.0}]
    missing = panel.preview(device, "列8の GAIN を 3 に")
    assert not missing["can_apply"] and "ブロックがありません" in missing["problems"][0]


def test_switches_move_only_by_option_name(device, fake):
    setup_grid(device, "GAIN")                         # this layout has a BRIGHT switch
    raw = panel.preview(device, "BRIGHT を 1 に")
    assert not raw["can_apply"] and "スイッチ" in raw["problems"][0]
    named = panel.preview(device, "BRIGHT をオンに")
    assert named["patch"]["ops"] == [{"op": "set_option", "row": 0, "col": 2,
                                      "param": "BRIGHT", "option": "On"}]
    panel.apply(device, named["patch"], "BRIGHT をオンに")
    assert param(device.grid(), 0, 2, "BRIGHT")["option"] == "On"
    with pytest.raises(panel.PanelError, match="スイッチ"):
        panel.apply(device, {"ops": [{"op": "set_param", "row": 0, "col": 2,
                                      "param": "BRIGHT", "value": 0.0}]})


def test_unknown_or_out_of_range_sends_nothing(device, fake):
    setup_grid(device, "GAIN")
    before = len(fake.calls)
    for text in ("ほげほげ", "暗く、ふがふが", "ゲインを 12 に", ""):
        result = panel.preview(device, text)
        assert not result["can_apply"] and result["problems"], text
    partial = panel.preview(device, "暗く、ふがふが")
    assert len(partial["summary"]) == 2               # what WAS understood is still shown
    assert "範囲" in panel.preview(device, "ゲインを 12 に")["problems"][0]
    assert setter_calls(fake, before) == []


def test_boost_goes_into_a_free_cell_before_the_amp(device):
    setup_grid(device, "GAIN")
    result = panel.preview(device, "ブースト追加")
    ops = result["patch"]["ops"]
    assert ops[0] == {"op": "set_block", "row": 0, "col": 1, "model": "Green 808"}
    assert {o["param"] for o in ops[1:]} <= {"GAIN", "DRIVE", "LEVEL"}
    panel.apply(device, result["patch"], "ブースト追加")
    boost = next(b for b in device.grid()["blocks"] if b["col"] == 1)
    assert boost["model"] == "Green 808" and boost["bypassed"] is False
    setup_grid(device, "GAIN", free_before_amp=False)
    full = panel.preview(device, "ブースト追加")
    assert not full["can_apply"] and "空きセル" in full["problems"][0]


def test_add_and_bypass_effects(device):
    setup_grid(device, "GAIN")
    delay = panel.preview(device, "ディレイを足して")
    assert delay["patch"]["ops"] == [{"op": "set_block", "row": 0, "col": 6,
                                      "model": "Analog Delay M"}]
    off = panel.preview(device, "リバーブを外して")
    assert off["patch"]["ops"] == [{"op": "bypass", "row": 0, "col": 5, "bypassed": True}]
    panel.apply(device, off["patch"])
    again = panel.preview(device, "リバーブを足して")      # exists but bypassed: re-enable
    assert again["patch"]["ops"] == [{"op": "bypass", "row": 0, "col": 5, "bypassed": False}]
    on = panel.preview(device, "リバーブを戻して")
    assert on["patch"]["ops"] == [{"op": "bypass", "row": 0, "col": 5, "bypassed": False}]


# -- preview, apply, undo ---------------------------------------------------------------------

def test_preview_does_not_touch_the_unit_and_apply_does(device, fake):
    setup_grid(device, "GAIN")
    grid_before = device.grid()
    calls = len(fake.calls)
    result = panel.preview(device, "少し暗く")
    assert device.grid() == grid_before and setter_calls(fake, calls) == []
    applied = panel.apply(device, result["patch"], "少し暗く", summary=result["summary"])
    assert param(device.grid(), 0, 2, "TREBLE")["value"] == pytest.approx(0.45)
    assert applied["applied"] == 2 and "保存はしていません" in applied["note"]
    assert panel.history_view(device.mock)[-1]["instruction"] == "少し暗く"


def test_undo_and_redo(device):
    setup_grid(device, "GAIN")
    panel.apply(device, panel.preview(device, "BRIGHT をオンに")["patch"], "bright")
    panel.apply(device, panel.preview(device, "かなり暗く")["patch"], "darker")
    assert param(device.grid(), 0, 2, "TREBLE")["value"] == pytest.approx(0.3)
    undone = panel.undo(device)
    assert undone["instruction"] == "darker"
    assert param(device.grid(), 0, 2, "TREBLE")["value"] == pytest.approx(0.5)
    panel.undo(device)
    assert param(device.grid(), 0, 2, "BRIGHT")["option"] == "Off"
    panel.redo(device)
    assert param(device.grid(), 0, 2, "BRIGHT")["option"] == "On"
    panel.redo(device)
    assert param(device.grid(), 0, 2, "TREBLE")["value"] == pytest.approx(0.3)
    with pytest.raises(panel.PanelError, match="やり直せる"):
        panel.redo(device)
    hist = panel.history_view(device.mock)
    assert [h["undone"] for h in hist] == [False, False]


def test_undo_restores_structure(device):
    setup_grid(device, "GAIN")
    panel.apply(device, panel.preview(device, "ディレイを足して")["patch"], "delay")
    assert any(b["col"] == 6 for b in device.grid()["blocks"])
    panel.undo(device)
    assert not any(b["col"] == 6 for b in device.grid()["blocks"])


def test_restore_patch_skips_unchanged_and_uses_option_names(device):
    setup_grid(device, "GAIN")
    a = device.grid()
    panel.apply(device, {"ops": [{"op": "set_option", "row": 0, "col": 2, "param": "BRIGHT",
                                  "option": "On"},
                                 {"op": "set_param", "row": 0, "col": 2, "param": "BASS",
                                  "value": 0.9}]})
    patch, notes = panel.restore_patch(a, device.grid())
    kinds = sorted((o["op"], o.get("param")) for o in patch["ops"])
    assert kinds == [("set_option", "BRIGHT"), ("set_param", "BASS")]
    assert any("文字列" in n for n in notes)


# -- A/B --------------------------------------------------------------------------------------------

def test_ab_round_trip(device):
    setup_grid(device, "GAIN")
    panel.ab_capture(device, "A")
    panel.apply(device, panel.preview(device, "かなり暗く")["patch"], "darker")
    summary = panel.ab_capture(device, "b")
    assert summary["active"] == "B" and any("TREBLE" in d for d in summary["diff"])
    panel.ab_switch(device, "A")
    assert param(device.grid(), 0, 2, "TREBLE")["value"] == pytest.approx(0.5)
    panel.ab_switch(device, "B")
    assert param(device.grid(), 0, 2, "TREBLE")["value"] == pytest.approx(0.3)
    with pytest.raises(panel.PanelError):
        panel.ab_capture(device, "C")


def test_ab_from_last_change(device):
    setup_grid(device, "DRIVE")
    with pytest.raises(panel.PanelError):
        panel.ab_from_last(device)
    panel.apply(device, panel.preview(device, "歪みを増やして")["patch"], "more gain")
    ab = panel.ab_from_last(device)
    assert ab["active"] == "B"
    assert [d for d in ab["diff"] if "DRIVE" in d] == [
        next(d for d in ab["diff"] if d.endswith("DRIVE: 5.0 → 6.0"))]
    panel.ab_switch(device, "A")
    assert param(device.grid(), 0, 2, "DRIVE")["value"] == pytest.approx(0.5)


# -- saving ---------------------------------------------------------------------------------------

def test_save_is_a_sandbox_plan(device, fake):
    from qcsuite import plan as plan_mod
    setup_grid(device, "GAIN")
    p = panel.save_plan(device, "Panel result")
    (step,) = p.steps
    assert step.action == "save" and step.folder.endswith("_qcsuite_sandbox")
    assert plan_mod.execute(p, device)["dry_run"]
    plan_mod.execute(p, device, confirm=True)
    assert fake.folders[step.folder][step.position].name == "Panel result"
    from qcsuite.errors import WriteRefused
    with pytest.raises(WriteRefused):
        panel.save_plan(device, "nope", folder="kami")


# -- Claude engine ------------------------------------------------------------------------------

class FakeClaude:
    """Stands in for anthropic.Anthropic: records the request, returns a canned answer."""

    def __init__(self, answer=None, stop_reason="end_turn"):
        self.requests = []
        self.answer = answer
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **body):
        self.requests.append(body)
        text = json.dumps(self.answer, ensure_ascii=False)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=text)])


def test_claude_engine_valid_patch_is_shown_not_applied(device, fake):
    setup_grid(device, "GAIN")
    client = FakeClaude({"ops": [{"op": "set_param", "row": 0, "col": 2, "param": "TREBLE",
                                  "real": 4.0}],
                         "summary": ["行1 列3 TREBLE: 5.0 → 4.0"], "not_understood": []})
    calls = len(fake.calls)
    result = panel.preview(device, "少し暗く", engine="claude", client=client)
    assert result["engine"] == "claude" and result["can_apply"]
    assert setter_calls(fake, calls) == []
    body = client.requests[0]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "never judge tone" in body["system"]
    sent = json.loads(body["messages"][0]["content"])
    assert sent["instruction"] == "少し暗く" and sent["grid"]["blocks"]


def test_claude_engine_invalid_patches_are_rejected(device):
    setup_grid(device, "GAIN")
    bad_param = FakeClaude({"ops": [{"op": "set_param", "row": 0, "col": 2,
                                     "param": "SPARKLE", "value": 0.5}],
                            "summary": [], "not_understood": []})
    result = panel.preview(device, "x", engine="claude", client=bad_param)
    assert not result["can_apply"] and "合いません" in result["problems"][0]
    raw_switch = FakeClaude({"ops": [{"op": "set_param", "row": 0, "col": 2,
                                      "param": "BRIGHT", "value": 1.0}],
                             "summary": [], "not_understood": []})
    result = panel.preview(device, "x", engine="claude", client=raw_switch)
    assert not result["can_apply"] and "スイッチ" in result["problems"][0]
    unclear = FakeClaude({"ops": [], "summary": [], "not_understood": ["音の判断はしません"]})
    assert not panel.preview(device, "良い音に", engine="claude", client=unclear)["can_apply"]


def test_claude_refusal_falls_back_to_rules(device):
    setup_grid(device, "GAIN")
    refused = FakeClaude({"ops": []}, stop_reason="refusal")
    result = panel.preview(device, "少し暗く", engine="claude", client=refused)
    assert result["engine"] == "rules" and "断りました" in result["fallback"]
    assert result["can_apply"]


def test_claude_without_key_falls_back(device, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    setup_grid(device, "GAIN")
    assert not panel.engines()["claude"]["available"]
    result = panel.preview(device, "少し暗く", engine="claude")
    assert result["engine"] == "rules" and "ANTHROPIC_API_KEY" in result["fallback"]
    monkeypatch.setenv("QCSUITE_CLAUDE_MODEL", "claude-sonnet-5-5")
    assert panel_claude.request_body("x", device.grid(), device.catalog)["model"] == \
        "claude-sonnet-5-5"


def test_rules_split_and_normalize():
    assert panel_rules.split_clauses("暗くして、ゲインを上げて。") == ["暗くして", "ゲインを上げて"]
    assert panel_rules.normalize_text("ゲインを６に") == "ゲインを6に"


# -- CLI and web ----------------------------------------------------------------------------------

def test_cli_say_apply_undo_and_ab(capsys):
    from qcsuite.cli import main
    main(["--mock", "panel", "say", "少し暗く"])
    out = capsys.readouterr()
    assert "プレビューのみ" in out.out and "TREBLE" in out.err
    main(["--mock", "panel", "say", "少し暗く", "--apply"])
    assert "適用しました" in capsys.readouterr().out
    main(["--mock", "--json", "panel", "history"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[-1]["instruction"] == "少し暗く" and not rows[-1]["undone"]
    main(["--mock", "panel", "ab", "last"])
    assert "A と B の違い" in capsys.readouterr().out
    main(["--mock", "panel", "ab", "switch", "A"])
    assert "A に切り替えました" in capsys.readouterr().out
    main(["--mock", "panel", "undo"])
    assert "取り消しました" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="何も送っていません"):
        main(["--mock", "panel", "say", "ほげほげ", "--apply"])
    main(["--mock", "panel", "show"])
    assert "シーン" in capsys.readouterr().out
    main(["--mock", "panel", "save", "cli result"])
    assert "dry-run" in capsys.readouterr().err


def test_web_flow(client):
    client.post("/api/connect")
    setup_grid(client.app.state.qc.device(), "GAIN")
    grid = client.get("/api/panel/grid").json()
    assert grid["rows"][0]["cells"][2]["model"]
    assert client.get("/api/panel/engines").json()["rules"]["available"]
    preview = client.post("/api/panel/preview", json={"instruction": "少し暗く"}).json()
    assert preview["can_apply"]
    bad = client.post("/api/panel/preview", json={"instruction": "ほげ"}).json()
    assert not bad["can_apply"]
    r = client.post("/api/panel/apply", json={"patch": preview["patch"],
                                              "instruction": "少し暗く",
                                              "summary": preview["summary"]})
    assert r.status_code == 200 and r.json()["applied"] == 2
    assert client.post("/api/panel/apply", json={"patch": {"ops": []}}).status_code == 400
    assert client.get("/api/panel/history").json()[-1]["instruction"] == "少し暗く"
    assert client.post("/api/panel/ab/last").json()["active"] == "B"
    assert client.post("/api/panel/ab/switch", json={"slot": "A"}).json()["active"] == "A"
    assert client.post("/api/panel/ab/capture", json={"slot": "B"}).status_code == 200
    assert client.post("/api/panel/undo").json()["instruction"] == "少し暗く"
    plan = client.post("/api/panel/save", json={"name": "web result"}).json()
    assert not plan["refused"] and plan["steps"][0]["action"] == "save"
    job = client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": "実行"}).json()["job"]
    assert wait_job(client, job)["summary"]["status"] == "done"
    assert "panel.js" in client.get("/static/panel.html").text


def test_chained_te_forms_are_all_read(device):
    setup_grid(device, "GAIN")
    result = panel.preview(device, "少し暗くしてゲインを上げて")
    assert [o["param"] for o in result["patch"]["ops"]] == ["TREBLE", "PRESENCE", "GAIN"]
    result = panel.preview(device, "リバーブを外してディレイを足して")
    assert [o["op"] for o in result["patch"]["ops"]] == ["bypass", "set_block"]
    boost_verb = panel.preview(device, "低音をブーストして")
    assert boost_verb["patch"]["ops"] == [{"op": "set_param", "row": 0, "col": 2,
                                           "param": "BASS", "value": 0.6}]
