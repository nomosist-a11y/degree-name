"""Photos of the real gear a Quad Cortex model is based on (エディターの「実機の写真」).

Display only: nothing here talks to the unit. For one QC name this module finds

* the origin - what the model is based on. First the ModelRepo's ``tm``
  ("Based on Klon® Centaur®", marks removed), then the player's own device list
  (a Markdown table: QC名 / 元ネタ / 旧名, path in settings). Captures are
  matched by their capture name. A model with neither is "Neural DSP original";
* a photo, in this order:
  1. the player's own picture in ``~/.qctone/photos/`` (file stem = QC name or
     origin name, any case and punctuation; the editor's drag-and-drop saves
     there),
  2. a freely licensed photo from Wikimedia Commons, searched by the origin
     name only, cached in ``~/.qctone/photos/cache/`` with its author and
     licence (always shown with the picture). A weak match is not shown: every
     model number in the name and the brand must appear in the file's title,
     description or categories,
  3. nothing - the page then offers "画像検索" / "Reverb" buttons that open the
     search in the browser. We never fetch Google's or Reverb's images.

The only data sent outside is the gear name being searched. The device list
and Neural DSP's data are never committed to the repository; they are read at
run time from the player's machine.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from qcsuite import paths
from qcsuite.errors import QcsuiteError

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "qcsuite-photo-bot/1.0 (local gear photo lookup) python-urllib"
# Wikimedia's limits (2026): 10 requests/min for a client without contact details
# in its User-Agent, 200/min with them; at most 3 at once; honour Retry-After on
# 429/503 (else wait at least 5 s). We send no contact unless the player sets one
# (`qcsuite photos config --contact ...`), so requests are spaced 6.5 s apart -
# across processes, since the server and `photos prefetch` may run together.
INTERVAL_ANON = 6.5
INTERVAL_CONTACT = 0.5
MIN_COOLDOWN = 30
THUMB_WIDTH = 640
NEGATIVE_DAYS = 30            # retry a "not found" after this long
TIMEOUT = 12
MAX_UPLOAD = 12 * 1024 * 1024
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
DEFAULT_DEVICE_LIST = Path.home() / "Downloads" / "quad-cortex-device-list.md"

_MARKS = re.compile(r"[®™©]")
_BASED = re.compile(r"^\s*based\s+on\s+", re.I)
_STOP = {"with", "and", "the", "of", "a", "an", "for", "in", "on", "by", "custom",
         "speakers", "speaker", "drivers", "driver", "s", "mod", "modded", "version",
         "amp", "amplifier", "amplifiers", "pedal", "head", "combo", "cabinet", "cab"}


class PhotoError(QcsuiteError):
    pass


def norm(text: str) -> str:
    """Comparison form: lower case letters and digits only."""
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def clean_origin(text: str) -> str:
    """'Based on Klon® Centaur®' -> 'Klon Centaur'."""
    text = _BASED.sub("", _MARKS.sub("", text or ""))
    return re.sub(r"\s+", " ", text).strip(" .,")


def search_query(origin: str) -> str:
    """The gear name to search for: the first item of a 'A / B' cab line, no
    'with ... speakers' tail, no parentheses, no production year."""
    q = re.sub(r"（[^）]*）", " ", origin)
    q = re.split(r"\s*/\s+|\s+-\s+", q)[0]
    q = _CJK.sub(" ", q)          # "Keeley 4 Knob 系の改造品" -> "Keeley 4 Knob"
    q = re.split(r"\s+(?:with|w/|loaded with|featuring)\s+", q, maxsplit=1, flags=re.I)[0]
    q = re.sub(r"\([^)]*\)", " ", q)
    # A production year ("Marshall JCM800 1987") narrows the search to nothing;
    # a photo of the model is what the panel wants.
    q = re.sub(r"\s(?:19|20)\d\d(?=\s|$)", " ", q)
    return re.sub(r"\s+", " ", q).strip(" .,-")


def search_links(query: str) -> dict:
    q = urllib.parse.quote_plus(query)
    return {"images": f"https://www.google.com/search?tbm=isch&q={q}",
            "reverb": f"https://reverb.com/marketplace?query={q}"}


# --- settings and the device list --------------------------------------------

def photos_dir() -> Path:
    # Photos are about gear, not about one unit, so mock runs share them.
    return paths.sub("photos")


def cache_dir() -> Path:
    return paths.ensure(photos_dir() / "cache")


def settings_file() -> Path:
    return paths.home() / "settings.json"


def settings() -> dict:
    try:
        data = json.loads(settings_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value) -> None:
    data = settings()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    paths.ensure(settings_file().parent)
    tmp = settings_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(settings_file())


def device_list_path() -> Path:
    """``QCSUITE_DEVICE_LIST``, else ``device_list`` in settings.json, else
    ~/Downloads/quad-cortex-device-list.md."""
    raw = os.environ.get("QCSUITE_DEVICE_LIST") or settings().get("device_list")
    return Path(raw).expanduser() if raw else DEFAULT_DEVICE_LIST


def commons_enabled() -> bool:
    """Off with QCSUITE_PHOTOS_OFFLINE=1 or ``"commons": false`` in settings."""
    if os.environ.get("QCSUITE_PHOTOS_OFFLINE") == "1":
        return False
    return settings().get("commons", True) is not False


@dataclass
class ListEntry:
    name: str
    origin: str = ""        # "" = no origin (Neural DSP's own design)
    note: str = ""
    section: str = ""


def _skipped(section: str) -> bool:
    low = section.lower()
    return low.startswith(("plugin", "announced", "qc に")) or "使い方" in section


def parse_device_list(text: str) -> dict[str, ListEntry]:
    """QC name (normalized; old names too) -> entry, from the Markdown tables.

    A column named exactly 元ネタ holds the origin. 元ネタ・備考 is only a note
    (e.g. "Little Labs IBP に着想": inspired by, not a model of). Sections for
    plugins, announced devices and "not on the QC" are skipped.
    """
    out: dict[str, ListEntry] = {}
    section, header = "", None
    for line in text.splitlines():
        if line.startswith("## "):
            section, header = line[3:].strip(), None
            continue
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if _skipped(section):
            continue
        row = dict(zip(header, cells))
        name = row.get("QC名") or ""
        if not name:
            continue
        origin = row.get("元ネタ", "")
        note = row.get("元ネタ・備考", "")
        entry = ListEntry(name=name, origin=origin, note=note, section=section)
        names = [name] + [n.strip() for n in name.split(" / ")[:1]]
        old = row.get("旧名", "")
        if old:
            names += [o.strip() for o in old.split(" / ")]
        for n in names:
            if norm(n):
                out.setdefault(norm(n), entry)
    return out


_list_cache: dict = {"key": None, "data": {}}
_list_lock = threading.Lock()


def device_list() -> dict[str, ListEntry]:
    path = device_list_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _list_lock:
        if _list_cache["key"] != key:
            try:
                _list_cache["data"] = parse_device_list(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                _list_cache["data"] = {}
            _list_cache["key"] = key
        return _list_cache["data"]


# --- origin --------------------------------------------------------------------

_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]+")
# Device-list lines that describe, rather than name, the gear: a generic type
# ("汎用オプト"), an inspiration ("... モードに着想") or a free version of a plugin effect.
_NOT_GEAR = re.compile(r"^汎用|に着想|の無償版")

# "Legendary 87 M", "Single (ST)", "Adaptive Gate (S/C)": the list names the base model.
_VARIANT = re.compile(r"(?:\s*\((?:M|ST|S/C|Lite)\)|\s+(?:M|ST))+\s*$")


def origin_for(name: str, based_on: str = "", plugin: bool = False) -> dict:
    """What a QC device is based on.

    ``kind`` is "hardware" (origin known), "original" (Neural DSP's own design,
    or a generic effect), "plugin" (a plugin device with no stated origin) or
    "unknown" (not in the ModelRepo's tm nor in the device list).
    """
    # Only a "Based on ..." line names real gear (a plugin's line names the plugin).
    origin = clean_origin(based_on) if _BASED.match(_MARKS.sub("", based_on or "")) else ""
    source = "modelrepo" if origin else None
    listed = device_list()
    entry = next((listed[k] for k in (norm(name), norm(_VARIANT.sub("", name)))
                  if k in listed), None)
    note = entry.note if entry else ""
    if not origin and entry and entry.origin:
        if _NOT_GEAR.search(entry.origin) or not norm(search_query(clean_origin(entry.origin))):
            note = note or entry.origin
        else:
            origin, source = clean_origin(entry.origin), "device_list"
    if origin:
        query = search_query(origin)
        return {"kind": "hardware", "origin": origin, "query": query, "source": source,
                "note": note, "links": search_links(query)}
    if plugin:
        return {"kind": "plugin", "origin": "", "query": "", "source": None, "note": note,
                "links": None}
    if entry is not None:
        return {"kind": "original", "origin": "", "query": "", "source": "device_list",
                "note": note, "links": None}
    return {"kind": "unknown", "origin": "", "query": "", "source": None, "note": "",
            "links": None}


# --- the player's own photos ------------------------------------------------------

def user_photo(*names: str) -> Path | None:
    keys = {norm(n) for n in names if norm(n)}
    if not keys:
        return None
    try:
        files = sorted(photos_dir().iterdir())
    except OSError:
        return None
    for f in files:
        if f.is_file() and f.suffix.lower() in IMAGE_EXT and not f.name.startswith(".") \
                and norm(f.stem) in keys:
            return f
    return None


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w .()+-]+", "_", name or "", flags=re.UNICODE).strip(" ._")
    if not stem:
        raise PhotoError("写真の名前(QC の名前)がありません。")
    return stem[:120]


def save_user_photo(name: str, data_url: str) -> Path:
    """Store a dropped picture as ``photos/<QC name>.<ext>``.

    An earlier picture for the same name is moved to ``photos/old/``, never
    deleted.
    """
    if not isinstance(data_url, str) or "," not in data_url:
        raise PhotoError("画像のデータが読めませんでした。")
    head, b64 = data_url.split(",", 1)
    if ";base64" not in head:
        raise PhotoError("画像のデータが読めませんでした。")
    if len(b64) > MAX_UPLOAD * 4 // 3 + 8:
        raise PhotoError("画像が大きすぎます(12 MB まで)。")
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError:
        raise PhotoError("画像のデータが読めませんでした。") from None
    ext = _sniff(data)
    if ext is None:
        raise PhotoError("JPEG / PNG / WebP / GIF の画像だけ登録できます。")
    stem = safe_stem(name)
    base = photos_dir()
    old = user_photo(stem)
    if old is not None:
        dest_dir = paths.ensure(base / "old")
        old.replace(dest_dir / f"{old.stem}-{int(time.time())}{old.suffix}")
    dest = base / f"{stem}{ext}"
    tmp = base / f".{stem}.tmp"
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


# --- Wikimedia Commons -----------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[0-9a-z]+", text.lower()) if t not in _STOP]


def _strip_html(text) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def good_match(query: str, haystack: str) -> bool:
    """True when the file is about this gear, not just a similar word.

    The brand (first word), every word with a digit (model numbers) and at
    least two thirds of the remaining words must appear. Words are matched
    with and without spaces, so "JCM800" matches "JCM 800".
    """
    words = _tokens(query)
    if not words:
        return False
    spaced = " " + " ".join(re.findall(r"[0-9a-z]+", haystack.lower())) + " "
    packed = norm(haystack)

    def has(w):
        return f" {w} " in spaced or (len(w) >= 3 and w in packed)

    if not has(words[0]):
        return False
    if not all(has(w) for w in words if re.search(r"\d", w)):
        return False
    rest = [w for w in words[1:] if not re.search(r"\d", w)]
    if rest and sum(has(w) for w in rest) * 3 < len(rest) * 2:
        return False
    return True


class RateLimited(OSError):
    pass


def user_agent() -> str:
    contact = str(settings().get("commons_contact") or "").strip()
    return USER_AGENT.replace("(local gear photo lookup)",
                              f"(local gear photo lookup; {contact})" if contact else
                              "(local gear photo lookup)")


def _gate_file() -> Path:
    return cache_dir() / ".ratelimit"


def _read_gate() -> dict:
    try:
        return json.loads(_gate_file().read_text())
    except (OSError, ValueError):
        return {}


def cooldown_left() -> float:
    return max(0.0, float(_read_gate().get("until", 0)) - time.time())


def _request(url: str):
    """One request to Wikimedia, spaced and serialized across processes."""
    import fcntl
    import urllib.error
    interval = INTERVAL_CONTACT if settings().get("commons_contact") else INTERVAL_ANON
    with open(cache_dir() / ".ratelimit.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        gate = _read_gate()
        now = time.time()
        if gate.get("until", 0) > now:
            raise RateLimited("Commons is asking us to wait")
        wait = gate.get("last", 0) + interval - now
        if wait > 0:
            time.sleep(min(wait, interval))
        gate["last"] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read(MAX_UPLOAD + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503):
                try:
                    after = float(exc.headers.get("Retry-After") or 0)
                except ValueError:
                    after = 0
                gate["until"] = time.time() + max(after, MIN_COOLDOWN)
                raise RateLimited(f"HTTP {exc.code}") from None
            raise
        finally:
            tmp = _gate_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(gate))
            tmp.replace(_gate_file())


def _get_json(url: str) -> dict:
    return json.loads(_request(url).decode("utf-8"))


def _get_bytes(url: str) -> bytes:
    return _request(url)


def commons_search(query: str, fetch_json=None) -> dict | None:
    """The best freely licensed Commons photo for ``query``, or None."""
    fetch_json = fetch_json or _get_json
    params = {"action": "query", "format": "json", "generator": "search",
              "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": "6", "gsrlimit": "10",
              "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
              "iiurlwidth": str(THUMB_WIDTH),
              "iiextmetadatafilter": "ObjectName|ImageDescription|Categories|Artist|"
                                     "LicenseShortName|LicenseUrl|NonFree|Restrictions"}
    data = fetch_json(COMMONS_API + "?" + urllib.parse.urlencode(params))
    pages = sorted((data.get("query") or {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 999))
    for page in pages:
        info = (page.get("imageinfo") or [None])[0]
        if not info or not str(info.get("mime", "")).startswith("image/"):
            continue
        meta = {k: _strip_html((v or {}).get("value")) for k, v in
                (info.get("extmetadata") or {}).items()}
        licence = meta.get("LicenseShortName", "")
        if not licence or meta.get("NonFree", "").lower() in ("true", "1"):
            continue
        title = page.get("title", "")
        hay = " ".join([title, meta.get("ObjectName", ""), meta.get("ImageDescription", "")[:600],
                        meta.get("Categories", "").replace("|", " ")])
        if not good_match(query, hay):
            continue
        return {"title": title.removeprefix("File:"),
                "thumb": info.get("thumburl") or info.get("url"),
                "page": info.get("descriptionurl", ""),
                "author": meta.get("Artist", "")[:200] or "不明",
                "license": licence, "license_url": meta.get("LicenseUrl", ""),
                "restrictions": meta.get("Restrictions", "")}
    return None


def _cache_key(query: str) -> str:
    return hashlib.sha1(norm(query).encode()).hexdigest()[:16]


_commons_lock = threading.Lock()


def commons_photo(query: str, *, fetch: bool = True, fetch_json=None,
                  fetch_bytes=None) -> dict | None:
    """The cached Commons photo for ``query`` (searching once if ``fetch``).

    Returns the cache record ``{"found": bool, ...}`` or None when nothing is
    cached and ``fetch`` is off, or the network failed (failures are not
    cached, so the next look tries again).
    """
    if not norm(query):
        return None
    key = _cache_key(query)
    meta_path = cache_dir() / f"{key}.json"

    def cached():
        try:
            rec = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            return None
        if rec.get("found"):
            if (cache_dir() / rec.get("file", "")).is_file():
                return rec
            return None
        if time.time() - rec.get("checked", 0) < NEGATIVE_DAYS * 86400:
            return rec
        return None

    rec = cached()
    if rec is not None or not fetch or not commons_enabled():
        return rec
    if fetch_json is None and cooldown_left() > 0:
        return None              # Commons asked us to wait (429); don't knock again
    with _commons_lock:          # one Commons request at a time
        rec = cached()
        if rec is not None:
            return rec
        try:
            hit = commons_search(query, fetch_json)
            rec = {"query": query, "checked": time.time(), "found": False}
            if hit:
                data = (fetch_bytes or _get_bytes)(hit["thumb"])
                ext = _sniff(data)
                if ext and len(data) <= MAX_UPLOAD:
                    (cache_dir() / f"{key}{ext}").write_bytes(data)
                    rec.update(hit, found=True, file=f"{key}{ext}")
        except (OSError, ValueError):
            return None
        tmp = meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False))
        tmp.replace(meta_path)
        return rec


# --- what the page asks for -----------------------------------------------------------

def photo_url(kind: str, filename: str) -> str:
    return f"/api/editor/photo/file/{kind}/{urllib.parse.quote(filename)}"


def lookup(name: str, based_on: str = "", plugin: bool = False, fetch: bool = True,
           **commons_kw) -> dict:
    """Everything the photo panel shows for one QC device."""
    origin = origin_for(name, based_on, plugin)
    out = {"name": name, **origin, "photo": None}
    own = user_photo(name, origin["origin"], origin["query"])
    if own is not None:
        out["photo"] = {"source": "user", "url": photo_url("user", own.name),
                        "file": own.name}
        return out
    if origin["kind"] != "hardware":
        return out
    rec = commons_photo(origin["query"], fetch=fetch, **commons_kw)
    if rec is None:
        out["commons"] = "limited" if commons_enabled() and cooldown_left() > 0 else \
        "pending" if not fetch and commons_enabled() else (
            "off" if not commons_enabled() else "error")
    elif rec.get("found"):
        out["photo"] = {"source": "commons", "url": photo_url("cache", rec["file"]),
                        "title": rec.get("title", ""), "page": rec.get("page", ""),
                        "author": rec.get("author", ""), "license": rec.get("license", ""),
                        "license_url": rec.get("license_url", "")}
    else:
        out["commons"] = "none"
    return out


def photo_file(kind: str, filename: str) -> Path:
    """A served picture; only plain image files directly inside the folder."""
    base = {"user": photos_dir(), "cache": cache_dir()}.get(kind)
    if base is None or not isinstance(filename, str) or "/" in filename or "\\" in filename \
            or filename.startswith(".") or Path(filename).suffix.lower() not in IMAGE_EXT:
        raise FileNotFoundError("写真が見つかりません。")
    path = base / filename
    if not path.is_file() or path.resolve().parent != base.resolve():
        raise FileNotFoundError("写真が見つかりません。")
    return path


# --- CLI ---------------------------------------------------------------------------------

@dataclass
class Tally:
    total: int = 0
    found: int = 0
    names: list = field(default_factory=list)


def prefetch(device, progress=print, **commons_kw) -> dict:
    """Look every placeable model up on Commons, within Wikimedia's rate limit
    (spacing is done by the request gate; a 429 waits out its Retry-After)."""
    from qcsuite.features import editor
    listing = editor.catalog_listing(device)
    seen: dict[str, dict] = {}
    tallies: dict[str, Tally] = {}
    for group in listing["groups"]:
        for m in group["models"]:
            o = origin_for(m["name"], m.get("based_on", ""), m.get("plugin", False))
            t = tallies.setdefault(group["label"], Tally())
            if o["kind"] != "hardware":
                continue
            t.total += 1
            q = o["query"]
            if q not in seen:
                rec = commons_photo(q, fetch=True, **commons_kw)
                for _ in range(3):
                    if rec is not None or cooldown_left() <= 0:
                        break
                    time.sleep(cooldown_left() + 1)
                    rec = commons_photo(q, fetch=True, **commons_kw)
                seen[q] = rec or {}
            if seen[q].get("found"):
                t.found += 1
                t.names.append(m["name"])
            progress(f"{group['label']:<12} {m['name']:<32} {q:<40} "
                     f"{'○' if seen[q].get('found') else '-' if seen[q] else '?'}")
    return {k: {"hardware": v.total, "found": v.found} for k, v in tallies.items()}


def cmd_show(ctx, a):
    r = lookup(a.name, a.based_on or "", fetch=not a.no_fetch)
    ctx.out(r, json.dumps(r, ensure_ascii=False, indent=2))


def cmd_prefetch(ctx, a):
    with ctx.rig("photos prefetch") as rig:
        listing_device = rig.device
        result = prefetch(listing_device)
    print()
    for label, t in result.items():
        if t["hardware"]:
            print(f"{label:<12} {t['found']:>3} / {t['hardware']:<3}")


def cmd_config(ctx, a):
    if a.device_list is not None:
        save_setting("device_list", a.device_list or None)
    if a.commons is not None:
        save_setting("commons", a.commons == "on")
    if a.contact is not None:
        save_setting("commons_contact", a.contact or None)
    print(f"一覧表: {device_list_path()} ({'あり' if device_list_path().exists() else 'なし'})")
    print(f"写真フォルダー: {photos_dir()}")
    print(f"Wikimedia Commons: {'使う' if commons_enabled() else '使わない'}"
          f"(User-Agent: {user_agent()})")


def add_cli(sub):
    p = sub.add_parser("photos", help="実機の写真(エディター)")
    s = p.add_subparsers(dest="photos_cmd", required=True)
    show = s.add_parser("show", help="1 台ぶんの元ネタと写真を表示")
    show.add_argument("name")
    show.add_argument("--based-on")
    show.add_argument("--no-fetch", action="store_true")
    show.set_defaults(fn=cmd_show)
    pre = s.add_parser("prefetch", help="置けるモデルを全部 Commons で探してキャッシュ(1 分 10 回以内)")
    pre.set_defaults(fn=cmd_prefetch)
    cfg = s.add_parser("config", help="一覧表のパスと Commons の使用を設定")
    cfg.add_argument("--device-list", help="デバイス一覧表(md)のパス。空文字で既定に戻す")
    cfg.add_argument("--commons", choices=["on", "off"])
    cfg.add_argument("--contact", help="Commons に名乗る連絡先(メールか URL)。付けると 1 分 200 回まで"
                                       "速く検索できる。空文字で外す。付けなければ 1 分 10 回以内に抑える")
    cfg.set_defaults(fn=cmd_config)
