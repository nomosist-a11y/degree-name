"""Regression tests for the safety audit: web request checks, overwrites, read-back,
folder deletion backups, dry runs that send nothing, folder-name tricks, read-only."""

import pytest

from qcsuite import plan as plan_mod
from qcsuite.device import guard
from qcsuite.errors import PlanError, WriteRefused

USER = "/media/p4/Presets"
SANDBOX = f"{USER}/_qcsuite_sandbox"


# -- web requests ---------------------------------------------------------------------

def test_cross_site_and_foreign_host_requests_are_refused(client):
    assert client.get("/api/status").status_code == 200
    r = client.post("/api/policy/allow", json={"folder": "Live"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.get("/api/status", headers={"Host": "attacker.example:8765"})
    assert r.status_code == 403


def test_api_needs_the_session_cookie_and_json(client):
    fresh = dict(client.cookies)
    client.cookies.clear()
    assert client.get("/api/status").status_code == 403
    for name, value in fresh.items():
        client.cookies.set(name, value)
    r = client.post("/api/policy/allow", content='{"folder":"Live"}',
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_grid_changes_wait_for_a_running_job(client, monkeypatch):
    client.post("/api/connect")
    state = client.app.state.qc
    monkeypatch.setattr(state.jobs, "busy", lambda: "job-x")
    r = client.post("/api/apply", json={"ops": [{"op": "set_input", "row": 0, "port": "USB_5"}]})
    assert r.status_code == 409 and "job-x" in r.json()["error"]


# -- overwrites and read-back -----------------------------------------------------------

def test_a_slot_filled_after_planning_is_not_overwritten(device, fake):
    device.policy.allow("Live")
    key = f"{USER}/Live"
    plan = plan_mod.Plan("copy", steps=[plan_mod.Step(
        "copy", key, position=0, name="mine", source={"folder": f"{USER}/kami", "position": 0})])
    fake.save_current_preset(key, 0, "arrived later")          # someone fills the slot
    fake.list_presets(key)                                      # (listing lag)
    with pytest.raises(PlanError, match="計画を作ったときは"):
        plan_mod.execute(plan, device, confirm=True)
    assert fake.folders[key][0].name == "arrived later"


def test_a_dropped_save_is_not_reported_as_done(device, fake, monkeypatch):
    key = device.ensure_sandbox()
    device.save_current(key, 0, "old")
    monkeypatch.setattr(fake, "save_current_preset",
                        lambda *a, **k: "old")                   # the unit ignores the write
    plan = plan_mod.Plan("over", steps=[plan_mod.Step("save", key, position=0, name="NEW",
                                                      overwrites="old")])
    with pytest.raises(PlanError, match="読み戻すと「old」"):
        plan_mod.execute(plan, device, confirm=True)


def test_name_matching_follows_the_units_dedup_rule():
    assert guard.name_matches("Laney test", "Laney test_1")
    assert guard.name_matches("Cali Basswalk [Ret1]", "Cali Basswalk [Ret_1")
    assert not guard.name_matches("NEW CONTENT", "Henriksen")
    assert not guard.name_matches("a", "b_1")


def test_deleting_a_folder_backs_up_every_preset_in_it(device, fake):
    key = device.ensure_sandbox()
    for i, name in enumerate(["one", "two", "three"]):
        device.save_current(key, i, name)
    result = plan_mod.execute(plan_mod.Plan("del", steps=[plan_mod.Step("delete_folder", key)]),
                              device, confirm=True)
    saved = [p for p in result["backups"] if p.endswith(".json") and "listing_" not in p]
    assert len(saved) == 3
    assert key not in fake.folders


# -- dry runs send nothing ------------------------------------------------------------------

def test_building_plans_never_creates_the_sandbox(device, fake, rig):
    from qcsuite.features import panel
    plan = panel.save_plan(device, "x")
    assert plan.steps[0].action == "create_folder"
    plan_mod.execute(plan, device)                                # dry run
    assert not any(c[0] == "create_setlist" for c in fake.calls)
    assert SANDBOX not in fake.folders


# -- folder-name tricks -----------------------------------------------------------------------

@pytest.mark.parametrize("target", [
    f"{USER}/kami/.", f"{USER}/My Presets/./", f"{USER}/Favorite/sub/../.", "Kami", "kami ",
    f"{USER}/kami/_qcsuite_x", f"{USER}/../Presets/kami",
])
def test_odd_spellings_of_protected_folders_are_refused(target):
    policy = guard.WritePolicy()
    with pytest.raises(WriteRefused):
        policy.check(target, "保存")
    with pytest.raises(WriteRefused):
        policy.allow(target)


@pytest.mark.parametrize("name", ["a/b", "../x", "x\\y", "tab\there", "", "   "])
def test_odd_preset_names_are_refused(device, name):
    key = device.ensure_sandbox()
    with pytest.raises(WriteRefused):
        device.save_current(key, 0, name)


def test_readonly_also_stops_grid_edits(device, monkeypatch):
    monkeypatch.setenv("QCSUITE_READONLY", "1")
    device.policy = guard.WritePolicy()
    with pytest.raises(WriteRefused, match="READONLY"):
        device.apply_patch({"ops": [{"op": "set_input", "row": 0, "port": "USB_5"}]})
    with pytest.raises(WriteRefused, match="READONLY"):
        device.set_lane_volume_db(0, -3.0)
