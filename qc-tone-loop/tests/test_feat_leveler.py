"""Loudness leveler: measure, propose, apply to copies, verify, lane-VOLUME check."""

import json

import pytest

from helpers import wait_job
from qcsuite import jobs, presets
from qcsuite import plan as plan_mod
from qcsuite.audio import di as di_mod
from qcsuite.device import fake as fake_mod
from qcsuite.device.session import open_rig
from qcsuite.errors import PlanError, WriteRefused
from qcsuite.features import leveler

USER = "/media/p4/Presets"
# Presets without delay blocks: the fake's delay is slow to compute, and these
# three sit at clearly different loudness on the fake.
PRESETS = [{"folder": f"{USER}/My Presets", "position": 0},     # Friedman BE50 X v
           {"folder": f"{USER}/Cory Wong", "position": 11},     # Octave Funk (named scenes)
           {"folder": f"{USER}/Favorite", "position": 20}]      # Big Lead


@pytest.fixture
def short_di(di):
    di_mod.save("short", di[: int(48000 * 1.5)], 48000, True, source="test")
    return "short"


def _measure(rig, short_di, items=PRESETS, scenes=None):
    job = leveler.start_measure(rig.device, presets=items, di=short_di, scenes=scenes, mock=True)
    summary = jobs.run(job, leveler.measure_work(rig, job))
    assert summary["status"] == "done", summary
    return job.id, leveler.results(job.id, True)


def test_parse_inputs():
    assert leveler.parse_preset("kami:1E") == {"folder": f"{USER}/kami", "position": 4}
    assert leveler.parse_preset("My Presets:12") == {"folder": f"{USER}/My Presets", "position": 12}
    with pytest.raises(ValueError):
        leveler.parse_preset("kami")
    assert leveler.parse_scenes("A,c") == [0, 2]
    assert leveler.parse_scenes("labelled") == "labelled"
    with pytest.raises(ValueError):
        leveler.parse_scenes("Z")


def test_measure_records_loudness_and_latest(rig, short_di):
    job_id, rows = _measure(rig, short_di)
    levels = [r["lufs"] for r in rows]
    assert all(level is not None for level in levels)
    assert max(levels) - min(levels) > 1.0          # the fake's lane levels differ
    assert all(r["lanes"] and r["layout"] for r in rows)
    latest = leveler.latest_measurements(True)
    for p in PRESETS:
        assert latest[(p["folder"], p["position"])]["lufs"] is not None
    assert leveler.latest_job(True) == job_id


def test_per_scene_measurement(rig, short_di):
    _, rows = _measure(rig, short_di, [PRESETS[1]], scenes="A,B")
    scenes = rows[0]["scenes"]
    assert set(scenes) == {"A", "B"} and scenes["A"]["label"] == "Rhythm"
    assert all(s["lufs"] is not None for s in scenes.values())


def _m(name, lufs, lanes=None, excluded=False, scenes=None, tp=-6.0):
    return {"folder": f"{USER}/Live", "position": 0, "slot": "1A", "name": name, "lufs": lufs,
            "true_peak_dbtp": tp, "excluded": excluded, "reasons": ["素通し"] if excluded else [],
            "lanes": {"0": 0.0} if lanes is None else lanes, "scenes": scenes or {},
            "layout": {"0": {"lane_db": 0.0, "blocks": [
                {"col": 0, "model_id": 1001, "model": "Brit 2203", "category": "Guitar Amplifier",
                 "knobs": {"MASTER": 0.5}},
                {"col": 1, "model_id": 12001, "model": "Cab", "category": "Cabsim Guitar (M)"}]}}}


def test_proposal_math_median_and_clamping():
    rows = [_m("a", -20.0), _m("b", -14.0), _m("c", -16.0), _m("x", -9.0, excluded=True)]
    p = leveler.propose(rows)
    assert p["target"] == -16.0 and p["target_source"] == "中央値"
    by = {r["name"]: r for r in p["rows"]}
    assert by["a"]["delta_db"] == 4.0 and by["a"]["lanes"]["0"]["new_db"] == 4.0
    assert by["b"]["delta_db"] == -2.0
    assert by["x"]["delta_db"] is None and "除外" in by["x"]["notes"][0]
    loud = leveler.propose([_m("quiet", -40.0, lanes={"0": 6.0})], target=-18.0)["rows"][0]
    assert loud["lanes"]["0"]["new_db"] == 12.0 and not loud["correctable"]
    assert any("頭打ち" in n for n in loud["notes"])
    hot = leveler.propose([_m("hot", -20.0, tp=-1.0)], target=-12.0)["rows"][0]
    assert any("クリップ" in n for n in hot["notes"])


def test_scene_spread_is_advice_not_correction():
    scenes = {"A": {"label": "Rhythm", "lufs": -18.0}, "B": {"label": "Lead", "lufs": -14.0}}
    row = leveler.propose([_m("s", -18.0, scenes=scenes)], target=-18.0)["rows"][0]
    assert any("シーン間で 4.0 LU" in n for n in row["notes"])


def test_gain_block_and_amp_level_proposals():
    p = leveler.propose([_m("a", -20.0)], target=-16.0, method="gain_block")
    assert p["rows"][0]["gain_blocks"] == {"0": {"col": 2, "db": 4.0}}
    full = _m("full", -20.0)
    full["layout"]["0"]["blocks"].append({"col": 7, "model_id": 5004, "model": "Comp",
                                          "category": "Compressor"})
    row = leveler.propose([full], target=-16.0, method="gain_block")["rows"][0]
    assert not row["gain_blocks"] and not row["correctable"]
    amp = leveler.propose([_m("a", -20.0)], target=-16.0, method="amp_level")["rows"][0]
    assert amp["knobs"][0]["param"] == "MASTER"
    assert amp["knobs"][0]["new"] == pytest.approx(0.5 + 4.0 / leveler.AMP_KNOB_SPAN_DB, abs=1e-4)
    assert any("音色" in n for n in amp["notes"])
    with pytest.raises(ValueError):
        leveler.propose([_m("a", -20.0)], method="magic")


def test_apply_to_sandbox_copies_then_verify(rig, device, fake, short_di):
    job_id, rows = _measure(rig, short_di)
    before = {p["position"]: presets.normalize(device.read_stored(p["folder"], p["position"]),
                                               device.catalog)["fingerprint"] for p in PRESETS}
    proposal = leveler.propose(rows, target=-18.0)
    plan = leveler.build_plan(device, proposal, di=short_di, measure_job=job_id)
    sandbox = f"{USER}/_qcsuite_sandbox"
    copies = [s for s in plan.steps if s.action == "copy"]
    assert plan_mod.execute(plan, device)["dry_run"] and sandbox not in fake.folders
    leveler.execute(plan, device, confirm=True)
    assert len(fake.folders[sandbox]) == len(copies)
    session = leveler.load_session(plan.id, True)
    vjob = leveler.start_verify(session, mock=True)
    jobs.run(vjob, leveler.measure_work(rig, vjob))
    summary = leveler.verify_summary(vjob.id, True)
    assert summary["count"] == len(copies)
    assert summary["within_goal"] == summary["count"], summary
    after = {p["position"]: presets.normalize(device.read_stored(p["folder"], p["position"]),
                                              device.catalog)["fingerprint"] for p in PRESETS}
    assert before == after                       # the sources were never written


def test_protected_and_unallowed_targets_are_refused(rig, device, short_di):
    _, rows = _measure(rig, short_di, PRESETS[:1])
    proposal = leveler.propose(rows, target=-18.0)
    with pytest.raises(WriteRefused):
        leveler.build_plan(device, proposal, to="kami")
    with pytest.raises(WriteRefused):
        leveler.build_plan(device, proposal, to="Live")
    device.policy.allow("Live")
    plan = leveler.build_plan(device, proposal, to="Live")
    assert plan.steps[0].folder == f"{USER}/Live"


def test_unknown_adjustment_is_an_error(device):
    with pytest.raises(PlanError):
        leveler.apply_adjust(device, {"master_volume": 3})


def test_lane_volume_check_on_normal_and_inert_units(rig, short_di):
    result = leveler.check_lane_volume_effect(rig, short_di, f"{USER}/My Presets", 0)
    assert result["effective"] and result["measured_change_db"] == pytest.approx(-6.0, abs=0.5)
    assert rig.device.lane_volume_db(0) == pytest.approx(result["rows"]["0"], abs=0.05)

    inert = fake_mod.FakeQuadCortex(fake_mod.FakeConfig(
        catalog_timeouts=0, first_read_dropped=False, lane_volume_inert=True))
    with open_rig(mock=True, fake=inert) as r2:
        check = leveler.check_lane_volume_effect(r2, short_di, f"{USER}/My Presets", 0)
        assert check["no_effect"] and not check["effective"]
        assert check["recommended_method"] == "gain_block"
        written = check["readback_db"]["0"]
        assert written == pytest.approx(check["rows"]["0"] - 6.0, abs=0.05)   # it read back
        assert r2.device.lane_volume_db(0) == pytest.approx(check["rows"]["0"], abs=0.05)


def test_gain_block_levels_where_lanes_are_inert(short_di):
    inert = fake_mod.FakeQuadCortex(fake_mod.FakeConfig(
        catalog_timeouts=0, first_read_dropped=False, lane_volume_inert=True))
    with open_rig(mock=True, fake=inert) as rig:
        device = rig.device
        job_id, rows = _measure(rig, short_di)
        proposal = leveler.propose(rows, target=-18.0, method="gain_block")
        plan = leveler.build_plan(device, proposal, measure_job=job_id)
        copies = [s for s in plan.steps if s.action == "copy"]
        assert all(s.adjust.get("gain_block") for s in copies)
        # The shared plan runner understands gain blocks (Device.apply_adjust).
        plan_mod.execute(plan, device, confirm=True)
        copy = presets.normalize(device.read_stored(copies[0].folder, copies[0].position),
                                 device.catalog)
        assert any(b["model_id"] == leveler.GAIN_BLOCK_ID for b in copy["blocks"])
        vjob = leveler.start_verify(leveler.load_session(plan.id, True), mock=True)
        jobs.run(vjob, leveler.measure_work(rig, vjob))
        summary = leveler.verify_summary(vjob.id, True)
        assert summary["within_goal"] == summary["count"] == len(copies), summary


def test_cli_flow(capsys, short_di):
    from qcsuite.cli import main
    main(["--mock", "--json", "level", "measure", "--preset", "My Presets:1A",
          "--preset", "Cory Wong:2D", "--di", short_di])
    measured = json.loads(capsys.readouterr().out)
    assert len(measured["results"]) == 2
    main(["--mock", "level", "propose", "--target", "-20"])
    assert "目標: -20.0 LUFS" in capsys.readouterr().out
    main(["--mock", "--json", "level", "apply", "--target", "-20"])
    assert json.loads(capsys.readouterr().out)["dry_run"]
    main(["--mock", "--yes", "--json", "level", "apply", "--target", "-20", "--apply"])
    assert not json.loads(capsys.readouterr().out)["dry_run"]
    main(["--mock", "--json", "level", "verify"])
    verified = json.loads(capsys.readouterr().out)
    assert verified["within_goal"] == verified["count"] == 2
    main(["--mock", "--json", "level", "check-lane", "--preset", "My Presets:1A",
          "--di", short_di])
    assert json.loads(capsys.readouterr().out)["effective"]


def test_web_flow(client, short_di):
    client.post("/api/connect")
    job = client.post("/api/leveler/measure", json={"presets": PRESETS[:2], "di": short_di}).json()["job"]
    assert wait_job(client, job)["summary"]["status"] == "done"
    measured = client.get(f"/api/leveler/results/{job}").json()["results"]
    assert len(measured) == 2
    p = client.post("/api/leveler/propose", json={"job": job, "target": -18}).json()
    assert p["target"] == -18
    plan = client.post("/api/leveler/plan", json={"job": job, "target": -18,
                                                  "method": "gain_block"}).json()
    assert not plan["refused"] and plan["execute_url"].endswith(plan["id"])
    assert client.post(plan["execute_url"], json={"confirm": "はい"}).status_code == 400
    run = client.post(plan["execute_url"], json={"confirm": "実行"}).json()["job"]
    assert wait_job(client, run)["summary"]["status"] == "done"
    vjob = client.post("/api/leveler/verify", json={"session": plan["id"], "di": short_di}).json()["job"]
    wait_job(client, vjob)
    v = client.get(f"/api/leveler/verify/{vjob}").json()
    assert v["within_goal"] == v["count"] == 2
    check = client.post("/api/leveler/check-lane", json={"di": short_di}).json()["job"]
    detail = wait_job(client, check)
    assert detail["results"][0]["result"]["effective"]
    assert client.get("/static/leveler.html").status_code == 200
