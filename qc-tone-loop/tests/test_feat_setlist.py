"""Setlists: song -> preset resolution, placement plans, MIDI tables."""

import csv
import io
import json

import pytest

from helpers import wait_job

from qcsuite import plan as plan_mod
from qcsuite.errors import PlanError, WriteRefused
from qcsuite.features import setlist as sl

USER = "/media/p4/Presets"
SONGS = """# test gig
Funk Opener | Cory Wong Funk Tour
Ballad | kami:1E
Henriksen
Last Song | Solo Funk
"""


def test_parse_text_and_json():
    songs = sl.parse_songs(SONGS)
    assert [s["song"] for s in songs] == ["Funk Opener", "Ballad", "Henriksen", "Last Song"]
    assert songs[1]["query"] == "kami:1E" and songs[2]["query"] == "Henriksen"
    js = sl.parse_songs(json.dumps({"songs": ["A", {"song": "B", "preset": "kami:1A"}]}))
    assert js == [{"song": "A", "query": "A"}, {"song": "B", "query": "kami:1A"}]
    with pytest.raises(ValueError):
        sl.parse_songs('[{"preset": "x"}]')


def test_ambiguous_names_are_reported_not_guessed(device):
    s = sl.build("gig", SONGS, device, mock=True)
    by_song = {x["song"]: x for x in s["songs"]}
    assert by_song["Ballad"]["source"]["name"] == "Brit 2203"
    assert by_song["Henriksen"]["status"] == "ambiguous"
    folders = {c["folder_name"] for c in by_song["Henriksen"]["candidates"]}
    assert folders == {"My Presets", "kami"}
    assert by_song["Funk Opener"]["status"] == "ambiguous"       # My Presets and Cory Wong
    assert by_song["Last Song"]["status"] == "resolved"
    assert len(sl.problems(s)) == 2
    s = sl.build("gig", SONGS + "Nothing Like This\n", device, mock=True, prefer_folder="kami")
    assert [x["status"] for x in s["songs"]] == ["ambiguous", "resolved", "resolved", "resolved",
                                                 "missing"]
    assert s["songs"][2]["source"]["folder_name"] == "kami"


def test_choose_settles_an_ambiguous_song(device):
    s = sl.build("gig", SONGS, device, mock=True)
    sl.choose(s, 1, f"{USER}/Cory Wong", 0, device)
    sl.choose(s, 3, f"{USER}/kami", 0, device)
    assert not sl.problems(s)
    with pytest.raises(PlanError):
        sl.choose(s, 1, f"{USER}/Live", 0, device)            # empty slot


def _resolved(device):
    s = sl.build("gig", SONGS, device, mock=True, prefer_folder="kami")
    sl.choose(s, 1, f"{USER}/Cory Wong", 0, device)
    return s


def test_placement_into_the_sandbox(device, fake):
    s = _resolved(device)
    plan = sl.plan_placement(device, s, start="2A")
    # The sandbox does not exist yet: the plan creates it, a dry run does not.
    assert plan.steps[0].action == "create_folder"
    assert [st.position for st in plan.steps[1:]] == [8, 9, 10, 11]
    assert not plan.destructive and plan.changes_sound
    key = s["placement"]["folder"]
    assert plan_mod.execute(plan, device)["dry_run"] and key not in fake.folders
    plan_mod.execute(plan, device, confirm=True)
    sl.refresh_targets(device, s)
    assert [x["target"]["slot"] for x in s["songs"]] == ["2A", "2B", "2C", "2D"]
    assert [x["target"]["name"] for x in s["songs"]] == ["Cory Wong Funk Tour", "Brit 2203",
                                                         "Henriksen", "Solo Funk"]
    rows = sl.midi_rows(s, sl.midi_config(True))
    assert rows[0]["cc32"] == sl.UNKNOWN_CC32 and rows[0]["pc"] == 8 and rows[0]["pc_display"] == 9


def test_occupied_slots_stop_the_plan_unless_overwrite(device):
    s = _resolved(device)
    key = device.ensure_sandbox()
    device.save_current(key, 1, "already here")
    with pytest.raises(PlanError, match="埋まっています"):
        sl.plan_placement(device, s, start="1A")
    plan = sl.plan_placement(device, s, start="1A", overwrite=True)
    assert plan.destructive and plan.steps[1].overwrites == "already here"


def test_unresolved_songs_stop_the_plan(device):
    s = sl.build("gig", SONGS, device, mock=True)
    with pytest.raises(PlanError, match="複数あります"):
        sl.plan_placement(device, s)


def test_protected_and_opt_in_targets(device):
    s = _resolved(device)
    with pytest.raises(WriteRefused, match="保護"):
        sl.plan_placement(device, s, folder="kami")
    with pytest.raises(WriteRefused, match="明示的"):
        sl.plan_placement(device, s, folder="Live")
    device.policy.allow("Live")
    plan = sl.plan_placement(device, s, folder="Live")
    assert plan.steps[0].folder == f"{USER}/Live"


@pytest.mark.parametrize("position, cc0, pc", [(0, 0, 0), (127, 0, 127), (128, 1, 0), (255, 1, 127)])
def test_midi_math(position, cc0, pc):
    n = sl.midi_numbers(position)
    assert (n["cc0"], n["pc"], n["pc_display"]) == (cc0, pc, pc + 1)


def test_midi_numbers_refuse_out_of_range():
    with pytest.raises(ValueError):
        sl.midi_numbers(256)


def test_map_mode_and_custom_setlist_numbers(device):
    s = _resolved(device)
    sl.use_existing(s)
    rows = sl.midi_rows(s, sl.midi_config(True), channel=3)
    by = {r["song"]: r for r in rows}
    assert by["Ballad"]["cc32"] == sl.UNKNOWN_CC32 and "実機" in by["Ballad"]["note"]
    assert (by["Ballad"]["cc0"], by["Ballad"]["pc"]) == (0, 4)
    assert by["Ballad"]["channel"] == 3
    sl.set_midi_config({"kami": 4, "Cory Wong": 5}, True)
    rows = {r["song"]: r for r in sl.midi_rows(s, sl.midi_config(True))}
    assert rows["Ballad"]["cc32"] == 4 and rows["Funk Opener"]["cc32"] == 5
    assert sl.midi_config(True)["My Presets"] == 1 and sl.midi_config(True)["Factory Library"] == 0
    with pytest.raises(ValueError):
        sl.set_midi_config({"kami": 13}, True)


def test_midi_exports(device):
    s = sl.use_existing(_resolved(device))
    mapping = sl.midi_config(True)
    table = list(csv.reader(io.StringIO(sl.midi_export(s, mapping, "csv"))))
    assert table[0][:3] == ["順番", "曲名", "プリセット"] and len(table) == 5
    md = sl.midi_export(s, mapping, "md")
    assert "| 順番 |" in md and "実機で確認" in md
    js = json.loads(sl.midi_export(s, mapping, "json"))
    assert len(js["rows"]) == 4 and js["notes"]
    with pytest.raises(ValueError):
        sl.midi_export(s, mapping, "xml")


def test_cli_plan_apply_and_midi(tmp_path, capsys):
    from qcsuite.cli import main
    songs = tmp_path / "songs.txt"
    songs.write_text(SONGS)
    with pytest.raises(SystemExit, match="未解決"):
        main(["--mock", "setlist", "plan", str(songs), "--name", "gig"])
    capsys.readouterr()
    with pytest.raises(SystemExit, match="未解決"):        # song 1 is in two folders
        main(["--mock", "--yes", "setlist", "plan", str(songs), "--name", "gig2",
              "--prefer-folder", "kami"])
    main(["--mock", "setlist", "choose", "gig", "1", "Cory Wong:1A"])
    main(["--mock", "setlist", "choose", "gig", "3", "kami:1A"])
    capsys.readouterr()
    # Planning again keeps the choices, dry run first, then --apply.
    main(["--mock", "--yes", "setlist", "plan", str(songs), "--name", "gig"])
    assert "_qcsuite_sandbox" not in capsys.readouterr().out
    main(["--mock", "--yes", "setlist", "plan", str(songs), "--name", "gig", "--apply"])
    out = capsys.readouterr().out
    assert "→ _qcsuite_sandbox:1A「Cory Wong Funk Tour」" in out
    songs.write_text(SONGS.replace("Cory Wong Funk Tour", "Cory Wong:1A"))
    main(["--mock", "--yes", "setlist", "plan", str(songs), "--name", "gig3",
          "--prefer-folder", "kami", "--start", "3A", "--apply"])
    out = capsys.readouterr().out
    # The sandbox already holds these names, so the unit (fake) de-duplicates them
    # to 20 characters + _N, and the setlist records the names actually stored.
    assert "→ _qcsuite_sandbox:3A「Cory Wong Funk Tou_1」" in out
    main(["--mock", "setlist", "midi", "gig3", "--format", "csv"])
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[1][3] == "_qcsuite_sandbox" and rows[1][4] == "3A" and rows[1][6] == "0"
    assert rows[1][8] == "16"
    main(["--mock", "setlist", "midi-map", "--set", "_qcsuite_sandbox=9"])
    capsys.readouterr()
    main(["--mock", "setlist", "midi", "gig3", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["rows"][0]["cc32"] == 9
    main(["--mock", "--json", "setlist", "list"])
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert {"gig", "gig3"} <= names


def test_web_flow(client):
    client.post("/api/connect")
    s = client.post("/api/setlist/build", json={"name": "web gig", "text": SONGS,
                                                "prefer_folder": "kami"}).json()
    assert len(s["problems"]) == 1
    s = client.post("/api/setlist/web gig/choose", json={"n": 1, "preset": "Cory Wong:1A"}).json()
    assert not s["problems"]
    plan = client.post("/api/setlist/web gig/plan", json={"start": "3A"}).json()
    assert not plan["refused"] and plan["confirm_word"] == "実行"
    job = client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": "実行"}).json()["job"]
    assert wait_job(client, job)["summary"]["status"] == "done"
    s = client.post("/api/setlist/web gig/refresh").json()
    assert all(x["target"]["placed"] for x in s["songs"])
    midi = client.get("/api/setlist/web gig/midi", params={"format": "json"}).json()
    assert midi["rows"][0]["slot"] == "3A" and midi["rows"][0]["pc"] == 16
    assert client.get("/api/setlist/web gig/midi", params={"format": "csv"}).text.startswith("順番")
    cfg = client.post("/api/setlist/midi-config", json={"mapping": {"Live": 6}}).json()
    assert cfg["mapping"]["Live"] == 6
    refused = client.post("/api/setlist/web gig/plan", json={"folder": "kami"})
    assert refused.status_code == 400 and "保護" in refused.json()["error"]
    assert client.get("/api/setlist/missing").status_code == 404
