"""Bulk reamp into stems (feature 9), first-arrival alignment (idea I), sound map (idea K)."""

import csv
import json

import numpy as np
import pytest
import soundfile as sf

from helpers import wait_job
from qcsuite import jobs, paths
from qcsuite.audio import di as di_mod
from qcsuite.audio import dsp, render
from qcsuite.errors import QcsuiteError
from qcsuite.features import batch

SR = 48000
LANEY, LANEY_1, LANEY_2, BRIT = "kami:1G", "kami:1H", "kami:2A", "kami:1E"
AAAA = "My Presets:2H"           # holds an unowned Plini block (slot 12 is a gap)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    di_mod.save("short", di_mod.synth_di(1.5), SR, True, source="test")


def _onset(x, threshold_db=-35.0):
    from qctone.audio_io import onset
    return onset(x.mean(axis=1) if x.ndim == 2 else x, threshold_db)


def test_preset_refs():
    assert batch.parse_preset_ref("kami:1E") == ("/media/p4/Presets/kami", 4)
    assert batch.parse_preset_ref("My Presets:4") == ("/media/p4/Presets/My Presets", 4)
    assert batch.parse_preset_ref("/media/p4/Presets/kami:2A") == ("/media/p4/Presets/kami", 8)
    with pytest.raises(QcsuiteError, match="フォルダ:スロット"):
        batch.parse_preset_ref("kami")


def test_items_are_preset_major_and_checked(device):
    items = batch.build_items(device, ["short", "synthetic-riff"], [LANEY, AAAA])
    assert [(i["di"], i["name"]) for i in items] == [
        ("short", "Laney test"), ("synthetic-riff", "Laney test"),
        ("short", "aaaa"), ("synthetic-riff", "aaaa")]
    assert [i["n"] for i in items] == [1, 1, 2, 2]
    with pytest.raises(QcsuiteError, match="空です"):
        batch.build_items(device, ["short"], ["Live:1A"])
    with pytest.raises(QcsuiteError, match="DI を"):
        batch.build_items(device, [], [LANEY])
    assert len(batch.build_items(device, ["short"], folder="Cory Wong")) == 15


def test_first_arrival_survives_high_gain_and_delay(rig):
    """The shared correlation drifts here; the stem alignment must not."""
    rig.device.read_stored("/media/p4/Presets/kami", 6)        # Laney test: has a delay block
    rig.device.prepare_reamp()
    amp = next(b for b in rig.device.grid()["blocks"] if b["category"] == "Guitar Amplifier")
    knob = next(p["name"] for p in amp["params"] if p["name"] in ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1"))
    rig.device.apply_patch({"ops": [{"op": "set_param", "row": 0, "col": amp["col"],
                                     "param": knob, "value": 0.95}]})
    di, _ = di_mod.load("short", True)
    result, diag = batch.reamp_aligned(rig, di, SR)
    assert diag["method"] == "first_arrival"
    assert result.latency_ms == pytest.approx(render.MEASURED_LATENCY_MS, abs=1.0)
    assert abs(_onset(result.audio) - _onset(di)) < SR * 0.002
    assert len(result.audio) == len(di) + int(render.TAIL_S * SR)


def _run(rig, dis, refs, fmt="wav"):
    job = batch.create(rig, dis, refs, None, fmt)
    return job, batch.run(rig, job)


def test_stems_are_aligned_named_and_listed(rig):
    job, summary = _run(rig, ["short"], [LANEY, BRIT, AAAA])
    assert summary["status"] == "done" and summary["excluded"] == 1
    folder = paths.home(True) / "batch" / job.id / "short"
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["00_short__DI.wav", "01_short__Laney test.wav", "02_short__Brit 2203.wav",
                     "03_short__aaaa__EXCLUDED.wav"]
    di, _ = di_mod.load("short", True)
    di_onset = _onset(di)
    lengths = set()
    for name in names:
        info = sf.info(str(folder / name))
        assert (info.samplerate, info.channels, info.subtype) == (SR, 2, "PCM_24")
        lengths.add(info.frames)
        if "EXCLUDED" in name:
            continue
        audio, _ = sf.read(str(folder / name))
        assert abs(_onset(audio) - di_onset) < SR * 0.005, name
    assert lengths == {len(di) + int(render.TAIL_S * SR)}
    with open(paths.home(True) / summary["csv"], encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert [r["preset"] for r in rows] == ["(DI)", "Laney test", "Brit 2203", "aaaa"]
    assert rows[3]["excluded"] == "yes" and "Plini" in rows[3]["reasons"]
    assert rows[1]["align_method"] == "first_arrival"
    readme = (paths.home(True) / summary["readme"]).read_text()
    assert "EXCLUDED" in readme and "1 小節目" in readme
    assert summary["latency"]["warning"] is None


def test_flac_format_and_failed_items_are_reported(rig):
    job = batch.create(rig, ["short"], [LANEY], None, "flac")
    rig.fake.folders["/media/p4/Presets/kami"].pop(6)    # the preset vanished before the run
    summary = batch.run(rig, job)
    assert summary["status"] == "done_with_errors"
    assert "失敗" in (paths.home(True) / summary["readme"]).read_text()
    assert sf.info(str(paths.home(True) / "batch" / job.id / "short" / "00_short__DI.flac")).format == "FLAC"


def test_resume_does_not_redo_finished_stems(rig):
    job = batch.create(rig, ["short"], [LANEY, LANEY_1, BRIT], None)

    def stop(done, total, key):
        if done == 1:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        batch.run(rig, job, on_progress=stop)
    renders = rig.renderer.renders
    summary = batch.run(rig, jobs.open_job(job.id, True))
    assert summary["ok"] == 3 and rig.renderer.renders == renders + 2


def test_sound_map_puts_identical_presets_together(rig):
    job, _ = _run(rig, ["short"], [LANEY, LANEY_1, LANEY_2, BRIT, AAAA, "My Presets:1A"])
    data = batch.sound_map(job)
    names = [p["name"] for p in data["points"]]
    assert "aaaa" not in names and data["excluded"] == ["aaaa"]
    laney = next(p for p in data["points"] if p["name"] == "Laney test")
    first = data["neighbours"][laney["key"]][0]
    assert first["name"] in ("Laney test_1", "Laney test_2") and first["distance"] == 0.0
    brit = next(p for p in data["points"] if p["name"] == "Brit 2203")
    assert data["neighbours"][brit["key"]][0]["distance"] > 0
    assert len(data["matrix"]) == len(names) and "良し悪し" in data["note"]
    near = batch.nearest(job, "kami:1G")
    assert near["preset"]["name"] == "Laney test"
    with pytest.raises(QcsuiteError, match="地図にありません"):
        batch.nearest(job, "aaaa")


def test_map_needs_results_of_the_chosen_di(rig):
    job = batch.create(rig, ["short"], [LANEY], None)
    with pytest.raises(QcsuiteError, match="まだ結果"):
        batch.sound_map(job)
    batch.run(rig, job)
    with pytest.raises(QcsuiteError, match="DI「x」"):
        batch.sound_map(job, "x")


def test_cli_run_show_map(capsys, tmp_path):
    from qcsuite.cli import main
    main(["--mock", "--yes", "--json", "batch", "run", "--di", "short", "--preset", LANEY,
          "--preset", LANEY_1, "--preset", BRIT])
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] == 3
    main(["--mock", "--json", "batch", "list"])
    assert json.loads(capsys.readouterr().out)[0]["id"] == summary["id"]
    main(["--mock", "--json", "batch", "show", summary["id"]])
    assert len(json.loads(capsys.readouterr().out)["stems"]) == 3
    main(["--mock", "--json", "batch", "map", summary["id"], "--near", "Laney test"])
    assert json.loads(capsys.readouterr().out)["neighbours"][0]["name"] == "Laney test_1"
    png = tmp_path / "map.png"
    main(["--mock", "batch", "map", summary["id"], "--png", str(png)])
    assert png.stat().st_size > 5000 and "音の地図" in capsys.readouterr().out


def test_web_flow(client):
    r = client.post("/api/batch/run", json={"dis": ["short"], "presets": [LANEY, LANEY_1, AAAA]}).json()
    assert r["items"] == 3
    detail = wait_job(client, r["job"])
    assert detail["summary"]["status"] == "done", detail
    show = client.get(f"/api/batch/{r['job']}").json()
    assert [s["excluded"] for s in show["stems"]] == [False, False, True]
    assert client.get("/api/files", params={"path": show["stems"][0]["file"]}).status_code == 200
    assert client.get("/api/files", params={"path": f"batch/{r['job']}/stems.csv"}).status_code == 200
    m = client.get(f"/api/batch/{r['job']}/map").json()
    assert len(m["points"]) == 2
    near = client.get(f"/api/batch/{r['job']}/near", params={"name": "Laney test"}).json()
    assert near["neighbours"][0]["name"] == "Laney test_1"
    assert any(j["id"] == r["job"] for j in client.get("/api/batch/jobs").json())
    whole = client.post("/api/batch/run", json={"dis": ["short"], "folder": "/media/p4/Presets/Cory Wong"})
    assert whole.json()["items"] == 15
    client.post(f"/api/jobs/{whole.json()['job']}/cancel")
    wait_job(client, whole.json()["job"])
