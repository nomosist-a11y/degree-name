"""Presets JSON, jobs, the CLI core and the web core."""

import json
import threading

import pytest

from qcsuite import jobs, presets
from qcsuite import plan as plan_mod
from qcsuite.device import fake_data

from helpers import wait_job

USER = "/media/p4/Presets"


# -- presets ---------------------------------------------------------------------

def test_identical_presets_share_a_fingerprint(device):
    catalog = device.catalog
    def norm(folder, name):
        pos = next(p["index"] for p in device.presets(f"{USER}/{folder}") if p["name"] == name)
        return presets.normalize(device.read_stored(f"{USER}/{folder}", pos), catalog,
                                 folder=f"{USER}/{folder}", position=pos)
    a = norm("My Presets", "ZUTOMAYO_M&M")
    b = norm("Favorite", "ZUTOMAYO_M&M")
    c = norm("kami", "Lead")
    d = norm("Favorite", "Lead")
    assert a["fingerprint"] == b["fingerprint"]
    assert c["fingerprint"] != d["fingerprint"]
    assert a["slot"] == "1H" and b["folder"].endswith("Favorite")


def test_normalized_json_is_plain_and_stable(device):
    p = device.read_stored(fake_data.FACTORY_KEY, 3)
    n1 = presets.normalize(p, device.catalog)
    n2 = presets.normalize(p, device.catalog)
    assert json.dumps(n1, sort_keys=True) == json.dumps(n2, sort_keys=True)
    assert n1["schema"] == presets.SCHEMA
    assert all("GAIN REDUCTION" not in r["gate"] for r in n1["rows"])
    cab = next(b for b in n1["blocks"] if b["category"].startswith("Cabsim"))
    assert cab["mic"].startswith("NG_")
    assert presets.chain_text(n1)[0].startswith("行1: [INPUT_1] →")


def test_diff_reports_knob_bypass_and_routing_changes(device):
    device.read_stored(f"{USER}/kami", 4)
    before = presets.normalize(device.current_preset(), device.catalog)
    amp = next(b for b in before["blocks"] if b["category"] == "Guitar Amplifier")
    knob = next(k for k in amp["params"] if k in ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1"))
    device.apply_patch({"ops": [
        {"op": "set_param", "row": amp["row"], "col": amp["col"], "param": knob, "value": 0.99},
        {"op": "bypass", "row": amp["row"], "col": amp["col"], "bypassed": True},
        {"op": "set_input", "row": 0, "port": "USB_5"}]})
    after = presets.normalize(device.current_preset(), device.catalog)
    text = presets.diff_text(presets.diff(before, after))
    assert knob in text and "バイパス" in text and "USB_5" in text
    assert presets.diff_text(presets.diff(before, before)) == "(違いはありません)"


# -- jobs -----------------------------------------------------------------------------

def test_jobs_resume_and_record_failures():
    items = [{"key": f"k{i}", "n": i} for i in range(6)]
    job = jobs.create("test", {"x": 1}, items, mock=True, title="t")
    seen = []

    def work(item):
        seen.append(item["key"])
        if item["n"] == 2:
            raise ValueError("boom")
        if item["n"] == 4 and len(seen) < 6:
            raise KeyboardInterrupt
        return {"double": item["n"] * 2}

    with pytest.raises(KeyboardInterrupt):
        jobs.run(job, work)
    assert job.state()["status"] == "interrupted"
    summary = jobs.run(jobs.open_job(job.id, True), work)
    assert summary["status"] == "done_with_errors"
    # k0, k1, k3 done the first time; the rerun does k2 (fails again), k4, k5.
    assert seen == ["k0", "k1", "k2", "k3", "k4", "k2", "k4", "k5"]
    errors = [r for r in job.results() if not r["ok"]]
    assert errors and "boom" in errors[0]["error"]
    assert job.done_keys() == {"k0", "k1", "k3", "k4", "k5"}


def test_jobs_stop_after_consecutive_failures():
    job = jobs.create("test", {}, [{"key": i} for i in range(10)], mock=True)
    with pytest.raises(jobs.JobStopped):
        jobs.run(job, lambda item: 1 / 0, max_consecutive_failures=3)
    assert job.state()["status"] == "failed" and len(job.results()) == 3


def test_torn_last_line_is_tolerated():
    job = jobs.create("test", {}, [{"key": 1}, {"key": 2}], mock=True)
    job.append({"key": 1, "ok": True, "result": None})
    with open(job.dir / "results.jsonl", "a") as f:
        f.write('{"key": 2, "ok": tr')
    assert job.done_keys() == {1}


def test_job_runner_cancels():
    job = jobs.create("test", {}, [{"key": i} for i in range(50)], mock=True)
    runner = jobs.JobRunner()
    gate = threading.Event()

    def target(job, cancel):
        def work(item):
            if item["key"] == 3:
                gate.set()
                cancel.wait(5)
            return item["key"]
        jobs.run(job, work, cancel=cancel)

    runner.start(job, target)
    gate.wait(5)
    runner.cancel(job.id)
    runner.wait(job.id, 5)
    assert job.state()["status"] == "cancelled"
    assert len(job.done_keys()) < 50


# -- CLI core -------------------------------------------------------------------------------

def test_cli_status_and_mock_persistence(capsys):
    from qcsuite.cli import main
    main(["--mock", "--json", "status"])
    data = json.loads(capsys.readouterr().out)
    names = {f["name"]: f for f in data["folders"]}
    assert names["kami"]["protected"] and names["kami"]["occupied"] == 20
    main(["--mock", "apply", '{"ops":[{"op":"set_input","row":0,"port":"USB_5"}]}'])
    main(["--mock", "--json", "grid"])
    grid = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert grid["rows"][0]["in_port"] == "USB_5"
    main(["--mock", "mock-reset"])
    main(["--mock", "--json", "grid"])
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["rows"][0]["in_port"] == "INPUT_1"


def test_cli_refuses_protected_writes_with_a_clear_message():
    from qcsuite.cli import main
    with pytest.raises(SystemExit, match="パッチを拒否"):
        main(["--mock", "apply", '{"ops":[{"op":"set_input","row":9,"port":"USB_5"}]}'])


# -- web core -------------------------------------------------------------------------------

def test_web_status_connect_and_folders(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "mock" and not s["connected"]
    assert client.post("/api/connect").json()["device"]["firmware"] == "d14e"
    folders = client.get("/api/folders").json()
    assert {f["name"] for f in folders} >= {"My Presets", "kami", "Factory Library"}
    assert client.get("/").status_code == 200
    assert "qcsuite" in client.get("/static/index.html").text


def test_web_policy_toggle_refuses_protected(client):
    client.post("/api/connect")
    r = client.post("/api/policy/allow", json={"folder": "Live", "allowed": True})
    assert f"{USER}/Live" in r.json()["allowed"]
    r = client.post("/api/policy/allow", json={"folder": "kami", "allowed": True})
    assert r.status_code == 400 and "保護" in r.json()["error"]


def test_web_plan_needs_the_confirm_word(client):
    state = client.app.state.qc
    device = state.device()
    key = device.ensure_sandbox()
    plan = plan_mod.Plan("copy", steps=[plan_mod.Step(
        "copy", key, position=0, name="c", source={"folder": f"{USER}/kami", "position": 1})])
    shown = state.plan_response(plan)
    assert shown["confirm_word"] == "実行" and not shown["refused"]
    r = client.post(f"/api/plans/{plan.id}/execute", json={"confirm": "はい"})
    assert r.status_code == 400
    job = client.post(f"/api/plans/{plan.id}/execute", json={"confirm": "実行"}).json()["job"]
    detail = wait_job(client, job)
    assert detail["summary"]["status"] == "done", detail
    assert state.rig().fake.folders[key][0].name == "c"


def test_web_files_stay_inside_the_home(client, qchome):
    (qchome / "mock").mkdir(parents=True, exist_ok=True)
    (qchome / "mock" / "x.txt").write_text("hi")
    assert client.get("/api/files", params={"path": "x.txt"}).text == "hi"
    assert client.get("/api/files", params={"path": "../../etc/passwd"}).status_code == 403
    assert client.get("/api/files", params={"path": "/etc/passwd"}).status_code == 403


def test_web_di_list_has_the_synthetic_riff(client):
    names = [d["name"] for d in client.get("/api/di").json()]
    assert "synthetic-riff" in names
