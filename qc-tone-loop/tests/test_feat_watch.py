"""Edit watcher (idea D) and linked takes (idea H), against the fake unit."""

import json
import time

import pytest
import soundfile as sf

from helpers import route_usb, wait_job
from qcsuite import presets
from qcsuite.audio import di as di_mod
from qcsuite.device import fake_data
from qcsuite.errors import WriteRefused
from qcsuite.features import watch, watch_diff

USER = "/media/p4/Presets"
KAMI = f"{USER}/kami"
REVERB = fake_data.factory_models.Reverb.HALL


def _amp(device):
    grid = device.grid()
    amp = next(b for b in grid["blocks"] if b["category"] == "Guitar Amplifier")
    knob = next(p["name"] for p in amp["params"] if p["name"] in ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1"))
    return amp, knob


def _free_col(device, row=0):
    used = {b["col"] for b in device.grid()["blocks"] if b["row"] == row}
    return next(c for c in range(8) if c not in used)


@pytest.fixture
def watching(device):
    device.read_stored(KAMI, 4)                     # "Brit 2203"
    w = watch.Watcher(device, mock=True, interval=0.01)
    w.start()
    return w


def _settle(w):
    """A quiet poll: knobs that stopped moving are written to the log."""
    w.poll()
    return [e for e in w.session.entries() if e["type"] == "change"]


def test_polling_logs_knobs_bypass_blocks_routing_and_scenes(device, fake, watching):
    w = watching
    amp, knob = _amp(device)
    device.apply_patch({"ops": [{"op": "set_param", "row": 0, "col": amp["col"], "param": knob,
                                 "value": 0.9}]})
    pending = w.poll()
    assert pending and pending[0]["kind"] == "param"
    changes = _settle(w)
    assert changes[-1]["where"].endswith(knob) and changes[-1]["after"] == "9.0"
    col = _free_col(device)
    device.apply_patch({"ops": [{"op": "bypass", "row": 0, "col": amp["col"], "bypassed": True},
                                {"op": "set_block", "row": 0, "col": col, "model": REVERB},
                                {"op": "set_input", "row": 0, "port": "USB_5"}]})
    w.poll()
    device.apply_patch({"ops": [{"op": "remove_block", "row": 0, "col": col}]})
    w.poll()
    fake.switch_scene(2)
    w.poll()
    kinds = [e.get("kind", e["type"]) for e in w.session.entries()
             if e["type"] in ("change", "scene")]
    assert {"param", "bypass", "block_added", "routing", "block_removed", "scene"} <= set(kinds)
    routing = next(e for e in w.session.entries() if e.get("kind") == "routing")
    assert routing["after"] == "USB_5"
    scene = next(e for e in w.session.entries() if e["type"] == "scene")
    assert (scene["before"], scene["after"]) == ("A", "C")
    assert fake.audio_interruptions == 1           # watching never recalled anything


def test_knob_turns_across_polls_become_one_entry(device, watching):
    w = watching
    amp, knob = _amp(device)
    for value in (0.6, 0.7, 0.8):
        device.apply_patch({"ops": [{"op": "set_param", "row": 0, "col": amp["col"],
                                     "param": knob, "value": value}]})
        w.poll()
    assert w.session.pending()                      # still moving: not written yet
    changes = _settle(w)
    assert len(changes) == 1
    assert changes[0]["polls"] == 3 and changes[0]["after"] == "8.0"
    assert changes[0]["before"] != "8.0"


def test_a_different_preset_on_the_unit_starts_a_segment(fake, watching):
    w = watching
    fake.read_preset(f"{USER}/My Presets", 0)      # the player loads another preset
    written = w.poll()
    assert written[-1]["type"] == "segment"
    meta = w.session.meta
    assert [s["preset"] for s in meta["segments"]] == ["Brit 2203", "Friedman BE50 X v"]
    assert w.session.snapshot(2, "baseline")["name"] == "Friedman BE50 X v"


def test_notes_and_markdown(device, watching):
    w = watching
    amp, knob = _amp(device)
    device.apply_patch({"ops": [{"op": "set_param", "row": 0, "col": amp["col"], "param": knob,
                                 "value": 0.95}]})
    w.poll()
    w.note("このクランチが良い")
    w.stop()
    watch.add_note("latest", "あとで追記", mock=True)
    md = watch.markdown(watch.open_session("latest", True))
    assert "このクランチが良い" in md and "あとで追記" in md
    assert f"{knob}: " in md and "開始時と最後の違い" in md


def test_capture_and_string_changes_are_marked_manual(device, fake, watching):
    w = watching
    cab = next(b for b in device.grid()["blocks"] if b["category"].startswith("Cabsim"))
    device.set_text(0, cab["col"], 0, "NG_412_Ribbon R121")
    w.poll()
    entries = _settle(w)
    assert entries and entries[-1]["manual"] and "manual_note" in entries[-1]
    r = watch.replay_for(w.session)
    assert any("Ribbon" in m for m in r["manual"])
    assert not any(op.get("param") == "ir selector" for op in r["patch"]["ops"])


def test_replay_reproduces_numeric_changes_on_a_fresh_recall(device, watching):
    w = watching
    amp, knob = _amp(device)
    col = _free_col(device)
    device.apply_patch({"ops": [
        {"op": "set_param", "row": 0, "col": amp["col"], "param": knob, "value": 0.33},
        {"op": "set_param", "row": 0, "col": amp["col"], "param": "BASS", "value": 0.71},
        {"op": "set_block", "row": 0, "col": col, "model": REVERB},
        {"op": "set_param", "row": 0, "col": col, "param": "MIX", "value": 0.42},
        {"op": "set_output", "row": 0, "port": "XLR_1"}]})
    device.set_lane_volume_db(0, -7.5)
    w.poll()
    w.poll()
    summary = w.stop()
    session = watch.open_session(summary["id"], True)
    final = session.snapshot(1, "final")
    device.read_stored(KAMI, 4)                     # back to the stored preset
    result = watch.apply_replay(device, session)
    assert result["applied"] > 0 and result["lane"] == 1
    now = presets.normalize(device.current_preset(), device.catalog)
    assert watch_diff.changes(final, now) == []


def test_to_sandbox_plans_both_ways(device, fake, watching):
    w = watching
    amp, knob = _amp(device)
    device.apply_patch({"ops": [{"op": "set_param", "row": 0, "col": amp["col"], "param": knob,
                                 "value": 0.25}]})
    w.poll()
    session = watch.open_session(w.stop()["id"], True)
    final = session.snapshot(1, "final")
    plan = watch.to_sandbox_plan(device, session)
    assert plan.steps[0].action == "rebuild" and "組み立て" in plan.render()
    result = watch.execute_plan(plan, device, confirm=False)
    assert result["dry_run"]
    key = device.ensure_sandbox()
    assert not fake.folders[key]
    source = watch.parse_source("kami:5E")
    assert source == {"folder": KAMI, "position": 36}
    plan = watch.to_sandbox_plan(device, session, source={"folder": KAMI, "position": 4},
                                 slot="2A", name="crunch idea")
    assert "元のプリセット" in plan.render()
    before = fake.audio_interruptions
    watch.execute_plan(plan, device, confirm=True)
    assert fake.audio_interruptions > before        # the source was recalled
    stored = fake.folders[key][8]
    assert stored.name == "crunch idea"
    saved = presets.normalize(device.read_stored(key, 8), device.catalog)
    assert [c for c in watch_diff.changes(final, saved) if c["kind"] != "name"] == []
    # Never into a protected folder.
    bad = watch.to_sandbox_plan(device, session, folder="kami", slot="30A")
    with pytest.raises(WriteRefused):
        watch.execute_plan(bad, device, confirm=True)


def test_take_files_the_dry_and_links_the_rig(rig, device, watching):
    link = watch.take(rig, "riff", 2.0, watcher=watching, note="サビ")
    meta = next(m for m in di_mod.listing(True) if m["name"] == "riff")
    assert meta["source"] == "take" and meta["seconds"] == pytest.approx(2.0)
    home = watch.takes_dir(True).parent
    info = sf.info(str(home / link["wet_file"]))
    assert info.channels == 2 and info.subtype == "PCM_24" and info.samplerate == 48000
    assert 0 < link["offset_ms"] < 10                # a few ms, not the 312 ms reamp trip
    assert link["preset"] == "Brit 2203" and link["scene"] == "A"
    assert any("Brit 2203" in block for block in link["knobs"])
    assert json.loads((home / link["snapshot_file"]).read_text())["name"] == "Brit 2203"
    logged = [e for e in watching.session.entries() if e["type"] == "take"]
    assert logged and logged[0]["di"] == "riff"
    second = watch.take(rig, "riff", 1.0)
    assert second["di"] == "riff_2" and second["session"] is None
    assert [t["di"] for t in watch.takes(True)] == ["riff", "riff_2"]


def test_take_refuses_when_the_row_listens_to_usb(rig, device):
    device.read_stored(KAMI, 4)
    route_usb(device)
    with pytest.raises(RuntimeError, match="INPUT_1"):
        watch.take(rig, "nope", 1.0)


def test_cli_flow(capsys):
    from qcsuite.cli import main
    main(["--mock", "--json", "watch", "start", "--interval", "0.01", "--duration", "0.05"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["running"] is False and summary["segments"]
    main(["--mock", "watch", "note", "latest", "メモ"])
    main(["--mock", "--json", "watch", "list"])
    rows = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert rows[0]["counts"]["note"] == 1
    main(["--mock", "watch", "show", "latest"])
    assert "メモ" in capsys.readouterr().out
    main(["--mock", "--json", "watch", "export", "latest", "--patch"])
    assert "patch" in json.loads(capsys.readouterr().out)
    main(["--mock", "--json", "watch", "take", "cli-take", "--seconds", "1", "--session", "latest"])
    assert json.loads(capsys.readouterr().out)["di"] == "cli-take"
    main(["--mock", "--json", "watch", "to-sandbox", "latest"])
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"]


def test_web_flow(client, fake):
    client.post("/api/connect")
    fake.read_preset(KAMI, 4)
    started = client.post("/api/watch/start", json={"interval": 0.02}).json()
    assert started["running"]
    assert client.post("/api/watch/start", json={}).status_code == 400
    grid = client.get("/api/grid").json()
    amp = next(b for b in grid["blocks"] if b["category"] == "Guitar Amplifier")
    knob = next(p["name"] for p in amp["params"] if p["name"] in ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1"))
    client.post("/api/apply", json={"patch": {"ops": [
        {"op": "set_param", "row": 0, "col": amp["col"], "param": knob, "value": 0.12}]}})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        s = client.get("/api/watch/status").json()
        if any(e["type"] == "change" for e in s.get("entries", [])):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the background watcher did not log the change")
    assert client.post("/api/watch/note", json={"text": "良い"}).json()["text"] == "良い"
    take = client.post("/api/watch/take", json={"name": "web-take", "seconds": 1}).json()
    assert take["session"] == started["id"]
    stopped = client.post("/api/watch/stop").json()
    assert stopped["counts"]["take"] == 1
    sessions = client.get("/api/watch/sessions").json()
    sid = sessions[0]["id"]
    assert "良い" in client.get(f"/api/watch/sessions/{sid}/markdown").text
    plan = client.post(f"/api/watch/sessions/{sid}/to-sandbox",
                       json={"source": "kami:1E"}).json()
    assert not plan["refused"] and plan["confirm_word"] == "実行"
    assert client.post(f"/api/watch/plans/{plan['id']}/execute",
                       json={"confirm": "はい"}).status_code == 400
    job = client.post(f"/api/watch/plans/{plan['id']}/execute", json={"confirm": "実行"}).json()["job"]
    detail = wait_job(client, job)
    assert detail["summary"]["status"] == "done", detail
    sandbox = client.app.state.qc.rig().fake.folders[f"{USER}/_qcsuite_sandbox"]
    assert [s.name for s in sandbox.values()] == ["Brit 2203 watch"]
    assert client.get("/api/watch/takes").json()[0]["di"] == "web-take"
    assert "編集ウォッチャー" in client.get("/static/watch.html").text
