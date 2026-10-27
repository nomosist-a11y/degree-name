"""Safety rules, retries, the capture quirk, plans and the device lock."""

import subprocess
import sys
import textwrap

import pyquadcortex as pq
import pytest

from qcsuite import plan as plan_mod
from qcsuite import presets
from qcsuite.device import fake as fake_mod
from qcsuite.device import guard
from qcsuite.device.core import Device
from qcsuite.device.lock import device_lock
from qcsuite.errors import DeviceBusy, PlanError, WriteRefused

USER = "/media/p4/Presets"
SANDBOX = f"{USER}/_qcsuite_sandbox"


# -- the policy ----------------------------------------------------------------

@pytest.mark.parametrize("name", ["My Presets", "Favorite", "kami", "Cory Wong"])
def test_protected_folders_refuse_writes_and_cannot_be_allowed(name):
    policy = guard.WritePolicy()
    with pytest.raises(WriteRefused, match="保護"):
        policy.check(f"{USER}/{name}", "保存")
    with pytest.raises(WriteRefused, match="保護"):
        policy.allow(name)


def test_factory_is_always_read_only():
    policy = guard.WritePolicy()
    with pytest.raises(WriteRefused):
        policy.check("/opt/neuraldsp/Factory Library/", "保存")
    with pytest.raises(WriteRefused):
        policy.allow("/opt/neuraldsp/Factory Library")


@pytest.mark.parametrize("name", ["Live", "Artist", "Song", "Workshop"])
def test_opt_in_folders_need_an_explicit_allow(name):
    policy = guard.WritePolicy()
    with pytest.raises(WriteRefused, match="明示的"):
        policy.check(f"{USER}/{name}", "保存")
    policy.allow(name)
    policy.check(f"{USER}/{name}", "保存")


def test_sandbox_is_always_allowed_and_unknown_folders_are_not():
    policy = guard.WritePolicy()
    policy.check(SANDBOX, "保存")
    policy.check(f"{USER}/_qcsuite_tournament", "保存")
    with pytest.raises(WriteRefused, match="許可リスト"):
        policy.check(f"{USER}/Someone Else", "保存")


def test_readonly_env_refuses_everything(monkeypatch):
    monkeypatch.setenv("QCSUITE_READONLY", "1")
    with pytest.raises(WriteRefused, match="READONLY"):
        guard.WritePolicy().check(SANDBOX, "保存")


def test_only_sandboxes_can_be_deleted():
    policy = guard.WritePolicy(allowed={"Live"})
    policy.check_delete_folder("_qcsuite_sandbox")
    with pytest.raises(WriteRefused):
        policy.check_delete_folder("Live")


def test_folder_key_round_trip():
    assert guard.folder_key("kami") == f"{USER}/kami"
    assert guard.folder_key(f"{USER}/kami/") == f"{USER}/kami"
    assert guard.folder_key("local_nc_root") == "local_nc_root"
    assert guard.folder_key("106_f") == "106_f"
    assert guard.folder_key(str(pq.Setlist.FACTORY)) == "/opt/neuraldsp/Factory Library"


# -- device wrapper -----------------------------------------------------------------

def test_device_rides_out_the_known_timeouts():
    f = fake_mod.FakeQuadCortex(fake_mod.FakeConfig(catalog_timeouts=2))
    d = Device(f, mock=True, retry_delay=0)
    assert len(d.catalog) > 300
    assert d.grid()["rows"]
    assert sum(n.kind == "retry" for n in d.notices) == 3


def test_device_exposes_no_whole_unit_settings(device):
    for name in ("set_global_eq", "set_input_port", "update_settings", "set_usb_port"):
        assert not hasattr(device, name)


def test_capture_placement_is_believed_from_the_grid(device):
    result = device.apply_patch({"ops": [{"op": "set_block", "row": 2, "col": 0,
                                          "model": 14000}]})
    assert any("キャプチャー" in w for w in result["warnings"])
    assert any(b["model_id"] == 14000 for b in device.grid()["blocks"])


def test_set_capture_says_it_will_not_sound(device):
    cap = device.captures()[0]
    result = device.set_capture(3, 0, cap)
    assert result["audible"] is False
    assert any(n.kind == "inert_write" for n in device.notices)


def test_a_really_refused_block_still_raises(device, fake):
    fake_mod.data.DSP_BUDGET, saved = 0, fake_mod.data.DSP_BUDGET
    try:
        with pytest.raises(pq.BlockRefused):
            device.place_block(3, 5, fake_mod.data.factory_models.Reverb.HALL)
    finally:
        fake_mod.data.DSP_BUDGET = saved


def test_writes_to_protected_folders_are_refused_before_sending(device, fake):
    calls = len(fake.calls)
    with pytest.raises(WriteRefused):
        device.save_current(f"{USER}/My Presets", 200, "nope")
    with pytest.raises(WriteRefused):
        device.copy_preset(f"{USER}/kami", 0, f"{USER}/Favorite")
    assert not any(c[0] in ("save_current_preset", "read_preset") for c in fake.calls[calls:])


def test_destructive_device_calls_require_a_plan(device):
    key = device.ensure_sandbox()
    device.save_current(key, 0, "first")
    with pytest.raises(PlanError):
        device.save_current(key, 0, "overwrite")
    with pytest.raises(PlanError):
        device.delete_preset(key, "first")
    with pytest.raises(PlanError):
        device.move_preset(key, "first", 3)
    with pytest.raises(PlanError):
        device.delete_folder("_qcsuite_sandbox")


def test_sandbox_is_created_once(device, fake):
    key = device.ensure_sandbox()
    assert key == SANDBOX
    assert device.ensure_sandbox() == key
    assert sum(1 for c in fake.calls if c[0] == "create_setlist") == 1


def test_save_reports_the_name_the_unit_chose(device):
    key = device.ensure_sandbox()
    device.save_current(key, 0, "Laney test")
    assert device.save_current(key, 1, "Laney test") == "Laney test_1"
    assert any("Laney test_1" in n.message for n in device.notices)


# -- plans ------------------------------------------------------------------------

def _copy_plan(device):
    key = device.ensure_sandbox()
    return key, plan_mod.Plan("copy two", steps=[
        plan_mod.Step("copy", key, position=0, name="Henriksen copy",
                      source={"folder": f"{USER}/kami", "position": 0}),
        plan_mod.Step("copy", key, position=1, name=None,
                      source={"folder": f"{USER}/My Presets", "position": 1}),
    ])


def test_plans_are_dry_runs_by_default(device, fake):
    key, plan = _copy_plan(device)
    result = plan_mod.execute(plan, device)
    assert result["dry_run"] and not fake.folders[key]


def test_plan_runs_and_verifies(device, fake):
    key, plan = _copy_plan(device)
    result = plan_mod.execute(plan, device, confirm=True)
    assert [r["stored_name"] for r in result["results"]] == ["Henriksen copy", "Two Rock Mayer"]
    assert {s.name for s in fake.folders[key].values()} == {"Henriksen copy", "Two Rock Mayer"}


def test_a_refused_step_stops_the_whole_plan(device, fake):
    key, plan = _copy_plan(device)
    plan.steps.append(plan_mod.Step("delete", f"{USER}/kami", name="Henriksen"))
    with pytest.raises(WriteRefused, match="何も実行していません"):
        plan_mod.execute(plan, device, confirm=True)
    assert not fake.folders[key]
    assert len(fake.folders[f"{USER}/kami"]) == 20


def test_destructive_plan_backs_up_first(device, fake, tmp_path):
    key = device.ensure_sandbox()
    device.save_current(key, 0, "victim")
    plan = plan_mod.Plan("delete", steps=[plan_mod.Step("delete", key, name="victim")])
    result = plan_mod.execute(plan, device, confirm=True)
    assert not fake.folders[key]
    backed = [p for p in result["backups"] if "victim" in p]
    assert backed
    import json
    n = json.loads(open(backed[0]).read())
    assert n["schema"] == presets.SCHEMA and n["name"] == "victim"


def test_overwrite_plan(device, fake):
    key = device.ensure_sandbox()
    device.save_current(key, 0, "old")
    plan = plan_mod.Plan("overwrite", steps=[plan_mod.Step("save", key, position=0,
                                                           name="new", overwrites="old")])
    assert plan.destructive
    plan_mod.execute(plan, device, confirm=True)
    assert fake.folders[key][0].name == "new"


def test_rebuild_plan_restores_numeric_state(device, fake):
    catalog = device.catalog
    source = presets.normalize(device.read_stored(f"{USER}/kami", 4), catalog)
    key = device.ensure_sandbox()
    plan = plan_mod.Plan("restore", steps=[plan_mod.Step("rebuild", key, position=3,
                                                         name="Brit 2203 restored",
                                                         payload=source)])
    plan_mod.execute(plan, device, confirm=True)
    restored = presets.normalize(device.read_stored(key, 3), catalog)
    changes = presets.diff(source, restored)
    assert [c["where"] for c in changes] == ["名前"]


# -- the lock ------------------------------------------------------------------------

def test_second_process_gets_a_clear_busy_error(tmp_path):
    lock = tmp_path / "device.lock"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import time, sys
        from qcsuite.device.lock import device_lock
        from pathlib import Path
        with device_lock(Path({str(lock)!r}), what="other tool"):
            print("held", flush=True)
            time.sleep(5)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(DeviceBusy, match="other tool"):
            with device_lock(lock, timeout=0.3):
                pass
    finally:
        holder.kill()
        holder.wait()
    with device_lock(lock, timeout=2):
        with device_lock(lock, timeout=0.1):     # re-entrant in one process
            pass
