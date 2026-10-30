"""Health check (idea A) and pre-gig check (idea B) on the fake unit."""

import copy
import json

import pytest

from helpers import route_usb, wait_job

from qcsuite import jobs, presets
from qcsuite.features import health
from qcsuite.features import setlist as sl
from qcsuite.features import sheets_sources as src

USER = "/media/p4/Presets"


def _codes(issues, severity=None):
    return {i["code"] for i in issues if severity is None or i["severity"] == severity}


def _norm(device, ref):
    key, position = src.parse_ref(ref)
    return presets.normalize(device.read_stored(key, position), device.catalog,
                             folder=key, position=position)


def test_my_presets_findings(device):
    report = health.scan(src.collect(device, mock=True, folder="My Presets"), "test")
    by = {r["name"]: r for r in report["presets"]}
    assert "usb_input" in _codes(by["Test2"]["issues"], health.MUST)
    assert "inert_plugin" in _codes(by["aaaa"]["issues"], health.MUST)
    assert report["presets"][0]["worst"] == health.MUST
    assert report["counts"][health.MUST] == 2
    assert "same_content" in _codes(by["ZUTOMAYO_M&M_1"]["issues"])
    assert any("capture" in _codes(r["issues"]) for r in report["presets"])


def test_a_clean_factory_preset_has_nothing_serious(device):
    issues = health.check_preset(_norm(device, "Factory Library:1D"))
    assert not _codes(issues, health.MUST) and not _codes(issues, health.WARN)


def test_synthetic_problems_are_found(device):
    base = _norm(device, "kami:1E")
    muted = copy.deepcopy(base)
    muted["rows"][0]["lane"]["MUTE"]["value"] = 1.0
    assert "muted" in _codes(health.check_preset(muted), health.MUST)
    off = copy.deepcopy(base)
    off["rows"][0]["lane"]["VOLUME"] = {"value": 0.0, "db": -40.0}
    assert "volume_off" in _codes(health.check_preset(off), health.MUST)
    loud = copy.deepcopy(base)
    loud["rows"][0]["lane"]["VOLUME"] = {"value": 0.95, "db": 9.4}
    assert "volume_high" in _codes(health.check_preset(loud), health.WARN)
    nowhere = copy.deepcopy(base)
    nowhere["rows"][0]["out_port"] = "EMPTY"
    assert {"no_output", "dead_end"} <= _codes(health.check_preset(nowhere))
    out34 = copy.deepcopy(base)
    out34["rows"][0]["out_port"] = "OUT_3_4"
    assert "other_output" in _codes(health.check_preset(out34), health.WARN)
    scenes = copy.deepcopy(base)
    scenes["blocks"][0]["bypass"] = {"scene_mode": True, "scenes": [True, False] * 4}
    scenes["scene_labels"] = [""] * 8
    assert "unnamed_scenes" in _codes(health.check_preset(scenes), health.WARN)
    heavy = copy.deepcopy(base)
    amp = next(b for b in heavy["blocks"] if b["category"] == "Guitar Amplifier")
    heavy["blocks"] += [dict(amp, col=6), dict(amp, col=7)]
    assert "dsp_heavy" in _codes(health.check_preset(heavy), health.WARN)
    gate = copy.deepcopy(base)
    gate["rows"][0]["gate"]["NOISE REDUCTION"] = {"value": 0.9}
    assert "strong_gate" in _codes(health.check_preset(gate), health.INFO)
    unnamed = copy.deepcopy(base)
    unnamed["name"] = ""
    assert "no_name" in _codes(health.check_preset(unnamed), health.WARN)


def test_current_grid_left_on_usb5_is_flagged(device):
    device.read_stored(f"{USER}/kami", 4)
    assert not _codes(health.check_preset(src.current(device)), health.MUST)
    route_usb(device)
    assert "usb_input" in _codes(health.check_preset(src.current(device)), health.MUST)


def test_report_markdown_and_persistence(device):
    report = health.scan([_norm(device, "My Presets:1G"), _norm(device, "kami:1E")], "test")
    md = health.report_markdown(report)
    assert "**要対応**" in md and "Test2" in md and "問題なし" in md
    health.save_report(report, True)
    assert health.last_report(True)["scanned"] == 2


def test_folder_scan_job_is_resumable(device, fake):
    job = health.create_folder_job(device, "kami", True)
    calls = {"n": 0}
    real_read = src.read

    def flaky(dev, folder, position, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return real_read(dev, folder, position, **kw)

    src_read = src.read
    try:
        src.read = flaky
        with pytest.raises(KeyboardInterrupt):
            health.run_folder_job(device, job, mock=True)
    finally:
        src.read = src_read
    assert job.state()["status"] == "interrupted" and len(job.done_keys()) == 2
    report = health.run_folder_job(device, jobs.open_job(job.id, True), mock=True)
    assert report["scanned"] == 20
    laney = next(r for r in report["presets"] if r["name"] == "Laney test")
    assert "same_content" in _codes(laney["issues"])


def test_from_backup_uses_no_recall(device, fake, monkeypatch):
    items = [_norm(device, "My Presets:1G"), _norm(device, "kami:1E")]
    monkeypatch.setattr(src, "backup_presets", lambda mock: items)
    before = fake.audio_interruptions
    report = health.scan(src.collect(device, mock=True, from_backup=True), "backup")
    assert report["scanned"] == 2 and fake.audio_interruptions == before


def _gig(device, lines):
    s = sl.build("gig", lines, device, mock=True)
    sl.use_existing(s)
    sl.save_setlist(s, True)
    return s


def test_gig_check_without_leveler_data(device, monkeypatch):
    monkeypatch.setattr(src, "leveler_measurements", lambda mock: None)
    _gig(device, "Opener | My Presets:1G\nBallad | kami:1E\n")
    g = health.gig_check(device, "gig", mock=True)
    first, second = g["songs"]
    assert first["status"] == "NG"
    assert any(c["item"] == "入力が Input 1" and c["ok"] is False for c in first["checks"])
    assert second["status"] == "未確認あり"
    assert any(c["item"].startswith("音量") and c["ok"] is None and "未測定" in c["detail"]
               for c in second["checks"])
    md = health.gig_markdown(g)
    assert "**NG**" in md and "未確認" in md


def test_gig_check_with_leveler_data(device, monkeypatch):
    measured = {(f"{USER}/kami", 4): {"lufs": -18.2}, (f"{USER}/kami", 0): {"lufs": -18.6},
                (f"{USER}/Cory Wong", 0): {"lufs": -14.0}}
    monkeypatch.setattr(src, "leveler_measurements", lambda mock: measured)
    _gig(device, "A | kami:1E\nB | kami:1A\nC | Cory Wong:1A\n")
    g = health.gig_check(device, "gig", mock=True)
    levels = {s["song"]: next(c for c in s["checks"] if c["item"].startswith("音量")) for s in g["songs"]}
    assert levels["A"]["ok"] and levels["B"]["ok"] and levels["C"]["ok"] is False
    assert g["median_lufs"] == pytest.approx(-18.2)
    assert g["songs"][0]["status"] == "OK"


def test_gig_check_reports_missing_presets(device, monkeypatch):
    monkeypatch.setattr(src, "leveler_measurements", lambda mock: None)
    s = sl.build("gig", "A | kami:1E\n", device, mock=True)
    key = device.ensure_sandbox()
    s["placement"] = {"mode": "copy", "folder": key, "folder_name": "_qcsuite_sandbox", "start": "1A"}
    sl.save_setlist(s, True)
    g = health.gig_check(device, "gig", mock=True)
    assert g["songs"][0]["status"] == "NG" and "空きスロット" in g["songs"][0]["checks"][0]["detail"]


def test_cli_scan_report_and_gig(capsys, tmp_path):
    from qcsuite.cli import main
    main(["--mock", "--yes", "health", "scan", "--folder", "My Presets"])
    out = capsys.readouterr().out
    assert "要対応" in out and "Test2" in out and "aaaa" in out
    main(["--mock", "health", "report", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["counts"]["要対応"] == 2
    main(["--mock", "health", "scan", "--current"])
    assert "プリセット健康診断" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="バックアップがありません"):
        main(["--mock", "health", "scan", "--from-backup"])
    songs = tmp_path / "s.txt"
    songs.write_text("A | kami:1E\n")
    main(["--mock", "--yes", "setlist", "plan", str(songs), "--name", "g", "--map"])
    capsys.readouterr()
    main(["--mock", "health", "gig", "g"])
    assert "ライブ前チェック" in capsys.readouterr().out


def test_web_flow(client):
    client.post("/api/connect")
    r = client.post("/api/health/scan", json={"source": "current"}).json()
    assert r["report"]["scanned"] == 1 and "健康診断" in r["markdown"]
    assert client.post("/api/health/scan", json={"source": "backup"}).status_code == 400
    job = client.post("/api/health/scan", json={"source": "folder", "folder": "My Presets"}).json()["job"]
    assert wait_job(client, job)["summary"]["status"] == "done"
    rep = client.get(f"/api/health/job/{job}").json()["report"]
    assert rep["scanned"] == 51 and rep["counts"]["要対応"] == 2
    assert "Test2" in client.get("/api/health/last.md").text
    refs = client.post("/api/health/scan", json={"source": "refs", "refs": ["My Presets:2H"]}).json()
    assert "inert_plugin" in {i["code"] for i in refs["report"]["presets"][0]["issues"]}
    client.post("/api/setlist/build", json={"name": "wg", "text": "A | kami:1E\n"})
    client.post("/api/setlist/wg/map")
    g = client.get("/api/health/gig/wg", params={"no_read": True}).json()
    assert g["songs"][0]["where"] == "kami:1E" and "ライブ前チェック" in g["markdown"]
