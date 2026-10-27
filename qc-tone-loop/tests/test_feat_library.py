"""Preset management: index, search, duplicates, copy plans, views, captures, favorites."""

import json

import pytest
from helpers import wait_job

from qcsuite import plan as plan_mod
from qcsuite.device import fake_data
from qcsuite.errors import QcsuiteError, WriteRefused
from qcsuite.features import backup, library

USER = "/media/p4/Presets"
SANDBOX = f"{USER}/_qcsuite_sandbox"


def _backup(device, folders):
    job = backup.create_job(device, folders, False, device.mock)
    return backup.run_job(device, job)


def test_refs_accept_both_separators():
    assert library.parse_ref("kami/1E") == library.parse_ref("kami:1E")
    assert library.parse_ref("Factory Library:1A")[0] == "/opt/neuraldsp/Factory Library"
    assert library.parse_ref("kami/Henriksen") == (f"{USER}/kami", "Henriksen")


def test_index_search_and_sort(device):
    rows = library.index(device)
    assert len(rows) == 51 + 21 + 20 + 15
    assert all(r["kind"] == "user" for r in rows)
    kami = library.search(rows, folder="kami")
    assert len(kami) == 20 and kami[0]["slot"] == "1A"
    leads = library.search(rows, "LEAD", sort="name")
    assert leads and all("lead" in r["name"].lower() for r in leads)
    assert [r["name"] for r in leads] == sorted((r["name"] for r in leads), key=str.casefold)
    by_slot = library.search(rows, sort="slot")
    assert by_slot[0]["position"] == 0
    assert len(library.index(device, include_factory=True)) == len(rows) + 256
    with pytest.raises(QcsuiteError):
        library.search(rows, sort="size")


def test_name_duplicates_match_the_real_unit(device):
    names = library.name_dupes(library.index(device, include_factory=True))
    assert len(names["groups"]) == 20
    assert len(names["factory_overlap"]) == 10
    zuto = next(g for g in names["groups"] if g["name"] == "ZUTOMAYO_M&M")
    assert {e["folder"] for e in zuto["entries"]} == {"My Presets", "Favorite"}


def test_content_duplicates_come_from_the_backup(device, fake):
    assert not library.content_dupes(True)["available"]
    _backup(device, ["My Presets", "Favorite", "kami"])
    found = library.content_dupes(True)
    assert found["available"]
    groups = [{(e["folder"], e["name"]) for e in g["entries"]} for g in found["groups"]]
    for (folder, name), (src_folder, src_name) in fake_data.IDENTICAL.items():
        assert any({(folder, name), (src_folder, src_name)} <= g for g in groups), (folder, name)
    md = library.cleanup_markdown(library.name_dupes(library.index(device)), found)
    assert "提案" in md and "削除しません" in md and "[残す案]" in md


def test_copy_plan_into_the_sandbox(device, fake):
    items = [{"folder": f"{USER}/kami", "position": 0}, {"folder": f"{USER}/kami", "position": 0}]
    plan = library.copy_plan(device, items)
    assert [s.action for s in plan.steps] == ["create_folder", "copy", "copy"]
    assert [s.position for s in plan.steps[1:]] == [0, 1]
    assert any("タグ" in w for w in plan.warnings)
    assert any("_1" in w for w in plan.warnings)            # second copy collides
    assert SANDBOX not in fake.folders
    assert plan_mod.execute(plan, device)["dry_run"] and SANDBOX not in fake.folders
    result = plan_mod.execute(plan, device, confirm=True)
    assert [r.get("stored_name") for r in result["results"][1:]] == ["Henriksen", "Henriksen_1"]
    # The next plan sees the taken slots.
    again = library.copy_plan(device, [{"folder": f"{USER}/Cory Wong", "position": 3}])
    assert [s.position for s in again.steps] == [2]


def test_copy_plan_refuses_protected_and_unallowed_targets(device, fake):
    item = [{"folder": f"{USER}/Cory Wong", "position": 0}]
    for target in ("kami", "My Presets", "Live"):
        plan = library.copy_plan(device, item, to=target)
        assert plan_mod.check(plan, device)
        with pytest.raises(WriteRefused):
            plan_mod.execute(plan, device, confirm=True)
    device.policy.allow("Live")
    plan_mod.execute(library.copy_plan(device, item, to="Live"), device, confirm=True)
    assert fake.folders[f"{USER}/Live"][0].name == "Cory Wong Funk Tour"
    with pytest.raises(QcsuiteError):
        library.copy_plan(device, [{"folder": f"{USER}/Live", "position": 99}])


def test_show_from_backup_current_and_load(device, fake):
    with pytest.raises(QcsuiteError, match="バックアップにありません"):
        library.show(device, "kami", 4, "backup")
    n = fake.audio_interruptions
    loaded = library.show(device, "kami", 4, "load")
    assert fake.audio_interruptions == n + 1 and loaded["warning"]
    assert loaded["preset"]["name"] == "Brit 2203"
    current = library.show(device, "", 0, "current")
    assert current["preset"]["fingerprint"] == loaded["preset"]["fingerprint"]
    _backup(device, ["kami"])
    n = fake.audio_interruptions
    view = library.show(device, "kami", 4, "backup")
    assert fake.audio_interruptions == n and view["commit"]
    assert "Brit 2203" in library.show_text(view)


def test_resolve_refs(device):
    assert library.resolve(device, "kami/1E")["name"] == "Brit 2203"
    assert library.resolve(device, "My Presets/Henriksen")["slot"] == "1F"
    assert library.resolve(device, "Factory Library/1A")["folder"] == fake_data.FACTORY_KEY
    with pytest.raises(QcsuiteError):
        library.resolve(device, "kami/Nope")
    with pytest.raises(QcsuiteError):
        library.resolve(device, "kami")


def test_captures_irs_catalog_and_candidates(device):
    corn = library.search_captures(device, "corn vixen")
    assert corn["total"] == 4
    assert library.search_captures(device, "", limit=10)["total"] == 2078
    irs = library.search_irs(device, "rex")
    assert irs["total"] == 2
    md = library.candidates(corn["items"])
    assert "Corn Vixen 1" in md and "本体の画面" in md
    csv_text = library.candidates(irs["items"], "csv")
    assert csv_text.splitlines()[0] == "種類,名前,key,メモ"
    plini = library.search_catalog(device, "plini")
    assert plini and all(m["inert_plugin"] for m in plini)
    assert not library.search_catalog(device, "Lane Output")       # internal
    amps = library.search_catalog(device, category="amplifier")
    assert any(m["name"] == "Brit 2203" for m in amps)


def test_folder_usage(device):
    u = library.usage(device)
    names = {f["name"]: f for f in u["folders"]}
    assert names["kami"]["occupied"] == 20 and names["kami"]["protected"]
    assert names["Factory Library"]["kind"] == "factory"
    assert u["user_setlists"] == 8 and u["setlist_limit"] == 12 and u["setlist_free"] == 4
    assert "12" in u["note"]


def test_favorites_comparison_is_read_only(device, fake):
    c = library.favorites_compare(device)
    assert [e["name"] for e in c["both"]] == ["Two Rock Mayer"]
    assert c["both"][0]["hearts"][0]["folder"] == "My Presets"
    assert not c["both"][0]["hearts"][0]["points_at_folder"]
    assert {e["name"] for e in c["hearts_only"]} == {"Cory Wong Funk Tour", "Fuzz This"}
    assert len(c["folder_only"]) == 20
    md = library.favorites_markdown(c)
    assert "ハートを付け外ししません" in md and "Fuzz This" in md
    assert not any(call[0] in ("add_favorite", "remove_favorite") for call in fake.calls)
    missing = library.favorites_compare(device, "Nowhere")
    assert not missing["folder_exists"] and len(missing["hearts_only"]) == 3


def test_cli_library(capsys, tmp_path):
    from qcsuite.cli import main
    main(["--mock", "library", "search", "Henriksen"])
    out = capsys.readouterr().out
    assert "kami" in out and "My Presets" in out
    main(["--mock", "--yes", "library", "copy", "kami/1A"])
    assert "dry-run" in capsys.readouterr().err
    main(["--mock", "--yes", "library", "copy", "kami/1A", "--rename", "H copy", "--apply"])
    assert "H copy" in capsys.readouterr().out
    main(["--mock", "--json", "library", "list", "--folder", "_qcsuite_sandbox"])
    assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["H copy"]
    with pytest.raises(SystemExit, match="保護"):
        main(["--mock", "--yes", "library", "copy", "kami/1A", "--to", "Favorite", "--apply"])
    md = tmp_path / "dupes.md"
    main(["--mock", "library", "dupes", "--markdown", str(md)])
    assert "重複の整理案" in md.read_text()
    main(["--mock", "library", "favorites"])
    assert "Two Rock Mayer" in capsys.readouterr().out
    main(["--mock", "library", "usage"])
    assert "12" in capsys.readouterr().out


def test_web_library(client):
    rows = client.get("/api/library/index", params={"q": "lead", "sort": "name"}).json()
    assert rows and all("lead" in r["name"].lower() for r in rows)
    d = client.get("/api/library/dupes").json()
    assert len(d["names"]["groups"]) == 20 and not d["contents"]["available"]
    plan = client.post("/api/library/copy-plan", json={
        "items": [{"folder": f"{USER}/kami", "position": 4}]}).json()
    assert not plan["refused"] and plan["confirm_word"]
    job = client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": "実行"}).json()["job"]
    assert wait_job(client, job)["summary"]["status"] == "done"
    refused = client.post("/api/library/copy-plan", json={
        "items": [{"folder": f"{USER}/kami", "position": 4}], "to": "Favorite"}).json()
    assert refused["refused"]
    fav = client.get("/api/library/favorites").json()
    assert fav["both"][0]["name"] == "Two Rock Mayer" and "markdown" in fav
    caps = client.get("/api/library/captures", params={"q": "corn"}).json()
    assert caps["total"] == 4
    text = client.post("/api/library/candidates", json={"items": caps["items"], "format": "csv"}).text
    assert "Corn Vixen 1" in text
    assert client.get("/api/library/usage").json()["setlist_limit"] == 12
    missing = client.get("/api/library/show", params={"folder": f"{USER}/kami", "position": 0}).json()
    assert missing["missing"]
    view = client.get("/api/library/show", params={"mode": "current"}).json()
    assert view["source"] == "current" and view["chain"]
    loaded = client.post("/api/library/load", json={"folder": f"{USER}/kami", "position": 4}).json()
    assert loaded["preset"]["name"] == "Brit 2203"
    assert client.get("/static/library.html").status_code == 200
