"""Real-gear photos for the editor (qcsuite/features/photos.py).

Wikimedia Commons is never contacted: every test hands in fake fetchers (and
conftest sets QCSUITE_PHOTOS_OFFLINE=1 for everything else). The device list is
a tiny hand-written table in the same shape as the player's Markdown file.
"""

import base64
import json
import time

import pytest

from qcsuite.features import photos

LIST_MD = """# test list

## Guitar amps（モデリング）

| QC名 | 元ネタ | 追加 | 旧名 |
|---|---|---|---|
| Brit 2203 | Marshall JCM800 | 1.0.0 | British 2203 |

## Neural Captures V2（実機キャプチャー）

| QC名 | 元ネタ | カテゴリ | 追加 |
|---|---|---|---|
| Brit 2203 87 | Marshall JCM800 1987 | Guitar amp | 3.3.0 |

## Guitar cabinets

| QC名 | 元ネタ | 追加 | 旧名 |
|---|---|---|---|
| 412 Brit 60B GB '71 | Marshall 1960B（ストレート）/ Celestion Pulsonic Greenback | 1.0.0 | |

## Compressor

| QC名 | 元ネタ | 追加 | 旧名 |
|---|---|---|---|
| Opto Comp / (ST) / (S/C) | 汎用オプト | 1.0.0 | |
| 4-Comp Custom | Keeley 4 Knob 系の改造品 | 1.0.0 | |

## Delay（すべて汎用。特定機種ベースのものは無い）

| QC名 | 追加 | 旧名 |
|---|---|---|
| Analog Delay | 1.3.0 | |

## Synth / Looper / IR loader / Utility

| QC名 | カテゴリ | 元ネタ・備考 | 追加 |
|---|---|---|---|
| Phase Doctor | Utility | Little Labs IBP に着想 | 4.0.0 |

## Announced（発表済み・未リリース。現時点では使えない）

| カテゴリ | QC名（予定） | 元ネタ |
|---|---|---|
| Compressor | Dynamic Comp | MXR Dynacomp |
"""

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64


def commons_answer(*files):
    """A Commons API answer: (title, licence, description) per hit, in rank order."""
    pages = {}
    for i, (title, licence, desc) in enumerate(files):
        meta = {"ObjectName": {"value": title}, "ImageDescription": {"value": desc},
                "Artist": {"value": '<a href="//commons.wikimedia.org/wiki/User:X">Photographer X</a>'},
                "Categories": {"value": "Guitar amplifiers"}}
        if licence:
            meta["LicenseShortName"] = {"value": licence}
            meta["LicenseUrl"] = {"value": "https://creativecommons.org/licenses/by-sa/4.0"}
        pages[str(100 + i)] = {"index": i + 1, "title": f"File:{title}.jpg", "imageinfo": [{
            "mime": "image/jpeg", "thumburl": f"https://upload.wikimedia.org/thumb/{i}.jpg",
            "url": f"https://upload.wikimedia.org/{i}.jpg",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{i}.jpg",
            "extmetadata": meta}]}
    return {"query": {"pages": pages}}


class FakeCommons:
    def __init__(self, answer, image=JPEG):
        self.answer, self.image, self.urls, self.images = answer, image, [], []

    def json(self, url):
        self.urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def bytes(self, url):
        self.images.append(url)
        return self.image

    @property
    def kw(self):
        return {"fetch_json": self.json, "fetch_bytes": self.bytes}


@pytest.fixture
def device_list(tmp_path, monkeypatch):
    path = tmp_path / "list.md"
    path.write_text(LIST_MD, encoding="utf-8")
    monkeypatch.setenv("QCSUITE_DEVICE_LIST", str(path))
    return path


@pytest.fixture
def online(monkeypatch):
    monkeypatch.delenv("QCSUITE_PHOTOS_OFFLINE", raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(url):
        raise AssertionError(f"a test tried to reach {url}")
    monkeypatch.setattr(photos, "_get_json", refuse)
    monkeypatch.setattr(photos, "_get_bytes", refuse)


# -- origin ------------------------------------------------------------------------------

def test_clean_origin_and_query():
    assert photos.clean_origin("Based on Klon® Centaur®") == "Klon Centaur"
    assert photos.search_query("Marshall JTM 45 - Normal Channel") == "Marshall JTM 45"
    assert photos.search_query("Fender Princeton / Jensen C10R") == "Fender Princeton"
    assert photos.search_query("Marshall 1960B（ストレート）/ Celestion Greenback") == "Marshall 1960B"
    assert photos.search_query("Ampeg SVT810 with 90s Eminence Speakers") == "Ampeg SVT810"
    assert photos.search_query("Marshall JCM800 1987") == "Marshall JCM800"


def test_origin_from_the_modelrepo_wins(device_list):
    o = photos.origin_for("Brit 2203", "Based on Marshall® JCM800® Head")
    assert o["kind"] == "hardware" and o["source"] == "modelrepo"
    assert o["origin"] == "Marshall JCM800 Head"
    assert o["links"]["images"].startswith("https://www.google.com/search?tbm=isch&q=Marshall+JCM800")
    assert o["links"]["reverb"].startswith("https://reverb.com/marketplace?query=")


def test_origin_from_the_device_list(device_list):
    assert photos.origin_for("Brit 2203")["origin"] == "Marshall JCM800"
    assert photos.origin_for("British 2203")["origin"] == "Marshall JCM800"      # old name
    cap = photos.origin_for("Brit 2203 87")                                       # a capture
    assert cap["origin"] == "Marshall JCM800 1987" and cap["query"] == "Marshall JCM800"
    cab = photos.origin_for("412 Brit 60B GB 71 M")                               # "'71", "M"
    assert cab["source"] == "device_list" and cab["query"] == "Marshall 1960B"


def test_originals_plugins_and_unknowns(device_list):
    assert photos.origin_for("Analog Delay M")["kind"] == "original"
    opto = photos.origin_for("Opto Comp M")
    assert opto["kind"] == "original" and opto["note"] == "汎用オプト"       # generic, not gear
    assert photos.origin_for("4-Comp Custom")["query"] == "Keeley 4 Knob"
    doc = photos.origin_for("Phase Doctor")
    assert doc["kind"] == "original" and "着想" in doc["note"]      # inspired by is not "based on"
    assert photos.origin_for("Dynamic Comp")["kind"] == "unknown"   # announced: skipped
    assert photos.origin_for("Plini Clean", "Plini plugin", plugin=True)["kind"] == "plugin"
    assert photos.origin_for("Nothing Like It")["kind"] == "unknown"
    assert photos.origin_for("Nothing Like It")["links"] is None


def test_device_list_path_from_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("QCSUITE_DEVICE_LIST")
    other = tmp_path / "elsewhere.md"
    other.write_text(LIST_MD, encoding="utf-8")
    photos.save_setting("device_list", str(other))
    assert photos.device_list_path() == other
    assert photos.origin_for("Brit 2203")["origin"] == "Marshall JCM800"
    photos.save_setting("device_list", None)
    assert photos.device_list_path() == photos.DEFAULT_DEVICE_LIST


def test_no_device_list_is_fine(monkeypatch, tmp_path):
    monkeypatch.setenv("QCSUITE_DEVICE_LIST", str(tmp_path / "missing.md"))
    assert photos.origin_for("Brit 2203")["kind"] == "unknown"
    assert photos.origin_for("Myth Drive", "Based on Klon® Centaur®")["origin"] == "Klon Centaur"


# -- matching ----------------------------------------------------------------------------

@pytest.mark.parametrize("query,hay,ok", [
    ("Marshall JCM800", "Marshall JCM 800 amplifier head", True),
    ("Marshall JCM800", "Marshall JCM900 amplifier", False),        # wrong model number
    ("Marshall JCM800", "JCM800 sticker", False),                    # no brand
    ("Klon Centaur", "Klon Centaur overdrive pedal", True),
    ("Klon Centaur", "Klon KTR", False),
    ("Fulltone OCD", "Fulltone OCD v2", True),
    ("Mesa Boogie Dual Rectifier", "Mesa/Boogie Dual Rectifier Solo Head", True),
    ("Mesa Boogie Dual Rectifier", "Mesa/Boogie Mark IV", False),
    # wrong matches seen on the real catalog with a looser rule (2026-11-10)
    ("Zilla Open", "Zilla Parishad High Addakal Open Area", False),
    ("Universal Audio 1176", "Universal Audio (AES 124)", False),
    ("Gibson GA-8", "Reverend Obediah C. Gibson House, Griffin, Spalding County, GA HABS GA,128-GRIF,4-1", False),
    ("Two Rock Open Back", "Two Mule Deer Grazing - Ridgeline Open Space - Castle Rock, CO", False),
    ("Marshall", "Marshall JCM800", False),                          # one word is too vague
    ("DOD Overdrive Preamp 250", "2013 DOD 250 Overdrive Preamp and 201 Phasor", True),
    ("Tech21 Bass Driver DI", "Tech 21 SansAmp Bass Driver DI", True),
])
def test_good_match(query, hay, ok):
    assert photos.good_match(query, hay) is ok


def test_description_alone_does_not_make_a_match():
    fake = FakeCommons(commons_answer(("Pedalboard at a show", "CC0", "a Fulltone OCD on the left")))
    assert photos.commons_search("Fulltone OCD", fake.json) is None


def test_a_cached_photo_from_a_looser_rule_is_searched_again(device_list, online):
    bad = FakeCommons(commons_answer(("Marshall JCM800 head", "CC0", "")))
    photos.lookup("Brit 2203", **bad.kw)
    meta = next(photos.cache_dir().glob("*.json"))
    rec = json.loads(meta.read_text())
    rec["title"], rec["object"] = "Some school.jpg", "Some school"
    meta.write_text(json.dumps(rec))
    none = FakeCommons(commons_answer())
    r = photos.lookup("Brit 2203", **none.kw)
    assert len(none.urls) == 1 and r["photo"] is None
    assert not list(photos.cache_dir().glob("*.jpg"))         # the wrong photo is gone


def test_commons_search_takes_the_first_good_free_photo():
    fake = FakeCommons(commons_answer(
        ("Marshall stack at a gig", "CC BY-SA 4.0", "a wall of amps"),       # no model number
        ("Marshall JCM800 2203", "", "non-free: no licence"),                # no licence
        ("Marshall JCM800 2203 head", "CC BY-SA 4.0", "front panel")))
    hit = photos.commons_search("Marshall JCM800", fake.json)
    assert hit["title"] == "Marshall JCM800 2203 head.jpg"
    assert hit["author"] == "Photographer X" and hit["license"] == "CC BY-SA 4.0"
    assert "Marshall+JCM800" in fake.urls[0] and "filetype%3Abitmap" in fake.urls[0]


# -- lookup and cache -----------------------------------------------------------------------

def test_lookup_caches_a_commons_photo_with_credit(device_list, online):
    fake = FakeCommons(commons_answer(("Marshall JCM800 head", "CC BY-SA 4.0", "")))
    r = photos.lookup("Brit 2203", **fake.kw)
    assert r["photo"]["source"] == "commons"
    assert r["photo"]["author"] == "Photographer X" and r["photo"]["license"] == "CC BY-SA 4.0"
    assert r["photo"]["page"].startswith("https://commons.wikimedia.org/")
    assert r["photo"]["url"].startswith("/api/editor/photo/file/cache/")
    again = photos.lookup("Brit 2203", **fake.kw)
    assert again["photo"] == r["photo"] and len(fake.urls) == 1          # cached
    assert len(list(photos.cache_dir().glob("*.jpg"))) == 1


def test_only_the_gear_name_is_sent(device_list, online):
    fake = FakeCommons(commons_answer())
    photos.lookup("Brit 2203", **fake.kw)
    sent = fake.urls[0]
    assert "Marshall+JCM800" in sent and "Brit" not in sent


def test_not_found_is_remembered_then_retried(device_list, online, monkeypatch):
    fake = FakeCommons(commons_answer())
    assert photos.lookup("Brit 2203", **fake.kw)["commons"] == "none"
    photos.lookup("Brit 2203", **fake.kw)
    assert len(fake.urls) == 1
    later = time.time() + (photos.NEGATIVE_DAYS + 1) * 86400
    monkeypatch.setattr(photos.time, "time", lambda: later)
    photos.lookup("Brit 2203", **fake.kw)
    assert len(fake.urls) == 2


def test_network_failure_is_not_cached(device_list, online):
    down = FakeCommons(OSError("offline"))
    assert photos.lookup("Brit 2203", **down.kw)["commons"] == "error"
    up = FakeCommons(commons_answer(("Marshall JCM800", "CC0", "")))
    assert photos.lookup("Brit 2203", **up.kw)["photo"]["source"] == "commons"


def test_fetch_off_answers_from_the_cache_only(device_list, online):
    fake = FakeCommons(commons_answer(("Marshall JCM800", "CC0", "")))
    assert photos.lookup("Brit 2203", fetch=False, **fake.kw)["commons"] == "pending"
    assert fake.urls == []


def test_offline_setting_never_searches(device_list):
    fake = FakeCommons(commons_answer(("Marshall JCM800", "CC0", "")))
    r = photos.lookup("Brit 2203", **fake.kw)
    assert r["photo"] is None and r["commons"] == "off" and fake.urls == []
    assert r["links"]["images"]                                   # the buttons still work


def test_not_an_image_is_not_kept(device_list, online):
    fake = FakeCommons(commons_answer(("Marshall JCM800", "CC0", "")), image=b"<html>")
    assert photos.lookup("Brit 2203", **fake.kw)["photo"] is None


def test_originals_are_not_searched(device_list, online):
    fake = FakeCommons(commons_answer())
    r = photos.lookup("Analog Delay")
    assert r["kind"] == "original" and r["photo"] is None and fake.urls == []


# -- the player's own photos ---------------------------------------------------------------

def test_user_photo_wins_over_commons(device_list, online):
    (photos.photos_dir() / "marshall jcm800.PNG").write_bytes(PNG)
    fake = FakeCommons(commons_answer(("Marshall JCM800", "CC0", "")))
    r = photos.lookup("Brit 2203", **fake.kw)
    assert r["photo"] == {"source": "user", "url": "/api/editor/photo/file/user/marshall%20jcm800.PNG",
                          "file": "marshall jcm800.PNG"}
    assert fake.urls == []


def test_user_photo_for_an_original(device_list):
    (photos.photos_dir() / "Analog-Delay.jpg").write_bytes(JPEG)
    assert photos.lookup("Analog Delay")["photo"]["source"] == "user"


def data_url(raw, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def test_save_user_photo_keeps_the_old_one():
    first = photos.save_user_photo("Brit 2203", data_url(PNG))
    assert first.name == "Brit 2203.png"
    second = photos.save_user_photo("Brit 2203", data_url(JPEG, "image/jpeg"))
    assert second.name == "Brit 2203.jpg" and not first.exists()
    assert len(list((photos.photos_dir() / "old").iterdir())) == 1


@pytest.mark.parametrize("data", ["", "not a data url", "data:image/png;base64,!!!",
                                  "data:image/png;base64," + base64.b64encode(b"<svg/>").decode()])
def test_save_user_photo_refuses_non_images(data):
    with pytest.raises(photos.PhotoError):
        photos.save_user_photo("Brit 2203", data)


def test_save_user_photo_names_stay_inside_the_folder():
    path = photos.save_user_photo("../../etc/passwd", data_url(PNG))
    assert path.parent == photos.photos_dir()


@pytest.mark.parametrize("kind,name", [("user", "../settings.json"), ("user", ".hidden.png"),
                                       ("cache", "x.json"), ("other", "a.png"), ("user", "missing.png")])
def test_photo_file_refuses_anything_else(kind, name):
    with pytest.raises(FileNotFoundError):
        photos.photo_file(kind, name)


# -- web routes ----------------------------------------------------------------------------

def test_photo_route_and_file(client, device_list):
    (photos.photos_dir() / "Marshall JCM800.png").write_bytes(PNG)
    r = client.get("/api/editor/photo", params={"name": "Brit 2203"})
    assert r.status_code == 200
    body = r.json()
    assert body["origin"] == "Marshall JCM800" and body["photo"]["source"] == "user"
    img = client.get(body["photo"]["url"])
    assert img.status_code == 200 and img.content == PNG
    assert client.get("/api/editor/photo/file/user/..%2Fsettings.json").status_code == 404


def test_photo_upload_route(client):
    r = client.post("/api/editor/photo", json={"name": "Myth Drive", "data": data_url(PNG)})
    assert r.status_code == 200 and r.json()["file"] == "Myth Drive.png"
    bad = client.post("/api/editor/photo", json={"name": "Myth Drive", "data": "data:,x"})
    assert bad.status_code == 400


def test_photo_routes_need_the_session(fake):
    from fastapi.testclient import TestClient
    from qcsuite.web.app import create_app
    with TestClient(create_app(mock=True, fake=fake)) as c:
        assert c.get("/api/editor/photo", params={"name": "x"}).status_code == 403


def test_photo_routes_do_not_touch_the_unit(client, device_list, monkeypatch):
    def boom():
        raise AssertionError("the photo route asked for the unit")
    monkeypatch.setattr(client.app.state.qc, "device", boom)
    r = client.get("/api/editor/photo", params={"name": "Analog Delay"})
    assert r.status_code == 200 and r.json()["kind"] == "original"
    up = client.post("/api/editor/photo", json={"name": "Analog Delay", "data": data_url(PNG)})
    assert up.status_code == 200


def test_editor_page_loads_the_photo_script(client):
    page = client.get("/static/editor.html").text
    assert "editor_photos.js" in page
    js = client.get("/static/editor_photos.js").text
    # Google / Reverb are links for the browser; the page never fetches their images.
    assert "google.com" not in js and "reverb.com" not in js


def test_cli_photos_show(capsys, device_list):
    from qcsuite import cli
    try:
        cli.main(["photos", "show", "Brit 2203", "--no-fetch"])
    except SystemExit as exc:
        assert not exc.code
    out = capsys.readouterr().out
    assert "Marshall JCM800" in out


# -- Wikimedia's rate limit ---------------------------------------------------------------

class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body


def test_requests_are_spaced_and_name_no_one(monkeypatch):
    import urllib.request
    seen, sleeps = [], []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: seen.append(req) or FakeResponse(b"{}"))
    monkeypatch.setattr(photos.time, "sleep", sleeps.append)
    photos._request("https://commons.wikimedia.org/w/api.php?a=1")
    photos._request("https://commons.wikimedia.org/w/api.php?a=2")
    assert len(seen) == 2 and sleeps and 0 < sleeps[0] <= photos.INTERVAL_ANON
    ua = seen[0].get_header("User-agent")
    assert ua.startswith("qcsuite-photo-bot/") and "@" not in ua


def test_contact_is_sent_only_when_the_player_sets_one(monkeypatch):
    photos.save_setting("commons_contact", "me@example.org")
    assert "me@example.org" in photos.user_agent()
    photos.save_setting("commons_contact", None)
    assert "example.org" not in photos.user_agent()


def test_429_starts_a_cooldown(monkeypatch, device_list, online):
    import io
    import urllib.error
    import urllib.request
    calls = []

    def too_many(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "120"}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", too_many)
    monkeypatch.setattr(photos, "_get_json", lambda url: json.loads(photos._request(url)))
    monkeypatch.setattr(photos.time, "sleep", lambda s: None)
    first = photos.lookup("Brit 2203")
    assert first["photo"] is None and len(calls) == 1
    assert 100 < photos.cooldown_left() <= 120
    again = photos.lookup("Brit 2203")
    assert again["commons"] == "limited" and len(calls) == 1      # did not knock again
