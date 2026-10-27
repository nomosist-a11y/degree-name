"""Backups as JSON in git: run, diff, resume, restore into the sandbox only."""

import json

import pyquadcortex as pq
import pytest
from helpers import wait_job

from qcsuite import jobs, presets
from qcsuite import plan as plan_mod
from qcsuite.errors import QcsuiteError, WriteRefused
from qcsuite.features import backup

USER = "/media/p4/Presets"
SANDBOX = f"{USER}/_qcsuite_sandbox"


def _run(device, folders):
    return backup.run_job(device, backup.create_job(device, folders, False, device.mock))


def _touch_stored(fake, folder, position, value):
    """Simulate the player turning a knob on the unit and saving."""
    stored = fake.folders[f"{USER}/{folder}"][position]
    p = fake._materialize(stored)
    amp = next(b for b in pq.blocks(p)
               if fake._catalog_obj[b.model_id].category == "Guitar Amplifier")
    for pv in p.chains[amp.row].models[amp.column].params[0].param_values:
        pv.float_value = value


def test_backup_writes_json_index_listings_and_commits(device, fake):
    result = _run(device, ["kami"])
    repo = backup.backup_dir(True)
    assert result["commit"] and result["files"] == 20
    files = sorted(p.relative_to(repo).as_posix() for p in (repo / "kami").glob("*.json"))
    assert len(files) == 20 and files[4] == "kami/004_1E_Brit 2203.json"
    n = json.loads((repo / files[4]).read_text())
    assert n["schema"] == presets.SCHEMA and n["slot"] == "1E"
    index = json.loads((repo / "index.json").read_text())
    assert len(index) == 20 and index[0]["fingerprint"]
    assert len(json.loads((repo / "_listings" / "kami.json").read_text())) == 20
    assert len(backup.load_latest(True)) == 20
    assert backup.latest_by_key(True)[(f"{USER}/kami", 4)]["name"] == "Brit 2203"
    assert sum(1 for c in fake.calls if c[0] == "read_preset") == 20
    assert backup.log(True)[0]["message"].startswith("backup ")
    # Nothing changed: no new commit.
    assert _run(device, ["kami"])["commit"] is None
    assert len(backup.log(True)) == 1


def test_second_backup_shows_what_changed(device, fake):
    _run(device, ["Cory Wong"])
    _touch_stored(fake, "Cory Wong", 2, 0.97)
    del fake.folders[f"{USER}/Cory Wong"][14]
    second = _run(device, ["Cory Wong"])
    assert second["commit"]
    d = backup.diff(True)
    kinds = {p["kind"] for p in d["presets"]}
    assert kinds == {"changed", "removed"}
    changed = next(p for p in d["presets"] if p["kind"] == "changed")
    assert changed["slot"] == "Cory Wong 1C" and "→" in changed["text"]
    assert "Solo Funk" in backup.diff_report(d)
    assert d["stat"]
    one = backup.diff(True, ref="Cory Wong/1C")
    assert [p["slot"] for p in one["presets"]] == ["Cory Wong 1C"]
    assert len(backup.preset_log(True, "Cory Wong/1C")) == 2
    # The first commit against the working tree shows the same change.
    first = backup.log(True)[-1]["commit"]
    assert backup.diff(True, first)["to"] == "working"


def test_interrupted_backup_resumes_and_commits_once(device, monkeypatch):
    job = backup.create_job(device, ["Cory Wong"], False, True)
    real = device.read_stored
    calls = []
    interrupt = [True]

    def flaky(key, position):
        calls.append(position)
        if len(calls) == 5 and interrupt[0]:
            interrupt[0] = False
            raise KeyboardInterrupt
        return real(key, position)

    monkeypatch.setattr(device, "read_stored", flaky)
    with pytest.raises(KeyboardInterrupt):
        backup.run_job(device, job)
    assert job.state()["status"] == "interrupted"
    assert not backup.has_commits(True)
    calls.clear()
    result = backup.run_job(device, jobs.open_job(job.id, True))
    assert len(calls) == 11 and result["commit"] and result["files"] == 15
    assert len(backup.load_latest(True)) == 15 and len(backup.log(True)) == 1


def test_failed_items_are_reported_not_hidden(device, monkeypatch):
    real = device.read_stored

    def broken(key, position):
        if position == 3:
            raise TimeoutError("no RecallPreset broadcast")
        return real(key, position)

    monkeypatch.setattr(device, "read_stored", broken)
    result = backup.run_job(device, backup.create_job(device, ["Cory Wong"], False, True))
    assert result["failed"] == 1
    assert "failed" in backup.log(True)[0]["message"]
    assert "失敗 1 件" in result["job"]["message"]


def test_refs_and_find(device):
    _run(device, ["kami"])
    rel, n = backup.find(True, "kami/1E")
    assert n["name"] == "Brit 2203"
    assert backup.find(True, "kami/Brit 2203")[0] == rel
    assert backup.find(True, rel)[1]["slot"] == "1E"
    commit = backup.log(True)[0]["commit"]
    assert backup.find(True, "kami/1E", commit)[1]["fingerprint"] == n["fingerprint"]
    with pytest.raises(QcsuiteError):
        backup.find(True, "kami/9H")


def test_restore_only_into_the_sandbox(device, fake):
    _run(device, ["kami"])
    _, n = backup.find(True, "kami/1E")
    plan = backup.restore_plan(device, n)
    assert [s.action for s in plan.steps] == ["create_folder", "rebuild"]
    assert any("タグ" in w or "20 文字" in w for w in plan.warnings)
    assert plan_mod.execute(plan, device)["dry_run"] and SANDBOX not in fake.folders
    plan_mod.execute(plan, device, confirm=True)
    restored = presets.normalize(device.read_stored(SANDBOX, 0), device.catalog)
    assert restored["name"] == "Brit 2203"
    assert [c["where"] for c in presets.diff(n, restored)] == []
    with pytest.raises(WriteRefused, match="元のフォルダ"):
        backup.restore_plan(device, n, to="kami")
    protected = backup.restore_plan(device, n, to="My Presets")
    with pytest.raises(WriteRefused):
        plan_mod.execute(protected, device, confirm=True)
    assert len(fake.folders[f"{USER}/My Presets"]) == 51
    # Restoring onto an occupied sandbox slot is an overwrite, so destructive.
    again = backup.restore_plan(device, n, slot="1A", name="again")
    assert again.steps[-1].overwrites == "Brit 2203" and again.destructive


def test_capture_presets_warn_on_restore(device):
    _run(device, ["My Presets"])
    with_capture = [n for n in backup.load_latest(True)
                    if any(b["model_id"] in (14000, 14001) for b in n["blocks"])]
    assert with_capture
    plan = backup.restore_plan(device, with_capture[0])
    assert any("キャプチャー" in w for w in plan.warnings)


def test_cli_backup(capsys):
    from qcsuite.cli import main
    main(["--mock", "--yes", "backup", "run", "--folders", "Cory Wong"])
    assert "15 件を保存しました" in capsys.readouterr().out
    main(["--mock", "backup", "status"])
    assert "Cory Wong: 15" in capsys.readouterr().out
    main(["--mock", "backup", "log"])
    assert "backup " in capsys.readouterr().out
    main(["--mock", "backup", "show", "Cory Wong/1A"])
    assert "Cory Wong Funk Tour" in capsys.readouterr().out
    main(["--mock", "backup", "diff"])
    assert "違いはありません" in capsys.readouterr().out
    main(["--mock", "--yes", "backup", "restore", "Cory Wong/1A"])
    assert "dry-run" in capsys.readouterr().err
    main(["--mock", "--yes", "backup", "restore", "Cory Wong/1A", "--apply"])
    assert "復元しました" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="元のフォルダ"):
        main(["--mock", "--yes", "--allow-folder", "Live", "backup", "restore",
              "Cory Wong/1A", "--to", "Cory Wong"])


def test_cli_backup_resume(capsys):
    from qcsuite.cli import main
    from qcsuite.device import fake as fake_mod
    from qcsuite.device.session import open_rig
    with open_rig(mock=True) as rig:
        job = backup.create_job(rig.device, ["Cory Wong"], False, True)
    main(["--mock", "backup", "run", "--resume", job.id])
    assert "15 件を保存しました" in capsys.readouterr().out
    assert fake_mod  # imported for clarity: the CLI used the persisted fake


def test_web_backup(client):
    s = client.get("/api/backup/status").json()
    assert s["presets"] == 0 and s["recall_warning"]
    job = client.post("/api/backup/run", json={"folders": [f"{USER}/Cory Wong"]}).json()["job"]
    detail = wait_job(client, job)
    assert detail["summary"]["status"] == "done", detail
    assert detail["results"][-1]["key"] == backup.COMMIT_KEY
    assert detail["results"][-1]["result"]["commit"]
    assert client.get("/api/backup/status").json()["presets"] == 15
    rows = client.get("/api/backup/presets").json()
    assert len(rows) == 15
    view = client.get("/api/backup/show", params={"ref": rows[0]["file"]}).json()
    assert view["chain"]
    assert len(client.get("/api/backup/log").json()) == 1
    assert "違いはありません" in client.get("/api/backup/diff").json()["text"]
    plan = client.post("/api/backup/restore", json={"ref": rows[0]["file"]}).json()
    assert not plan["refused"] and plan["warnings"]
    job = client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": "実行"}).json()["job"]
    assert wait_job(client, job)["summary"]["status"] == "done"
    bad = client.post("/api/backup/restore", json={"ref": rows[0]["file"], "to": "Cory Wong"})
    assert bad.status_code == 400 and "元のフォルダ" in bad.json()["error"]
    assert client.get("/static/backup.html").status_code == 200
