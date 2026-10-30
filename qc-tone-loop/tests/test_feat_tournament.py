"""Blind comparison: matching, hiding, pairing, Elo, reveal, presets, web, CLI."""

import json

import numpy as np
import pytest
import soundfile as sf

from helpers import wait_job
from qcsuite import paths
from qcsuite.audio import di as di_mod
from qcsuite.audio import lufs
from qcsuite.errors import QcsuiteError
from qcsuite.features import tournament as T

SR = 48000
USER = "/media/p4/Presets"


@pytest.fixture
def files(tmp_path, di):
    """Six synthetic 'renders' of the DI at different levels and tones."""
    out = []
    piece = di[: int(SR * 1.5)]
    for i in range(6):
        x = np.tanh(piece * (1 + 3 * i)) * (0.1 + 0.15 * i)
        path = tmp_path / f"take{i}.wav"
        sf.write(str(path), np.stack([x, x], axis=1), SR)
        out.append({"id": f"c{i}", "label": f"Secret Name {i}", "file": str(path),
                    "meta": {"i": i}})
    return out


def _play(tid, chooser=lambda p: "1", limit=200):
    n = 0
    while n < limit:
        p = T.next_pair(tid, True)
        if p["done"]:
            return n
        T.vote(tid, True, chooser(p), p["pair_id"])
        n += 1
    raise AssertionError("tournament did not finish")


def test_create_matches_loudness_and_hides_names(files):
    t = T.create(files[:3], mock=True, title="t", source="files")
    blob = json.dumps(t, ensure_ascii=False)
    assert "Secret Name" not in blob and "take" not in blob
    levels = []
    for c in t["candidates"]:
        data, sr = sf.read(str(paths.home(True) / c["audio"]))
        levels.append(lufs.integrated_loudness(data, sr))
    assert max(levels) - min(levels) < 0.1
    assert abs(levels[0] - t["target_lufs"]) < 0.1
    assert [c["code"] for c in t["candidates"]] == ["候補 A", "候補 B", "候補 C"]
    with pytest.raises(ValueError):
        T.create(files[:1], mock=True, title="t", source="files")


def test_elo_math():
    assert T.expected(1500, 1500) == 0.5
    table = T.elo([{"a": "x", "b": "y", "choice": "a"}], ["x", "y"], k=32)
    assert table["x"]["rating"] == 1516.0 and table["y"]["rating"] == 1484.0
    tie = T.elo([{"a": "x", "b": "y", "choice": "tie"}], ["x", "y"])
    assert tie["x"]["rating"] == tie["y"]["rating"] == 1500.0 and tie["x"]["ties"] == 1
    check = T.elo([{"a": "x", "b": "x", "choice": "a", "check": True}], ["x", "y"])
    assert check["x"]["games"] == 0


def test_swiss_pairs_never_repeat(files):
    t = T.create(files, mock=True, title="swiss", source="files", seed=1)
    votes = _play(t["id"], chooser=lambda p: ["1", "2", "0"][p["pair_id"] % 3])
    folder = T.root(True) / t["id"]
    recorded = [json.loads(line) for line in (folder / "votes.jsonl").read_text().splitlines()]
    pairs = [frozenset((v["a"], v["b"])) for v in recorded]
    assert len(pairs) == len(set(pairs)) == votes
    assert all(len(p) == 2 for p in pairs)
    rows = T.standings(t["id"], True)
    assert sum(r["games"] for r in rows) == 2 * votes
    assert any(r["ties"] for r in rows)
    assert T.next_pair(t["id"], True)["done"]


def test_knockout_refuses_ties_and_leaves_one(files):
    t = T.create(files[:5], mock=True, title="ko", source="files", format="knockout")
    p = T.next_pair(t["id"], True)
    assert not p["allow_tie"]
    with pytest.raises(QcsuiteError, match="どちらか"):
        T.vote(t["id"], True, "0", p["pair_id"])
    assert _play(t["id"]) == 4                      # n - 1 games
    alive = [r for r in T.standings(t["id"], True) if r["alive"]]
    assert len(alive) == 1


def test_stale_pair_is_refused(files):
    t = T.create(files[:2], mock=True, title="t", source="files")
    p = T.next_pair(t["id"], True)
    assert T.next_pair(t["id"], True)["pair_id"] == p["pair_id"]     # same pair until voted
    with pytest.raises(QcsuiteError):
        T.vote(t["id"], True, "1", p["pair_id"] + 5)
    T.vote(t["id"], True, "2", p["pair_id"])
    with pytest.raises(QcsuiteError):
        T.vote(t["id"], True, "1")


def test_reveal_and_export(files):
    t = T.create(files[:3], mock=True, title="reveal me", source="files")
    _play(t["id"])
    hidden = T.export_results(t["id"], True)
    assert "Secret Name" not in hidden and "伏せています" in hidden
    v = T.reveal(t["id"], True)
    assert v["revealed"] and {r["label"] for r in v["reveal"]} == {f"Secret Name {i}" for i in range(3)}
    md = T.export_results(t["id"], True)
    assert "Secret Name" in md and "あなたの投票の集計" in md
    assert json.loads(T.export_results(t["id"], True, "json"))["revealed"]


def test_hidden_consistency_check(files):
    t = T.create(files[:4], mock=True, title="check", source="files", consistency_checks=1)
    seen_check = []

    def chooser(p):
        if p["a"]["audio"] != p["b"]["audio"]:
            folder = T.root(True) / t["id"]
            state = json.loads((folder / "state.json").read_text())
            if state["pending"]["check"]:
                seen_check.append(p)
                a = sf.read(p["a"]["path"])[0]
                b = sf.read(p["b"]["path"])[0]
                assert np.array_equal(a, b)          # same audio, different files
                return "0"
        return "1"
    _play(t["id"], chooser)
    assert len(seen_check) == 1
    v = T.reveal(t["id"], True)
    assert v["consistency"] == {"checks": 1, "answered_same": 1, "share": 1.0}


def test_create_from_presets_excludes_what_does_not_sound(rig, di):
    di_mod.save("short", di[: int(SR * 1.5)], SR, True, source="test")
    aaaa = next(p["index"] for p in rig.device.presets(f"{USER}/My Presets") if p["name"] == "aaaa")
    presets = [{"folder": f"{USER}/My Presets", "position": 0},
               {"folder": f"{USER}/Cory Wong", "position": 11},
               {"folder": f"{USER}/My Presets", "position": aaaa}]
    v = T.create_from_presets(rig, presets, "short", title="presets")
    assert len(v["candidates"]) == 2
    assert [e["label"].endswith("aaaa") for e in v["excluded"]] == [True]
    assert "Plini" in " ".join(v["excluded"][0]["reasons"]) or "plini" in " ".join(v["excluded"][0]["reasons"])
    with pytest.raises(QcsuiteError, match="2 つ未満"):
        T.create_from_presets(rig, presets[2:], "short")


def test_web_flow(client, files, di):
    client.post("/api/connect")
    t = T.create(files[:3], mock=True, title="web", source="files")
    assert client.get("/api/tournament").json()[0]["id"] == t["id"]
    p = client.get(f"/api/tournament/{t['id']}/next").json()
    assert client.get("/api/files", params={"path": p["a"]["audio"]}).status_code == 200
    r = client.post(f"/api/tournament/{t['id']}/vote", json={"choice": "1", "pair_id": p["pair_id"]})
    assert r.json()["recorded"]
    view = client.get(f"/api/tournament/{t['id']}").json()
    assert "label" not in json.dumps(view["standings"])
    revealed = client.post(f"/api/tournament/{t['id']}/reveal").json()
    assert revealed["revealed"]
    assert "Secret Name" in client.get(f"/api/tournament/{t['id']}/export").text
    outside = client.post("/api/tournament/files", json={"candidates": files[:2], "title": "x"})
    assert outside.status_code == 400

    di_mod.save("short", di[: int(SR * 1.5)], SR, True, source="test")
    job = client.post("/api/tournament/presets", json={
        "presets": [{"folder": f"{USER}/My Presets", "position": 0},
                    {"folder": f"{USER}/Favorite", "position": 20}], "di": "short"}).json()["job"]
    wait_job(client, job)
    for _ in range(100):
        r = client.get(f"/api/tournament/by-job/{job}")
        if r.status_code == 200:
            break
    assert r.status_code == 200 and len(r.json()["candidates"]) == 2
    assert client.get("/static/tournament.html").status_code == 200


def test_cli_flow(files, capsys):
    from qcsuite.cli import main
    main(["--mock", "--json", "tournament", "new", files[0]["file"], files[1]["file"],
          "--label", "One", "--label", "Two", "--title", "cli"])
    tid = json.loads(capsys.readouterr().out)["id"]
    main(["--mock", "tournament", "next", tid])
    assert "A:" in capsys.readouterr().out
    main(["--mock", "tournament", "vote", tid, "2"])
    main(["--mock", "tournament", "standings", tid])
    out = capsys.readouterr().out
    assert "One" not in out and "候補" in out
    main(["--mock", "tournament", "reveal", tid])
    out = capsys.readouterr().out
    assert "One" in out and "Two" in out
    main(["--mock", "tournament", "list"])
    assert tid in capsys.readouterr().out
