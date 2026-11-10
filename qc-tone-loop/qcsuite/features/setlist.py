"""Setlists and MIDI (feature 8): song order -> slots -> Program Change table.

A setlist starts as a song list, one song per line::

    # 2026-11-03 渋谷
    Funk Opener | Cory Wong Funk Tour
    バラード     | kami:1E
    Solo                                  (no preset given: the song name is looked up)

Each song is resolved to a stored preset by name (exact, then partial, case
insensitive) across the user folders and the Factory Library. On the player's
unit 20 names exist in more than one folder, so an ambiguous name is reported
with its candidates rather than guessed; ``--prefer-folder`` settles it.

Placing the songs in order means copying each preset into consecutive slots
of a target folder - the sandbox by default, Live/Artist/Song/Workshop only
when allowed, the protected folders never. It is a :mod:`qcsuite.plan` like
every other write: dry run by default, and on ``--apply`` every copy recalls
its source on the unit (the sound changes), drops tags, and may come back
renamed (the unit de-duplicates names to 20 characters + ``_N``). The ``map``
mode copies nothing and just uses where the presets already are.

**MIDI**, from Neural DSP's support page (found by web search, not verified on
the player's unit - the hardware test plan checks it):

* CC#32 (0-12) selects the setlist: Factory Library = 0, My Presets = 1, the
  custom setlists 2-12. Which number each CUSTOM folder gets is not documented
  (creation order? alphabetical?), so it comes from ``midi_setlists.json`` in
  the qcsuite home, and anything not in there is shown as unknown.
* CC#0 picks the group of 128: 0 for slots 1A-16H (positions 0-127), 1 for
  17A-32H (128-255). Send the CCs before the PC.
* The PC number within the group: the support page's two examples disagree by
  one ("17A in setlist 5 = CC#0 1, CC#32 5, Program #1" reads 1-based; "9D =
  Program #67" reads 0-based). The table gives both the 0-based wire value and
  the 1-based number many controllers display; check which one your
  controller means.
* The unit's MIDI channel setting is not read here; ``--channel`` only labels
  the table.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
from pathlib import Path

import pyquadcortex as pq

from qcsuite import paths
from qcsuite import plan as plan_mod
from qcsuite.audio import export
from qcsuite.device import guard
from qcsuite.errors import PlanError
from qcsuite.features import sheets_sources as src

UNKNOWN_CC32 = "未設定(実機で確認)"
DEFAULT_CC32 = {"Factory Library": 0, "My Presets": 1}
_SLOT = re.compile(r"^\d{1,2}[A-Ha-h]$")


# -- storage -------------------------------------------------------------------------

def setlists_dir(mock: bool) -> Path:
    return paths.sub("setlists", mock)


def _path(name: str, mock: bool) -> Path:
    return setlists_dir(mock) / f"{export.safe_name(name)}.json"


def save_setlist(setlist: dict, mock: bool) -> Path:
    setlist["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = _path(setlist["name"], mock)
    path.write_text(json.dumps(setlist, ensure_ascii=False, indent=2) + "\n")
    return path


def load_setlist(name: str, mock: bool) -> dict:
    path = _path(name, mock)
    if not path.exists():
        raise FileNotFoundError(f"セットリスト「{name}」がありません(qcsuite setlist list で確認)")
    return json.loads(path.read_text())


def listing(mock: bool) -> list[dict]:
    out = []
    for path in sorted(setlists_dir(mock).glob("*.json")):
        s = json.loads(path.read_text())
        songs = s.get("songs", [])
        out.append({"name": s["name"], "songs": len(songs), "updated": s.get("updated"),
                    "resolved": sum(1 for x in songs if x["status"] == "resolved"),
                    "placement": s.get("placement")})
    return out


# -- parsing and resolving -------------------------------------------------------------

def parse_songs(text: str) -> list[dict]:
    """Songs from text lines (``曲名 | プリセット`` or ``曲名``) or a JSON list."""
    stripped = text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(stripped)
        items = data.get("songs", []) if isinstance(data, dict) else data
        out = []
        for item in items:
            if isinstance(item, str):
                out.append({"song": item, "query": item})
            else:
                song = item.get("song") or item.get("title") or item.get("name")
                if not song:
                    raise ValueError(f"曲名がありません: {item}")
                out.append({"song": song, "query": item.get("preset") or item.get("ref") or song})
        return out
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        song, _, query = (part.strip() for part in line.partition("|"))
        out.append({"song": song, "query": query or song})
    return out


def preset_index(device) -> list[dict]:
    """Every stored preset by listing (no recall): user folders, then factory."""
    out = []
    for f in device.setlists():
        if f["kind"] == "sandbox":
            continue
        for p in device.presets(f["key"]):
            out.append({"folder": f["key"], "folder_name": f["name"], "position": p["index"],
                        "slot": p["slot"], "name": p["name"], "kind": f["kind"]})
    return out


def _is_ref(query: str) -> bool:
    return ":" in query and bool(_SLOT.match(query.rsplit(":", 1)[1].strip()))


def resolve(songs: list[dict], index: list[dict], prefer_folder: str | None = None) -> list[dict]:
    """Attach a source preset to every song, or the reason it has none."""
    prefer = src.folder_key(prefer_folder) if prefer_folder else None
    out = []
    for n, song in enumerate(songs, 1):
        q = song["query"].strip()
        entry = {"n": n, "song": song["song"], "query": q, "status": "missing",
                 "source": None, "candidates": []}
        if _is_ref(q):
            key, position = src.parse_ref(q)
            found = [p for p in index if p["folder"] == key and p["position"] == position]
        else:
            low = q.lower()
            found = [p for p in index if p["name"].lower() == low] or \
                [p for p in index if low in p["name"].lower()]
            if len(found) > 1 and prefer:
                preferred = [p for p in found if p["folder"] == prefer]
                if len(preferred) == 1:
                    found = preferred
        if len(found) == 1:
            entry.update(status="resolved", source=found[0])
        elif found:
            entry.update(status="ambiguous", candidates=found[:20])
        out.append(entry)
    return out


def build(name: str, text: str, device, *, mock: bool, prefer_folder: str | None = None) -> dict:
    """Parse, resolve and store a setlist.

    Rebuilding a setlist of the same name keeps the choices made with
    :func:`choose` for songs whose line did not change, so "plan, choose,
    plan again" works without repeating the choices.
    """
    songs = resolve(parse_songs(text), preset_index(device), prefer_folder)
    try:
        previous = {(s["n"], s["song"], s["query"]): s for s in load_setlist(name, mock)["songs"]}
    except FileNotFoundError:
        previous = {}
    for song in songs:
        old = previous.get((song["n"], song["song"], song["query"]))
        if song["status"] != "resolved" and old and old["status"] == "resolved":
            song.update(status="resolved", source=old["source"], candidates=[])
    setlist = {"name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
               "source_text": text, "songs": songs, "placement": None}
    save_setlist(setlist, mock)
    return setlist


def choose(setlist: dict, n: int, folder: str, position: int, device) -> dict:
    """Settle one ambiguous (or missing) song by naming its preset."""
    key = src.folder_key(folder)
    entries = {p["index"]: p for p in device.presets(key)}
    if position not in entries:
        raise PlanError(f"{guard.folder_name(key)} {pq.position_to_slot(position)} にプリセットがありません。")
    song = next(s for s in setlist["songs"] if s["n"] == n)
    song.update(status="resolved", candidates=[], source={
        "folder": key, "folder_name": guard.folder_name(key), "position": position,
        "slot": pq.position_to_slot(position), "name": entries[position]["name"],
        "kind": "factory" if key == src.FACTORY_KEY else "user"})
    return setlist


def problems(setlist: dict) -> list[str]:
    out = []
    for s in setlist["songs"]:
        if s["status"] == "ambiguous":
            names = ", ".join(f"{c['folder_name']}:{c['slot']}「{c['name']}」" for c in s["candidates"][:6])
            out.append(f"{s['n']}. {s['song']}: 「{s['query']}」が複数あります → {names}")
        elif s["status"] == "missing":
            out.append(f"{s['n']}. {s['song']}: 「{s['query']}」が見つかりません")
    return out


# -- placement -----------------------------------------------------------------------------

def plan_placement(device, setlist: dict, folder: str | None = None, start: str = "1A",
                   overwrite: bool = False, naming: str = "preset") -> plan_mod.Plan:
    """Copy steps putting the songs in order into ``folder`` (default: the sandbox).

    Nothing is sent. Unresolved songs and occupied target slots stop the plan
    here (occupied slots are only overwritten with ``overwrite``, as explicit
    destructive steps).
    """
    issues = problems(setlist)
    if issues:
        raise PlanError("曲とプリセットの対応が決まっていません:\n" + "\n".join(issues))
    sandbox = guard.folder_key(guard.SANDBOX_NAME)
    key = sandbox if folder is None else src.folder_key(folder)
    device.policy.check(key, "セットリストの配置")
    first = pq.slot_to_position(start)
    songs = setlist["songs"]
    if first + len(songs) > 256:
        raise PlanError(f"{start} から {len(songs)} 曲は 32H を超えます。")
    steps, blocked = [], []
    if key in {f["key"] for f in device.setlists()}:
        occupied = {p["index"]: p["name"] for p in device.presets(key)}
    elif key == sandbox:
        # A dry run sends nothing, so the sandbox is created by the plan itself.
        steps.append(plan_mod.Step("create_folder", key, note="sandbox を作ります"))
        occupied = {}
    else:
        raise PlanError(f"配置先のフォルダ「{guard.folder_name(key)}」がありません。")
    for i, s in enumerate(songs):
        position = first + i
        if position in occupied and not overwrite:
            blocked.append(f"{pq.position_to_slot(position)}「{occupied[position]}」")
            continue
        name = s["song"] if naming == "song" else s["source"]["name"]
        steps.append(plan_mod.Step(
            "copy", key, position=position, name=name,
            source={"folder": s["source"]["folder"], "position": s["source"]["position"]},
            overwrites=occupied.get(position),
            note=f"{s['n']}. {s['song']}"))
    if blocked:
        raise PlanError("配置先のスロットが埋まっています(上書きするなら --overwrite):\n"
                        + "\n".join(blocked))
    plan = plan_mod.Plan(f"セットリスト「{setlist['name']}」を {guard.folder_name(key)} "
                         f"{start} から配置", steps=steps)
    plan.warnings += [
        "コピーのたびに元のプリセットを本体に読み込むので、音が切り替わります。",
        "タグは引き継がれません。同じ名前があると本体が名前を 20 文字以内に切り詰めて _N を付けます。",
    ]
    setlist["placement"] = {"folder": key, "folder_name": guard.folder_name(key),
                            "start": start, "mode": "copy", "plan_id": plan.id}
    return plan


def use_existing(setlist: dict) -> dict:
    """The "map" mode: no copies, the MIDI table points at the presets where they are."""
    setlist["placement"] = {"mode": "map"}
    for s in setlist["songs"]:
        s.pop("target", None)
    return setlist


def refresh_targets(device, setlist: dict) -> dict:
    """Read back the target folder after placing: stored names and which slots exist."""
    placement = setlist.get("placement") or {}
    if placement.get("mode") != "copy":
        return setlist
    key = placement["folder"]
    entries = {p["index"]: p["name"] for p in device.presets(key)}
    first = pq.slot_to_position(placement["start"])
    for i, s in enumerate(setlist["songs"]):
        position = first + i
        s["target"] = {"folder": key, "folder_name": guard.folder_name(key), "position": position,
                       "slot": pq.position_to_slot(position), "name": entries.get(position),
                       "placed": position in entries}
    return setlist


# -- MIDI ------------------------------------------------------------------------------------

def midi_config_path(mock: bool) -> Path:
    return paths.home(mock) / "midi_setlists.json"


def midi_config(mock: bool) -> dict:
    """Folder name -> CC#32 number. Defaults: Factory Library 0, My Presets 1."""
    mapping = dict(DEFAULT_CC32)
    path = midi_config_path(mock)
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{path} はフォルダ名 → 番号の JSON にしてください")
        for name, number in data.items():
            if not isinstance(number, int) or not 0 <= number <= 12:
                raise ValueError(f"{path}: 「{name}」の番号 {number!r} は 0〜12 の整数にしてください")
            mapping[name] = number
    return mapping


def set_midi_config(mapping: dict, mock: bool) -> dict:
    clean = {k: int(v) for k, v in mapping.items() if v not in (None, "")}
    for name, number in clean.items():
        if not 0 <= number <= 12:
            raise ValueError(f"「{name}」の番号 {number} は 0〜12 にしてください")
    paths.ensure(paths.home(mock))
    midi_config_path(mock).write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n")
    return midi_config(mock)


def midi_numbers(position: int) -> dict:
    """CC#0 and PC for a linear slot position (0-255)."""
    if not 0 <= position < 256:
        raise ValueError(f"位置は 0〜255: {position}")
    return {"cc0": position // 128, "pc": position % 128, "pc_display": position % 128 + 1}


def midi_rows(setlist: dict, mapping: dict, channel: int = 1) -> list[dict]:
    rows = []
    mode = (setlist.get("placement") or {}).get("mode", "map")
    for s in setlist["songs"]:
        where = s.get("target") if mode == "copy" and s.get("target") else s.get("source")
        if where is None:
            rows.append({"n": s["n"], "song": s["song"], "preset": None, "note": "未解決"})
            continue
        cc32 = mapping.get(where["folder_name"])
        notes = []
        if cc32 is None:
            notes.append(f"CC#32 は {UNKNOWN_CC32}")
        if mode == "copy" and not where.get("placed", True):
            notes.append("まだ配置されていません")
        rows.append({"n": s["n"], "song": s["song"], "preset": where.get("name"),
                     "folder": where["folder_name"], "slot": where["slot"],
                     "position": where["position"], **midi_numbers(where["position"]),
                     "cc32": cc32 if cc32 is not None else UNKNOWN_CC32,
                     "channel": channel, "note": "、".join(notes)})
    return rows


MIDI_NOTES = [
    "CC#0 → CC#32 → PC の順に送ります(Neural DSP のサポート情報。実機で未確認)。",
    "PC はグループ内の番号です。0 始まり(送信値)と 1 始まり(表示)の両方を載せています。"
    "サポート情報の例が 1 つずれているため、どちらで合うかは実機で確認してください。",
    "CC#32 はセットリスト番号です(Factory 0、My Presets 1、ほかは 2〜12)。"
    "自分で作ったフォルダの番号の決まり方は資料に無いため、midi_setlists.json で設定します。",
    "MIDI チャンネルは本体の設定に合わせてください(本体の既定値は未確認)。",
]


def midi_export(setlist: dict, mapping: dict, fmt: str = "csv", channel: int = 1) -> str:
    rows = midi_rows(setlist, mapping, channel)
    if fmt == "json":
        return json.dumps({"setlist": setlist["name"], "notes": MIDI_NOTES, "rows": rows},
                          ensure_ascii=False, indent=2) + "\n"
    header = ["順番", "曲名", "プリセット", "フォルダ", "スロット", "位置", "CC#0", "CC#32",
              "PC(0始まり)", "PC(1始まり表示)", "MIDIch", "注意"]
    keys = ["n", "song", "preset", "folder", "slot", "position", "cc0", "cc32", "pc",
            "pc_display", "channel", "note"]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(k, "") for k in keys])
        return buf.getvalue()
    if fmt == "md":
        lines = [f"# {setlist['name']} の MIDI 対応表", "",
                 "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for r in rows:
            lines.append("| " + " | ".join(str(r.get(k, "") if r.get(k) is not None else "")
                                           for k in keys) + " |")
        lines += ["", *[f"- {n}" for n in MIDI_NOTES]]
        return "\n".join(lines) + "\n"
    raise ValueError(f"形式は csv / md / json: {fmt}")


# -- CLI ---------------------------------------------------------------------------------------

def _text(ctx, setlist: dict) -> str:
    lines = [f"セットリスト「{setlist['name']}」({len(setlist['songs'])} 曲)"]
    for s in setlist["songs"]:
        if s["status"] == "resolved":
            where = f"{s['source']['folder_name']}:{s['source']['slot']}「{s['source']['name']}」"
            target = s.get("target")
            if target:
                where += f" → {target['folder_name']}:{target['slot']}" + (
                    f"「{target['name']}」" if target.get("name") else "(未配置)")
            lines.append(f"  {s['n']:>2}. {s['song']}: {where}")
        else:
            lines.append(f"  {s['n']:>2}. {s['song']}: [{ '曖昧' if s['status'] == 'ambiguous' else '見つからない'}]")
    issues = problems(setlist)
    if issues:
        lines += ["", "未解決:", *[f"  {i}" for i in issues],
                  "", "決め方: --prefer-folder で優先フォルダを指定するか、`qcsuite setlist choose`"]
    return "\n".join(lines)


def cmd_plan(ctx, a):
    text = Path(a.songs).read_text()
    with ctx.rig("setlist plan") as rig:
        setlist = build(a.name, text, rig.device, mock=ctx.mock, prefer_folder=a.prefer_folder)
        if problems(setlist):
            ctx.out(setlist, _text(ctx, setlist))
            raise SystemExit("曲とプリセットの対応に未解決があるため、配置計画は作りませんでした。")
        if a.map:
            use_existing(setlist)
            save_setlist(setlist, ctx.mock)
            ctx.out(setlist, _text(ctx, setlist) + "\n(map: コピーせず、今ある場所で MIDI 表を作ります)")
            return
        plan = plan_placement(rig.device, setlist, a.folder, a.start, a.overwrite, a.naming)
        save_setlist(setlist, ctx.mock)
        result = ctx.run_plan(plan, rig.device, apply=a.apply)
        if not result.get("dry_run"):
            refresh_targets(rig.device, setlist)
            save_setlist(setlist, ctx.mock)
    ctx.out({"setlist": setlist, "plan": result.get("plan", {}).get("id")}, _text(ctx, setlist))


def cmd_choose(ctx, a):
    setlist = load_setlist(a.name, ctx.mock)
    key, position = src.parse_ref(a.preset)
    with ctx.rig("setlist choose") as rig:
        choose(setlist, a.n, key, position, rig.device)
    save_setlist(setlist, ctx.mock)
    ctx.out(setlist, _text(ctx, setlist))


def cmd_refresh(ctx, a):
    setlist = load_setlist(a.name, ctx.mock)
    with ctx.rig("setlist refresh") as rig:
        refresh_targets(rig.device, setlist)
    save_setlist(setlist, ctx.mock)
    ctx.out(setlist, _text(ctx, setlist))


def cmd_midi(ctx, a):
    setlist = load_setlist(a.name, ctx.mock)
    text = midi_export(setlist, midi_config(ctx.mock), a.format, a.channel)
    if a.out:
        Path(a.out).write_text(text)
        print(f"書き出しました: {a.out}", file=sys.stderr)
    else:
        print(text, end="")


def cmd_list(ctx, a):
    rows = listing(ctx.mock)
    ctx.out(rows, "\n".join(f"{r['name']:<24} {r['resolved']}/{r['songs']} 曲  {r['updated']}"
                            for r in rows) or "(セットリストはありません)")


def cmd_show(ctx, a):
    setlist = load_setlist(a.name, ctx.mock)
    ctx.out(setlist, _text(ctx, setlist))


def cmd_midi_map(ctx, a):
    if a.set:
        path = midi_config_path(ctx.mock)
        current = json.loads(path.read_text()) if path.exists() else {}
        for item in a.set:
            name, _, number = item.rpartition("=")
            if not name.strip() or not number.strip().isdigit():
                raise SystemExit(f"「フォルダ名=番号」で指定してください: {item}")
            current[name.strip()] = int(number)
        set_midi_config(current, ctx.mock)
    mapping = midi_config(ctx.mock)
    ctx.out(mapping, "\n".join(f"{k}: CC#32 = {v}" for k, v in mapping.items())
            + "\n(ここに無いフォルダは「未設定」。番号は実機で確認してから設定してください)")


def add_cli(sub):
    p = sub.add_parser("setlist", help="曲順からスロット配置計画と MIDI 対応表を作る")
    s = p.add_subparsers(dest="setlist_cmd", required=True)
    c = s.add_parser("plan", help="曲のリストから配置計画を作る(--apply で実行)")
    c.add_argument("songs", help="曲のリスト(1 行 1 曲、「曲名 | プリセット」も可)か JSON")
    c.add_argument("--name", required=True, help="セットリストの名前")
    c.add_argument("--folder", help="配置先(既定: sandbox。Live などは --allow-folder も必要)")
    c.add_argument("--start", default="1A", help="最初の曲を置くスロット(既定 1A)")
    c.add_argument("--prefer-folder", help="同じ名前が複数あるときに優先するフォルダ")
    c.add_argument("--naming", choices=["preset", "song"], default="preset",
                   help="コピー後の名前(プリセット名のまま / 曲名)")
    c.add_argument("--overwrite", action="store_true", help="埋まっているスロットを上書きする(破壊的)")
    c.add_argument("--map", action="store_true", help="コピーせず、今ある場所で MIDI 表を作る")
    c.add_argument("--apply", action="store_true")
    c.set_defaults(fn=cmd_plan)
    c = s.add_parser("choose", help="曖昧な曲のプリセットを決める")
    c.add_argument("name")
    c.add_argument("n", type=int, help="曲の番号")
    c.add_argument("preset", help="フォルダ:スロット")
    c.set_defaults(fn=cmd_choose)
    c = s.add_parser("refresh", help="配置先を読み直して、保存された名前を記録する")
    c.add_argument("name")
    c.set_defaults(fn=cmd_refresh)
    c = s.add_parser("midi", help="MIDI 対応表(CSV / Markdown / JSON)")
    c.add_argument("name")
    c.add_argument("--format", choices=["csv", "md", "json"], default="md")
    c.add_argument("--channel", type=int, default=1, help="表に書く MIDI チャンネル(本体の設定に合わせる)")
    c.add_argument("--out")
    c.set_defaults(fn=cmd_midi)
    c = s.add_parser("midi-map", help="フォルダごとの CC#32 番号を表示・設定する")
    c.add_argument("--set", action="append", metavar="フォルダ名=番号")
    c.set_defaults(fn=cmd_midi_map)
    c = s.add_parser("list", help="セットリストの一覧")
    c.set_defaults(fn=cmd_list)
    c = s.add_parser("show", help="セットリストの中身")
    c.add_argument("name")
    c.set_defaults(fn=cmd_show)
