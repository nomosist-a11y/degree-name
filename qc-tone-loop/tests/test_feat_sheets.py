"""Preset sheets (HTML / PDF), scene maps and share packs on the fake unit."""

import json
import zipfile

import numpy as np
import pytest
import soundfile as sf

from qcsuite import presets
from qcsuite.features import sheets
from qcsuite.features import sheets_sources as src

USER = "/media/p4/Presets"


def _norm(device, folder, slot):
    key, position = src.parse_ref(f"{folder}:{slot}")
    return presets.normalize(device.read_stored(key, position), device.catalog,
                             folder=key, position=position)


def _with_scenes(device):
    """The first preset in kami/My Presets that switches blocks per scene."""
    for folder in ("kami", "My Presets"):
        for p in device.presets(f"{USER}/{folder}"):
            n = presets.normalize(device.read_stored(f"{USER}/{folder}", p["index"]), device.catalog,
                                  folder=f"{USER}/{folder}", position=p["index"])
            if sheets.scene_map(n)["scenes_used"]:
                return n
    raise AssertionError("the fake has no preset using scenes")


def test_parse_ref_and_folder_names():
    assert src.parse_ref("kami:1E") == (f"{USER}/kami", 4)
    assert src.parse_ref("My Presets:2A") == (f"{USER}/My Presets", 8)
    assert src.parse_ref("Factory Library:1A") == (src.FACTORY_KEY, 0)
    assert src.parse_ref("kami:7") == (f"{USER}/kami", 7)
    with pytest.raises(ValueError):
        src.parse_ref("kami")
    assert src.ref_text(f"{USER}/kami", 4) == "kami:1E"


def test_html_sheet_has_chain_knobs_and_scene_map(device):
    n = _with_scenes(device)
    doc = sheets.sheet_html([n, _norm(device, "kami", "1E")], "テスト資料")
    assert doc.startswith("<!doctype html>") and "<svg" in doc
    assert "テスト資料" in doc and "シーン早見表" in doc and "Rhythm" in doc
    amp = next(b for b in n["blocks"] if b["category"] == "Guitar Amplifier"
               or b["category"].startswith("Neural Capture"))
    assert amp["model"] in doc
    knob = presets.main_knobs(amp)[0][0]
    assert f"<td>{knob}</td>" in doc
    assert doc.count("<section class='preset'>") == 2
    assert "@page { size: A4" in doc
    m = sheets.scene_map(n)
    assert any(r["kind"] == "bypass" and "—" in r["cells"] and "●" in r["cells"] for r in m["rows"])


def test_html_flags_blocks_that_will_not_follow_host_edits(device):
    aaaa = next(p for p in device.presets(f"{USER}/My Presets") if p["name"] == "aaaa")
    n = presets.normalize(device.read_stored(f"{USER}/My Presets", aaaa["index"]), device.catalog,
                          folder=f"{USER}/My Presets", position=aaaa["index"])
    doc = sheets.sheet_html([n])
    assert "未所有のプラグイン" in doc and "マイク" in doc
    assert 'stroke="#c2392b"' in doc          # the inert block is outlined in red


def test_pdf_is_written_with_a_page_per_preset(device, tmp_path):
    items = [_norm(device, "kami", "1E"), _norm(device, "Factory Library", "1C")]
    info = sheets.sheet_pdf(items, tmp_path / "out.pdf")
    data = (tmp_path / "out.pdf").read_bytes()
    assert data.startswith(b"%PDF") and b"%%EOF" in data[-64:] and len(data) > 5000
    assert info["pages"] >= 2
    assert info["font"] in sheets.JP_FONTS + ["DejaVu Sans"]


def test_share_pack_contents(rig, device, tmp_path):
    items = [_norm(device, "kami", "1E")]
    wav = tmp_path / "take.wav"
    sf.write(str(wav), np.zeros((4800, 2), np.float32), 48000)
    renders = sheets.render_for_pack(rig, [src.parse_ref("My Presets:1E"), src.parse_ref("My Presets:2H")],
                                     "synthetic-riff", True, tmp_path / "renders")
    # 1E is a high-gain preset; 2H is "aaaa", which holds an unowned Plini block.
    assert [r["excluded"] for r in renders] == [False, True]
    info = sheets.share_pack(items, tmp_path / "pack.zip", pdf=True, audio_files=[str(wav)],
                             renders=renders)
    with zipfile.ZipFile(info["file"]) as z:
        names = z.namelist()
        readme = z.read("README.txt").decode()
        manifest = json.loads(z.read("manifest.json"))
        preset_json = json.loads(z.read(next(n for n in names if n.startswith("presets/"))))
    assert {"sheet.html", "sheet.pdf", "README.txt", "manifest.json"} <= set(names)
    assert sum(n.startswith("audio/") for n in names) == 3
    assert "直接読み込めません" in readme and "本体の画面で" in readme
    assert preset_json["schema"] == presets.SCHEMA
    assert any(a.get("excluded") for a in manifest["audio"])


def test_cli_html_pdf_and_pack(tmp_path, capsys):
    from qcsuite.cli import main
    main(["--mock", "--yes", "--json", "sheets", "html", "--preset", "kami:1E",
          "--preset", "My Presets:1A", "--out", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert out["presets"] == 2 and "<svg" in open(out["file"]).read()
    main(["--mock", "--yes", "--json", "sheets", "pdf", "--current", "--out", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["pages"] >= 1
    main(["--mock", "--yes", "--json", "sheets", "pack", "--preset", "kami:1E", "--pdf",
          "--out", str(tmp_path)])
    with zipfile.ZipFile(json.loads(capsys.readouterr().out)["file"]) as z:
        assert "sheet.pdf" in z.namelist()
    with pytest.raises(SystemExit, match="対象のプリセットがありません"):
        main(["--mock", "sheets", "html", "--from-backup", "--out", str(tmp_path)])


def test_cli_asks_before_recalling(tmp_path):
    from qcsuite.cli import main
    with pytest.raises(SystemExit, match="中止"):
        main(["--mock", "sheets", "html", "--preset", "kami:1E", "--out", str(tmp_path)])


def test_from_backup_needs_no_recall(device, monkeypatch, fake):
    n = _norm(device, "kami", "1E")
    monkeypatch.setattr(src, "backup_presets", lambda mock: [n])
    before = fake.audio_interruptions
    items = src.collect(device, mock=True, refs=[src.parse_ref("kami:1E")])
    assert items[0]["fingerprint"] == n["fingerprint"]
    assert fake.audio_interruptions == before
    assert src.needs_recall([src.parse_ref("kami:1E"), src.parse_ref("kami:1A")], True) == 1


def test_web_flow(client):
    client.post("/api/connect")
    assert client.post("/api/sheets/recalls", json={"refs": ["kami:1E"]}).json()["recalls"] == 1
    r = client.post("/api/sheets/html", json={"refs": ["kami:1E"], "title": "web"}).json()
    page = client.get("/api/files", params={"path": r["file"]})
    assert page.status_code == 200 and "<svg" in page.text
    pdf = client.post("/api/sheets/pdf", json={"current": True}).json()
    assert client.get("/api/files", params={"path": pdf["file"]}).content.startswith(b"%PDF")
    pack = client.post("/api/sheets/pack", json={"refs": ["kami:1E"], "pdf": False}).json()
    assert pack["file"].endswith(".zip")
    missing = client.post("/api/sheets/html", json={"from_backup": True, "refs": ["kami:1E"]})
    assert missing.status_code == 404
    assert "folders" in client.get("/api/sheets/sources").json()
