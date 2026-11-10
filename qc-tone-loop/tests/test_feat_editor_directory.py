"""Editor DIRECTORY: preset rename / move / delete and setlist create / delete as plans,
only where the write policy allows; the capture library and MIDI Out as reads."""

import json
from pathlib import Path

import pyquadcortex as pq
import pytest

from helpers import wait_job
from qcsuite import plan as plan_mod
from qcsuite.device import guard
from qcsuite.features import editor_directory as d

MY = "/media/p4/Presets/My Presets"
LIVE = "/media/p4/Presets/Live"
SANDBOX = f"{guard.USER_ROOT}/{guard.SANDBOX_NAME}"


def names(fake, key):
    return {i: s.name for i, s in fake.folders[key].items()}


@pytest.fixture
def sandbox(fake):
    """A sandbox holding three presets copied from My Presets (positions 0, 1, 3)."""
    fake.create_setlist(guard.SANDBOX_NAME)
    for pos, src in ((0, 0), (1, 1), (3, 2)):
        fake.folders[SANDBOX][pos] = d_copy(fake, MY, src)
    return SANDBOX


def d_copy(fake, key, position):
    import copy
    return copy.deepcopy(fake.folders[key][position])


def run(device, plan):
    return plan_mod.execute(plan, device, confirm=True)


# -- what may be written ---------------------------------------------------------------------

def test_listing_marks_read_only_folders(device, sandbox):
    data = d.directory(device, MY)
    assert data["folder"] == MY and not data["writable"]
    assert d.READ_ONLY_NOTE in data["reason"] and data["free_slots"] == []
    rows = {f["name"]: f for f in data["setlists"]}
    assert not rows["My Presets"]["writable"] and not rows["Factory Library"]["writable"]
    assert not rows["Live"]["writable"]                       # not allowed on this server
    assert rows[guard.SANDBOX_NAME]["writable"] and rows[guard.SANDBOX_NAME]["deletable"]
    assert not rows["Live"]["deletable"]
    s = d.directory(device, sandbox)
    assert s["writable"] and "1C" in s["free_slots"] and "1A" not in s["free_slots"]


def test_plans_refused_outside_the_allowed_folders(device, fake, sandbox):
    before = names(fake, MY)
    for action in ("rename", "move", "delete"):
        with pytest.raises(d.DirectoryError, match=d.READ_ONLY_NOTE):
            d.build_plan(device, {"action": action, "folder": MY, "position": 0,
                                  "name": "x", "to": "30A"})
    with pytest.raises(d.DirectoryError):
        d.build_plan(device, {"action": "delete", "folder": "Factory Library", "position": 0})
    with pytest.raises(d.DirectoryError):
        d.build_plan(device, {"action": "delete", "folder": LIVE, "position": 0})
    with pytest.raises(d.DirectoryError):
        d.build_plan(device, {"action": "delete_setlist", "folder": "Live"})
    with pytest.raises(d.DirectoryError):
        d.build_plan(device, {"action": "delete_setlist", "folder": "kami"})
    with pytest.raises(d.DirectoryError):
        d.build_plan(device, {"action": "unknown"})
    assert names(fake, MY) == before
    assert not any(c[0] in ("delete_preset", "move_preset", "save_current_preset")
                   for c in fake.calls)


def test_allowed_opt_in_folder_is_writable(device, fake):
    device.policy.allow("Live")
    fake.folders[LIVE][0] = d_copy(fake, MY, 0)
    assert d.directory(device, LIVE)["writable"]
    plan = d.build_plan(device, {"action": "move", "folder": LIVE, "position": 0, "to": "2A"})
    run(device, plan)
    assert names(fake, LIVE) == {8: fake.folders[LIVE][8].name}


# -- presets ---------------------------------------------------------------------------------

def test_delete_backs_up_then_deletes(device, fake, sandbox):
    gone = fake.folders[sandbox][1].name
    plan = d.build_plan(device, {"action": "delete", "folder": sandbox, "position": 1})
    assert plan.destructive and [s.action for s in plan.steps] == ["delete"]
    assert 1 in fake.folders[sandbox]                          # building sends nothing
    result = run(device, plan)
    assert 1 not in fake.folders[sandbox]
    backup = [p for p in result["backups"] if gone.split()[0] in p and p.endswith(".json")
              and "listing_" not in p]
    assert backup and json.loads(Path(backup[0]).read_text())["name"] == gone


def test_move_only_to_an_empty_slot(device, fake, sandbox):
    with pytest.raises(d.DirectoryError, match="上書き"):
        d.build_plan(device, {"action": "move", "folder": sandbox, "position": 0, "to": "1B"})
    with pytest.raises(d.DirectoryError, match="スロット"):
        d.build_plan(device, {"action": "move", "folder": sandbox, "position": 0, "to": "99Z"})
    name = fake.folders[sandbox][0].name
    run(device, d.build_plan(device, {"action": "move", "folder": sandbox, "position": 0,
                                      "to": "4H"}))
    assert fake.folders[sandbox][pq.slot_to_position("4H")].name == name
    assert 0 not in fake.folders[sandbox]


def test_rename_copies_deletes_and_moves_back(device, fake, sandbox):
    old = fake.folders[sandbox][3].name
    plan = d.build_plan(device, {"action": "rename", "folder": sandbox, "position": 3,
                                 "name": "Renamed Tone"})
    assert [s.action for s in plan.steps] == ["copy", "delete", "move"]
    assert plan.steps[0].position == 2 and plan.steps[2].position == 3   # spare slot, then back
    assert any("名前を変える命令が無い" in w for w in plan.warnings)
    result = run(device, plan)
    after = names(fake, sandbox)
    assert after[3] == "Renamed Tone" and old not in after.values() and 2 not in after
    assert any(old.split()[0] in b for b in result["backups"])


def test_rename_refuses_ambiguous_names(device, fake, sandbox):
    other = fake.folders[sandbox][0].name
    with pytest.raises(d.DirectoryError, match="すでにあります"):
        d.build_plan(device, {"action": "rename", "folder": sandbox, "position": 1,
                              "name": other.upper()})
    with pytest.raises(d.DirectoryError, match="同じ名前"):
        d.build_plan(device, {"action": "rename", "folder": sandbox, "position": 1,
                              "name": fake.folders[sandbox][1].name})
    with pytest.raises(d.DirectoryError, match="20 文字"):
        d.build_plan(device, {"action": "rename", "folder": sandbox, "position": 1,
                              "name": "x" * 21})
    with pytest.raises(Exception, match="使えない文字"):
        d.build_plan(device, {"action": "rename", "folder": sandbox, "position": 1,
                              "name": "../kami/x"})
    fake.folders[sandbox][5] = d_copy(fake, sandbox, 0)       # the same name twice
    with pytest.raises(d.DirectoryError, match="2 つ"):
        d.build_plan(device, {"action": "delete", "folder": sandbox, "position": 0})
    with pytest.raises(d.DirectoryError, match="空き"):
        d.build_plan(device, {"action": "delete", "folder": sandbox, "position": 200})


# -- setlists --------------------------------------------------------------------------------

def test_create_and_delete_sandbox_setlists(device, fake):
    key = f"{guard.USER_ROOT}/_qcsuite_gig"
    plan = d.build_plan(device, {"action": "create_setlist", "name": "_qcsuite_gig"})
    assert key not in fake.folders
    run(device, plan)
    assert key in fake.folders
    fake.folders[key][0] = d_copy(fake, MY, 0)
    plan = d.build_plan(device, {"action": "delete_setlist", "folder": key})
    assert plan.destructive and any("1 件" in w for w in plan.warnings)
    result = run(device, plan)
    assert key not in fake.folders and any(b.endswith(".json") and "listing_" not in b
                                           for b in result["backups"])


def test_create_setlist_names(device, fake):
    for bad in ("My Presets", "kami", "Gig", "Live", "a/b", ""):
        with pytest.raises(d.DirectoryError):
            d.build_plan(device, {"action": "create_setlist", "name": bad})
    assert "Live" not in d.directory(device)["create"]["allowed"]   # exists already


def test_create_setlist_respects_the_limit(device, fake, monkeypatch):
    monkeypatch.setenv("QCSUITE_SETLIST_LIMIT", "8")            # the fake has 8 user folders
    with pytest.raises(d.DirectoryError, match="上限"):
        d.build_plan(device, {"action": "create_setlist", "name": "_qcsuite_more"})


def test_readonly_env_refuses_every_plan(device, sandbox, monkeypatch):
    device.policy.readonly = True
    with pytest.raises(d.DirectoryError):
        d.build_plan(device, {"action": "create_setlist", "name": "_qcsuite_x"})
    assert not d.directory(device, sandbox)["writable"]


# -- reads -----------------------------------------------------------------------------------

def test_captures_listing_is_read_only(device, fake):
    data = d.captures_listing(device)
    assert data["captures"] and {"index", "name", "key"} <= set(data["captures"][0])
    assert "鳴りません" in data["note"]


def test_midi_listing(device, fake):
    assert d.midi_listing(device)["rows"] == []
    slots = fake.live.midi_messages_general_v2
    while len(slots) < 120:
        slots.add()
    slots[0].type, slots[0].channel, slots[0].param1, slots[0].param2 = 1, 3, 10, 64
    slots[96].type, slots[96].channel, slots[96].param1 = 1, 2, 11
    slots[96].param2, slots[96].param3 = 0, 127
    slots[13].type, slots[13].channel, slots[13].param3 = 3, 1, 5
    rows = d.midi_listing(device)["rows"]
    assert rows[0] == {"source": "フットスイッチ A", "type": "CC", "channel": 3, "detail": "CC 10 = 64"}
    assert rows[1]["source"] == "フットスイッチ B" and rows[1]["type"] == "PC"
    assert rows[2]["source"] == "エクスプレッション 1" and rows[2]["detail"] == "CC 11: 0〜127"
    assert not fake.dirty                                      # a read only


# -- web ---------------------------------------------------------------------------------------

def test_web_plan_then_execute_with_the_word(client, fake):
    fake.create_setlist(guard.SANDBOX_NAME)
    fake.folders[SANDBOX][0] = d_copy(fake, MY, 0)
    data = client.get(f"/api/editor/directory?folder={SANDBOX}").json()
    assert data["writable"] and data["confirm_word"] == "実行"
    r = client.post("/api/editor/directory/plan",
                    json={"action": "delete", "folder": SANDBOX, "position": 0})
    plan = r.json()
    assert r.status_code == 200 and plan["confirm_word"] == "実行" and plan["refused"] == []
    assert plan["backup_dir"].endswith(f"{plan['id']}/backup")
    assert 0 in fake.folders[SANDBOX]
    bad = client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": "はい"})
    assert bad.status_code == 400 and 0 in fake.folders[SANDBOX]
    job = client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": "実行"}).json()["job"]
    detail = wait_job(client, job)
    assert detail["summary"]["status"] == "done", detail
    assert 0 not in fake.folders[SANDBOX]


def test_web_create_setlist_plan_asks_for_the_word(client, fake):
    plan = client.post("/api/editor/directory/plan",
                       json={"action": "create_setlist", "name": "_qcsuite_web"}).json()
    assert plan["confirm_word"] == "実行" and plan["backup_dir"] is None


def test_web_refuses_protected(client):
    r = client.post("/api/editor/directory/plan",
                    json={"action": "delete", "folder": MY, "position": 0})
    assert r.status_code == 400 and d.READ_ONLY_NOTE in r.json()["error"]


def test_web_reads(client):
    assert client.get("/api/editor/directory/captures").json()["captures"]
    assert client.get("/api/editor/directory/midi").json()["rows"] == []


def test_finished_plan_stops_the_undo_history(device, fake, sandbox):
    from qcsuite.features import editor, panel
    s = editor.EditorSession()
    b = device.grid()["blocks"][-1]
    s.layout(device, "delete", {"row": b["row"], "col": b["col"], "model_id": b["model_id"]})
    plan = d.build_plan(device, {"action": "delete", "folder": sandbox, "position": 1})
    run(device, plan)
    assert d.plan_finished(device, plan.id) == {"barrier": True}
    with pytest.raises(panel.PanelError):
        panel.undo(device)
