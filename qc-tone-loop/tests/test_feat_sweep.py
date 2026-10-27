"""Sweeps (feature 6) and the sensitivity map (idea M), on the fake unit."""

import json

import numpy as np
import pytest
import soundfile as sf

from helpers import wait_job
from qcsuite import jobs, paths
from qcsuite.audio import di as di_mod
from qcsuite.audio import dsp
from qcsuite.device import fake_data
from qcsuite.errors import QcsuiteError
from qcsuite.features import sweep

KAMI_BRIT = {"preset": {"folder": "kami", "slot": "1E"}}     # Brit 2203 sits at col 2 (index 1)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    di_mod.save("short", di_mod.synth_di(1.5), 48000, True, source="test")


def _spec(**over):
    spec = {"title": "t", "di": "short", "base": KAMI_BRIT, "mode": "grid",
            "cells": [{"row": 0, "col": 1, "params": {"gain": [0.1, 0.9]}}]}
    spec.update(over)
    return spec


def _run(rig, spec):
    job, info = sweep.create(rig, spec)
    base = json.loads((job.dir / "base_grid.json").read_text())
    sweep.run(rig, job, base_grid=base)
    return job, info


# -- spec ----------------------------------------------------------------------------

@pytest.mark.parametrize("layout, gain", [(0, "GAIN"), (1, "DRIVE"), (2, "GAIN 1"), (3, "INPUT GAIN")])
def test_aliases_follow_each_amp_layout(layout, gain):
    catalog = fake_data.build_catalog()
    model = next(m for m in catalog if m.category == "Guitar Amplifier" and m.id % 4 == layout
                 and not m.plugin_id)
    assert sweep.resolve_param(model, "gain").name == gain
    assert sweep.resolve_param(model, "mid").name in ("MID", "MIDDLE")
    assert sweep.resolve_param(model, "treble").name == "TREBLE"
    assert sweep.resolve_param(model, "TREBLE").name == "TREBLE"
    depth = sweep.resolve_param(model, "depth")
    assert depth is None or depth.name in ("DEPTH", "RESONANCE")
    if layout == 1:
        assert sweep.resolve_param(model, "BRIGHT") is None


def test_switches_only_move_by_option_name():
    catalog = fake_data.build_catalog()
    amp = next(m for m in catalog if m.category == "Guitar Amplifier" and m.id % 4 == 0)
    bright = amp.parameter("BRIGHT")
    with pytest.raises(QcsuiteError, match="スイッチ"):
        sweep._values(bright, [0.0, 1.0])
    assert sweep._values(bright, {"options": ["Off", "On"]}) == [{"option": "Off"}, {"option": "On"}]
    with pytest.raises(QcsuiteError, match="スイッチではない"):
        sweep._values(amp.parameter("GAIN"), {"options": ["a"]})
    with pytest.raises(QcsuiteError, match="real"):
        sweep._values(amp.parameter("LEVEL"), {"real": [0.5]})
    with pytest.raises(QcsuiteError, match="範囲外"):
        sweep._values(amp.parameter("GAIN"), {"real": [11]})
    with pytest.raises(QcsuiteError, match="0..1"):
        sweep._values(amp.parameter("GAIN"), [1.5])
    assert sweep._values(amp.parameter("GAIN"), {"from": 0, "to": 1, "steps": 3}) == \
        [{"value": 0.0}, {"value": 0.5}, {"value": 1.0}]


def test_grid_and_one_at_a_time_counts():
    catalog = fake_data.build_catalog()
    spec = {"mode": "grid", "cells": [{"row": 0, "col": 1, "models": ["Brit 2203", "PV 505 Lead"],
                                       "params": {"gain": [0.1, 0.5, 0.9], "treble": [0.3, 0.7],
                                                  "BRIGHT": {"options": ["Off", "On"]}}}]}
    grid = sweep.expand(spec, catalog)
    # Neither fake layout of these two amps has BRIGHT: 2 models x 3 x 2.
    assert len(grid["configs"]) == 12
    assert any("BRIGHT" in n for n in grid["notes"])
    one = sweep.expand({**spec, "mode": "one_at_a_time"}, catalog)
    assert len(one["configs"]) == 1 + 1 + 3 + 2
    assert one["configs"][0]["varied"] is None
    assert one["configs"][2]["varied"]["param"] in ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1")
    with pytest.raises(QcsuiteError, match="models を指定"):
        sweep.expand({"cells": [{"row": 3, "col": 7, "params": {"gain": [0.5]}}]}, catalog,
                     {"rows": [], "blocks": []})


# -- running ------------------------------------------------------------------------------

def test_small_sweep_end_to_end(rig):
    job, info = _run(rig, _spec(cells=[{"row": 0, "col": 1,
                                        "params": {"gain": [0.1, 0.9], "treble": [0.3, 0.7]}}]))
    assert info["count"] == 4
    results = job.results()
    assert len(results) == 4 and all(r["ok"] for r in results)
    for r in results:
        assert (paths.home(True) / r["result"]["file"]).exists()
        assert r["result"]["render"]["latency_ms"] > 300      # the ~312 ms round trip is trimmed
    data = sweep.results(job, "flatness")
    assert data["rows"][0]["label"].endswith("TREBLE=0.7") or "=0.9" in data["rows"][0]["label"]
    by_key = {r["key"]: r for r in sweep.rows(job)}
    assert by_key["c0003"]["flatness"] > by_key["c0000"]["flatness"]     # gain 0.9 vs 0.1
    # The base grid is restored before each config: the stored preset is untouched.
    assert rig.fake.folders["/media/p4/Presets/kami"][4].preset.chains[0].in_portid == 1


def test_unowned_plugin_and_capture_configs_are_excluded(rig):
    job, _ = _run(rig, _spec(cells=[{"row": 0, "col": 1, "models": ["Plini Lead", "Eltron 30"],
                                     "params": {"gain": [0.6]}}]))
    rows = sweep.rows(job)
    assert len(rows) == 2 and all(r["excluded"] for r in rows)
    reasons = " ".join(" ".join(r["reasons"]) for r in rows)
    assert "未所有のプラグイン" in reasons and "キャプチャー" in reasons
    assert sweep.results(job, include_excluded=False)["rows"] == []


def test_resume_after_interruption(rig):
    job, _ = sweep.create(rig, _spec(cells=[{"row": 0, "col": 1, "params": {"gain": [0.1, 0.4, 0.7, 0.9]}}]))

    def stop(done, total, key):
        if done == 2:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        sweep.run(rig, job, on_progress=stop)
    assert job.state()["status"] == "interrupted" and len(job.done_keys()) == 2
    renders = rig.renderer.renders
    rig.device.read_stored("/media/p4/Presets/My Presets", 0)          # the grid moved on meanwhile
    sweep.run(rig, jobs.open_job(job.id, True))
    assert job.done_keys() == {"c0000", "c0001", "c0002", "c0003"}
    assert rig.renderer.renders == renders + 2
    labels = [r["label"] for r in sweep.rows(job)]
    assert all("Brit 2203" in label for label in labels)


def test_scoring_against_a_reference(rig, tmp_path):
    first, _ = _run(rig, _spec(cells=[{"row": 0, "col": 1, "params": {"gain": [0.9]}}]))
    ref = tmp_path / "ref.flac"
    audio, sr = sf.read(str(paths.home(True) / first.results()[0]["result"]["file"]))
    sf.write(str(ref), audio, sr)
    job, _ = _run(rig, _spec(reference={"file": str(ref)},
                             cells=[{"row": 0, "col": 1, "params": {"gain": [0.1, 0.5, 0.9]}}]))
    rows = sweep.results(job, "score")["rows"]
    assert rows[0]["key"] == "c0002" and rows[0]["distance"] < 0.5
    assert rows[-1]["score"] < rows[0]["score"]


def test_large_sweeps_need_a_go_ahead(rig):
    spec = _spec(cells=[{"row": 0, "col": 1, "params": {"gain": {"from": 0, "to": 1, "steps": 30},
                                                         "treble": {"from": 0, "to": 1, "steps": 20}}}])
    with pytest.raises(QcsuiteError, match="600 通り"):
        sweep.create(rig, spec)
    job, info = sweep.create(rig, spec, allow_large=True)
    assert info["count"] == 600 and info["estimate_s"] == pytest.approx(600 * 0.3)
    assert sweep.estimate_seconds(430, 12.0, mock=False) == pytest.approx(430 * 16.0)


def test_sensitivity_map(rig, tmp_path):
    rig.device.read_stored("/media/p4/Presets/kami", 4)
    spec = sweep.sensitivity_spec(0, 1, "gain", steps=4, base="current", di="short")
    job, _ = sweep.create(rig, spec)
    sweep.run(rig, job, base_grid=json.loads((job.dir / "base_grid.json").read_text()))
    data = sweep.sensitivity(job)
    assert data["param"] == "DRIVE" and [p["value"] for p in data["points"]] == [0.0, 0.3333, 0.6667, 1.0]
    flat = [p["flatness"] for p in data["points"]]
    crest = [p["crest"] for p in data["points"]]
    assert flat[-1] > flat[0] and crest[-1] < crest[0]
    png = sweep.sensitivity_png(job, tmp_path / "s.png")
    assert png.stat().st_size > 5000


def test_send_to_tournament(rig):
    from qcsuite.features import tournament
    if not hasattr(tournament, "create"):
        pytest.skip("tournament.create はまだ実装されていない(別フォークが作業中)")
    # Driven settings: on the fake, very clean ones sit near the passthrough threshold.
    job, _ = _run(rig, _spec(cells=[{"row": 0, "col": 1, "params": {"gain": [0.6, 0.8, 0.95]}}]))
    result = sweep.to_tournament(job, True, top=3)
    assert len(result["sent"]) == 3 and result["tournament"]["id"]
    with pytest.raises(QcsuiteError, match="2 つ以上"):
        sweep.to_tournament(job, True, keys=["c0000"])


# -- CLI and web ------------------------------------------------------------------------------

def test_cli_preview_run_results_sensitivity(tmp_path, capsys):
    from qcsuite.cli import main
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec()))
    main(["--mock", "--json", "sweep", "preview", str(spec)])
    assert json.loads(capsys.readouterr().out)["count"] == 2
    main(["--mock", "--yes", "--json", "sweep", "run", str(spec)])
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "done" and summary["ok"] == 2
    main(["--mock", "--json", "sweep", "results", summary["id"], "--sort", "crest"])
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert len(rows) == 2 and rows[0]["crest"] <= rows[1]["crest"]
    main(["--mock", "--yes", "--json", "sweep", "sensitivity", "--row", "1", "--col", "2",
          "--param", "treble", "--steps", "3", "--base", "kami:1E", "--di", "short"])
    data = json.loads(capsys.readouterr().out)
    assert data["param"] == "TREBLE" and len(data["points"]) == 3


def test_web_flow(client):
    spec = _spec()
    pre = client.post("/api/sweep/preview", json={"spec": spec}).json()
    assert pre["count"] == 2 and not pre["large"]
    r = client.post("/api/sweep/run", json={"spec": spec}).json()
    detail = wait_job(client, r["job"])
    assert detail["summary"]["status"] == "done", detail
    jobs_list = client.get("/api/sweep/jobs").json()
    assert any(j["id"] == r["job"] for j in jobs_list)
    res = client.get(f"/api/sweep/{r['job']}/results", params={"sort": "lufs"}).json()
    assert len(res["rows"]) == 2
    file = res["rows"][0]["file"]
    assert client.get("/api/files", params={"path": file}).status_code == 200
    s = client.post("/api/sweep/sensitivity", json={"row": 0, "col": 1, "param": "gain",
                                                     "steps": 3, "base": KAMI_BRIT, "di": "short"}).json()
    wait_job(client, s["job"])
    data = client.get(f"/api/sweep/{s['job']}/sensitivity").json()
    assert len(data["points"]) == 3
    bad = client.post("/api/sweep/preview", json={"spec": {"cells": []}})
    assert bad.status_code == 400 and "cells" in bad.json()["error"]
