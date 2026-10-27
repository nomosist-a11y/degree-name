"""Preset management (feature 1, ideas J, O, P, R): find, compare, copy, look inside.

Everything here works from **folder listings**, which the unit answers without
loading anything - so listing, searching, sorting and spotting duplicate NAMES
never change the sound. Looking INSIDE a preset is different: the unit has no
side-effect-free read of a stored preset (``read_preset`` recalls it), so the
chain and knob view comes from the latest backup when there is one
(:mod:`qcsuite.features.backup`), from the live grid (no side effects), or -
only when the player asks - by loading the preset on the unit.

Duplicate CONTENT is found from backup fingerprints: two presets whose
sound-relevant state (blocks, knobs, bypass, routing, row levels) is equal share
a fingerprint whatever their names. The tool proposes a cleanup in Markdown
for the player to carry out on the unit; it deletes nothing in the player's
folders.

Copying goes through a plan (:mod:`qcsuite.plan`): one recall and one save per
preset. The copy carries no tags (pyquadcortex ``save_current_preset``), and if
the destination already holds the name the unit renames it to at most 20
characters plus ``_N`` (pyquadcortex docstring; also seen on the player's unit).

The unit's Favorites (hearts) and the player's own "Favorite" folder are two
different things; idea R compares them by name, read only - no heart is ever
added or removed.

Captures and IRs (ideas J): the library is searchable, but a capture or IR
loaded from the computer reads back fine and does not change the sound
(measured on the player's unit), so search results are exported as a
shortlist to load by hand on the unit's screen.
"""

from __future__ import annotations

import csv
import io
import sys

import pyquadcortex as pq

from qcsuite import plan as plan_mod
from qcsuite import presets
from qcsuite.device import guard, inert
from qcsuite.errors import QcsuiteError
from qcsuite.features import backup

LOAD_WARNING = ("プリセットを本体に読み込んで表示します。本体の音が切り替わり、"
                "本体で保存していない編集は消えます。")
SORTS = ("folder", "slot", "name")


# -- the index ----------------------------------------------------------------------

def index(device, include_factory: bool = False, folders: list[str] | None = None) -> list[dict]:
    """One row per occupied slot of every user folder (and the factory, if asked)."""
    wanted = {guard.folder_key(f) for f in folders} if folders else None
    rows = []
    for f in device.setlists():
        if f["kind"] == "factory" and not include_factory:
            continue
        if wanted is not None and f["key"] not in wanted:
            continue
        for p in device.presets(f["key"]):
            rows.append({"folder": f["name"], "folder_key": f["key"], "position": p["index"],
                         "slot": p["slot"], "name": p["name"], "instrument": p["instrument"],
                         "kind": f["kind"], "protected": f["protected"]})
    return rows


def search(rows: list[dict], query: str = "", folder: str | None = None,
           sort: str = "folder") -> list[dict]:
    """Case-insensitive substring search over names (and folder names)."""
    if sort not in SORTS:
        raise QcsuiteError(f"並べ替えは {', '.join(SORTS)} のどれかです。")
    q = query.strip().casefold()
    key = guard.folder_key(folder) if folder else None
    out = [r for r in rows
           if (not q or q in r["name"].casefold() or q in r["folder"].casefold())
           and (key is None or r["folder_key"] == key)]
    if sort == "name":
        out.sort(key=lambda r: (r["name"].casefold(), r["folder"], r["position"]))
    elif sort == "slot":
        out.sort(key=lambda r: (r["position"], r["folder"]))
    else:
        out.sort(key=lambda r: (r["kind"] == "factory", r["folder"].casefold(), r["position"]))
    return out


# -- duplicates (ideas O) -------------------------------------------------------------

def name_dupes(rows: list[dict]) -> dict:
    """Names used in more than one user folder, and user names shared with factory."""
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        if r["kind"] == "user":
            by_name.setdefault(r["name"], []).append(r)
    groups = [{"name": n, "entries": sorted(es, key=lambda r: (r["folder"], r["position"]))}
              for n, es in by_name.items() if len({e["folder_key"] for e in es}) > 1]
    groups.sort(key=lambda g: g["name"].casefold())
    factory = {r["name"] for r in rows if r["kind"] == "factory"}
    overlap = sorted((n for n in by_name if n in factory), key=str.casefold)
    return {"groups": groups, "factory_overlap": overlap, "factory_checked": bool(factory)}


def content_dupes(mock: bool) -> dict:
    """Presets with identical sound-relevant content, from the latest backup."""
    latest = [n for n in backup.load_latest(mock)
              if not guard.is_sandbox(n.get("folder") or "")]
    if not latest:
        return {"available": False, "groups": [],
                "message": "バックアップがないため、中身の重複は調べられません。"
                           "バックアップ(プリセットを 1 つずつ読み込むので音が切り替わります)を"
                           "実行してから、もう一度開いてください。"}
    groups: dict[str, list[dict]] = {}
    for n in latest:
        groups.setdefault(n["fingerprint"], []).append(
            {"folder": guard.folder_name(n["folder"]), "folder_key": n["folder"],
             "position": n["position"], "slot": n["slot"], "name": n["name"],
             "file": n["_file"]})
    out = [{"fingerprint": fp, "entries": es} for fp, es in groups.items() if len(es) > 1]
    out.sort(key=lambda g: (-len(g["entries"]), g["entries"][0]["name"].casefold()))
    return {"available": True, "groups": out, "presets": len(latest),
            "commit": (backup.log(mock, limit=1) or [{}])[0].get("short")}


def cleanup_markdown(names: dict, contents: dict) -> str:
    """A cleanup PROPOSAL for the player to carry out on the unit (idea O)."""
    lines = ["# 重複の整理案(提案)", "",
             "qcsuite はプリセットを削除しません。下の案を見て、残すものを決め、"
             "整理は本体(または Cortex Control)で行ってください。", ""]
    lines += ["## 中身が同じプリセット", ""]
    if not contents.get("available"):
        lines.append(contents.get("message", "(バックアップがありません)"))
    elif not contents["groups"]:
        lines.append("(見つかりませんでした)")
    for g in contents.get("groups", []):
        keep = _keep_suggestion(g["entries"])
        lines.append(f"- 同じ中身 {len(g['entries'])} 件(指紋 {g['fingerprint']}):")
        for e in g["entries"]:
            mark = "残す案" if e is keep else "整理候補"
            lines.append(f"  - [{mark}] {e['folder']} {e['slot']}「{e['name']}」")
    lines += ["", "## 同じ名前が複数のフォルダにあるもの", "",
              "名前が同じでも中身が違うことがあります。中身が同じかは上の一覧で確認してください。", ""]
    for g in names["groups"]:
        where = "、".join(f"{e['folder']} {e['slot']}" for e in g["entries"])
        lines.append(f"- 「{g['name']}」: {where}")
    if names.get("factory_overlap"):
        lines += ["", "## 工場出荷のプリセットと同じ名前", ""]
        lines += [f"- 「{n}」" for n in names["factory_overlap"]]
    lines += ["", "_この案はモックで作成手順を確認済み・実機の内容では未確認です。_", ""]
    return "\n".join(lines)


def _keep_suggestion(entries: list[dict]) -> dict:
    """Prefer My Presets, then a name without a ``_N`` suffix, then the lowest slot."""
    def rank(e):
        suffixed = len(e["name"]) > 2 and e["name"][-2] == "_" and e["name"][-1].isdigit()
        return (e["folder"] != "My Presets", suffixed, e["position"])
    return min(entries, key=rank)


# -- copying ------------------------------------------------------------------------------

def parse_ref(ref: str) -> tuple[str, str]:
    """``"My Presets/1A"``, ``"kami/Henriksen"`` or ``"kami:1E"`` (the form the
    other commands use) -> (folder key, slot-or-name)."""
    if ":" in ref and "/" not in ref.rsplit(":", 1)[1]:
        folder, rest = ref.rsplit(":", 1)
        return guard.folder_key(folder), rest.strip()
    if "/" not in ref.strip("/"):
        raise QcsuiteError(f"「{ref}」は「フォルダ/1A」「フォルダ:1A」か「フォルダ/名前」で指定してください。")
    folder, rest = ref.rsplit("/", 1)
    return guard.folder_key(folder), rest


def resolve(device, ref: str) -> dict:
    """A reference to one listed preset -> ``{"folder", "position", "name", "slot"}``."""
    key, rest = parse_ref(ref)
    folder = ref.rsplit("/", 1)[0].strip()
    by_name = {f["name"].casefold(): f["key"] for f in device.setlists()}
    key = by_name.get(folder.casefold(), key)          # "Factory Library" is not a user folder
    entries = device.presets(key)
    try:
        position = pq.slot_to_position(rest)
        hit = next((e for e in entries if e["index"] == position), None)
    except ValueError:
        hit = next((e for e in entries if e["name"] == rest), None)
    if hit is None:
        raise QcsuiteError(f"「{ref}」が見つかりません。")
    return {"folder": key, "position": hit["index"], "name": hit["name"], "slot": hit["slot"]}


def copy_plan(device, items: list[dict], to: str | None = None,
              names: dict | None = None) -> plan_mod.Plan:
    """Copy ``items`` (``{"folder", "position"}``) into ``to`` (default sandbox).

    Destination slots are fixed now, at plan time: the first free ones in order.
    ``names`` maps ``"folder#position"`` to a new name.
    """
    if not items:
        raise QcsuiteError("コピーするプリセットを選んでください。")
    sandbox = guard.folder_key(guard.SANDBOX_NAME)
    target = guard.folder_key(to) if to else sandbox
    existing = {f["key"] for f in device.setlists()}
    steps, taken = [], {}
    if target not in existing:
        if target != sandbox:
            raise QcsuiteError(f"コピー先のフォルダ「{guard.folder_name(target)}」がありません。")
        steps.append(plan_mod.Step("create_folder", target, note="sandbox を作ります"))
    else:
        taken = {p["index"]: p["name"] for p in device.presets(target)}
    listed: dict[str, dict] = {}
    free = (i for i in range(256) if i not in taken)
    warnings = ["タグは引き継がれません(保存で運べるのは楽器の種類だけです)。",
                "コピーのたびに、元のプリセットが本体に読み込まれ、音が切り替わります。"]
    dest_names = set(taken.values())
    for item in items:
        key = guard.folder_key(item["folder"])
        if key not in listed:
            listed[key] = {p["index"]: p for p in device.presets(key)}
        source = listed[key].get(int(item["position"]))
        if source is None:
            raise QcsuiteError(f"「{guard.folder_name(key)}」"
                               f"{pq.position_to_slot(int(item['position']))} は空です。")
        position = next(free, None)
        if position is None:
            raise QcsuiteError(f"「{guard.folder_name(target)}」の空きスロットが足りません。")
        name = (names or {}).get(f"{key}#{source['index']}") or source["name"]
        if name in dest_names:
            warnings.append(f"「{name}」はコピー先にすでにあるため、本体が「{name[:18]}_1」のように"
                            "名前を変えて保存する見込みです(最大 20 文字 + _N)。")
        dest_names.add(name)
        steps.append(plan_mod.Step("copy", target, position=position, name=name,
                                   source={"folder": key, "position": source["index"]},
                                   note=f"元: 「{guard.folder_name(key)}」{source['slot']}"))
    count = sum(1 for s in steps if s.action == "copy")
    return plan_mod.Plan(f"コピー: {count} 件 →「{guard.folder_name(target)}」",
                         steps=steps, warnings=warnings)


# -- looking inside -------------------------------------------------------------------------

def _view(n: dict, source: str, **extra) -> dict:
    knobs = [{"block": presets.block_label(b), "model": b["model"],
              "knobs": presets.main_knobs(b, limit=12),
              "issues": inert.block_issues(b) + ([f"キャプチャー: {b['capture']}"]
                                                 if b.get("capture") else [])}
             for b in n["blocks"]]
    return {"source": source, "preset": n, "chain": presets.chain_text(n), "blocks": knobs,
            **extra}


def show(device, folder: str, position: int, mode: str = "backup") -> dict:
    """Chain and knobs of a preset.

    ``backup``: from the latest backup, no device access. ``load``: recall it on
    the unit (the sound changes). ``current``: the live grid, no side effects.
    """
    if mode == "current":
        n = presets.normalize(device.current_preset(), device.catalog)
        return _view(n, "current")
    key = guard.folder_key(folder)
    if mode == "backup":
        n = backup.latest_by_key(device.mock).get((key, int(position)))
        if n is None:
            raise QcsuiteError("このプリセットはバックアップにありません。「本体に読み込んで表示」"
                               "(音が切り替わります)か、バックアップを実行してください。")
        commit = (backup.log(device.mock, limit=1) or [{}])[0].get("short")
        return _view(n, "backup", commit=commit)
    if mode == "load":
        stored = device.read_stored(key, int(position))
        n = presets.normalize(stored, device.catalog, folder=key, position=int(position))
        return _view(n, "load", warning=LOAD_WARNING)
    raise QcsuiteError(f"表示の方法は backup / load / current のどれかです: {mode}")


def show_text(view: dict) -> str:
    n = view["preset"]
    where = f"{guard.folder_name(n['folder'])} {n['slot']}" if n.get("folder") else "現在のグリッド"
    src = {"backup": f"バックアップ {view.get('commit') or ''}", "load": "本体に読み込み",
           "current": "現在のグリッド"}[view["source"]]
    lines = [f"{where}「{n.get('name') or ''}」({src})", *view["chain"]]
    for b in view["blocks"]:
        knobs = ", ".join(f"{k} {v}" for k, v in b["knobs"])
        lines.append(f"  {b['block']}: {knobs}")
        lines.extend(f"    ! {i}" for i in b["issues"])
    labels = [f"{'ABCDEFGH'[i]}={l}" for i, l in enumerate(n.get("scene_labels", [])) if l]
    if labels:
        lines.append("シーン: " + " ".join(labels))
    return "\n".join(lines)


# -- captures, IRs, catalog (ideas J) ------------------------------------------------------

def search_captures(device, query: str = "", limit: int = 100) -> dict:
    q = query.strip().casefold()
    hits = [c for c in device.captures() if not q or q in c["name"].casefold()]
    return {"total": len(hits), "items": [{"kind": "capture", **c} for c in hits[:limit]]}


def search_irs(device, query: str = "", folder: str | None = None) -> dict:
    q = query.strip().casefold()
    hits = [i for i in device.irs(folder) if not q or q in i["name"].casefold()]
    return {"total": len(hits), "items": [{"kind": "ir", **i} for i in hits]}


def search_catalog(device, query: str = "", category: str = "") -> list[dict]:
    q, cat = query.strip().casefold(), category.strip().casefold()
    out = []
    for m in device.catalog:
        if m.hidden or m.internal or m.category_hidden:
            continue
        if cat and cat not in m.category.casefold():
            continue
        if q and q not in f"{m.name} {m.based_on}".casefold():
            continue
        out.append({"id": m.id, "name": m.name, "category": m.category,
                    "based_on": m.based_on, "plugin": getattr(m, "plugin_id", None) or None,
                    "inert_plugin": inert.inert_plugin(m), "superseded": m.superseded,
                    "params": [{"name": p.name, "range": [p.minimum, p.maximum],
                                "units": p.units, **({"options": p.option_count}
                                                     if p.option_count else {})}
                               for p in m.parameters]})
    out.sort(key=lambda m: (m["category"], m["name"].casefold()))
    return out


def candidates(items: list[dict], fmt: str = "md") -> str:
    """A shortlist of captures/IRs to load by hand on the unit."""
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["種類", "名前", "key", "メモ"])
        for i in items:
            w.writerow(["キャプチャー" if i.get("kind") == "capture" else "IR", i["name"],
                        i.get("key", ""), i.get("note", "")])
        return buf.getvalue()
    lines = ["# 本体で読み込む候補リスト", "",
             "パソコンから書き込んだキャプチャーや IR は、読み戻しは正しく見えても音に反映されません"
             "(実機で確認済みの事実)。本体の画面で、下の名前を検索して読み込んでください。", "",
             "| # | 種類 | 名前 | メモ |", "|---:|---|---|---|"]
    for n, i in enumerate(items, 1):
        kind = "キャプチャー" if i.get("kind") == "capture" else "IR"
        lines.append(f"| {n} | {kind} | {i['name']} | {i.get('note', '')} |")
    return "\n".join(lines) + "\n"


# -- folder usage (idea P) ---------------------------------------------------------------------

def usage(device) -> dict:
    folders = [f for f in device.setlists() if f["kind"] in ("user", "sandbox", "factory")]
    room = device.setlist_room()
    return {
        "folders": [{"name": f["name"], "key": f["key"], "occupied": f["occupied"],
                     "slots": f["slots"], "kind": f["kind"], "protected": f["protected"],
                     "free": f["slots"] - f["occupied"]} for f in folders],
        "user_setlists": room["user_folders"],
        "setlist_limit": room["limit"],
        "setlist_free": room["free"],
        "sandbox_exists": any(f["kind"] == "sandbox" for f in folders),
        "note": ("Neural DSP のサポート情報によると、カスタムのセットリスト(フォルダ)は最大 12 個です。"
                 f"My Presets を数に含むかは未確認のため、qcsuite は My Presets を含めて {room['limit']} "
                 "個と見なしています(実機テスト手順書で確認)。qcsuite の sandbox も 1 個分を使います。"),
    }


def usage_text(u: dict) -> str:
    lines = [f"{'フォルダ':<20} 使用/全体  空き"]
    for f in u["folders"]:
        mark = " 保護" if f["protected"] else (" sandbox" if f["kind"] == "sandbox" else "")
        mark = " 読み取りのみ" if f["kind"] == "factory" else mark
        lines.append(f"{f['name']:<20} {f['occupied']:>3}/{f['slots']}  {f['free']:>4}{mark}")
    lines.append(f"ユーザーのセットリスト: {u['user_setlists']} / {u['setlist_limit']}(想定)"
                 f"  残り {u['setlist_free']}")
    lines.append(u["note"])
    return "\n".join(lines)


# -- the unit's Favorites vs the "Favorite" folder (idea R) ---------------------------------

FAVORITE_FOLDER = "Favorite"


def favorites_compare(device, folder: str = FAVORITE_FOLDER) -> dict:
    """Compare the unit's Favorites (hearts) with the player's "Favorite" folder.

    Read only: nothing here adds or removes a heart. The two are different
    things on the unit - the hearts are a device-wide list of entries pointing
    into any folder, the folder is an ordinary setlist the player made - so
    they are matched by NAME, and each heart shows which folder it points at,
    because the same name can live in several folders.
    """
    hearts = device.favorites()
    key = guard.folder_key(folder)
    known = {f["key"] for f in device.setlists()}
    listed = device.presets(key) if key in known else []
    in_folder: dict[str, list[dict]] = {}
    for p in listed:
        in_folder.setdefault(p["name"], []).append(p)
    hearted: dict[str, list[dict]] = {}
    for h in hearts:
        hearted.setdefault(h["name"], []).append(h)

    def heart_rows(name):
        return [{"folder": h["folder_name"] or guard.folder_name(h["folder_key"]),
                 "folder_key": h["folder_key"].rstrip("/"), "is_factory": h["is_factory"],
                 "points_at_folder": h["folder_key"].rstrip("/") == key}
                for h in hearted[name]]

    both, hearts_only, folder_only = [], [], []
    for name in sorted(set(hearted) | set(in_folder), key=str.casefold):
        slots = [p["slot"] for p in in_folder.get(name, [])]
        if name in hearted and name in in_folder:
            both.append({"name": name, "slots": slots, "hearts": heart_rows(name)})
        elif name in hearted:
            hearts_only.append({"name": name, "hearts": heart_rows(name)})
        else:
            folder_only.append({"name": name, "slots": slots})
    return {"folder": folder, "folder_exists": key in known, "hearts": len(hearts),
            "folder_presets": len(listed), "both": both, "hearts_only": hearts_only,
            "folder_only": folder_only}


def favorites_markdown(c: dict) -> str:
    lines = [f"# お気に入り(ハート)と「{c['folder']}」フォルダの比較", "",
             f"本体のお気に入り {c['hearts']} 件 / 「{c['folder']}」フォルダ {c['folder_presets']} 件。"
             "名前で照合しています(同じ名前が別のフォルダにあることがあります)。"
             "qcsuite はハートを付け外ししません。", ""]
    if not c["folder_exists"]:
        lines += [f"「{c['folder']}」フォルダが本体にありません。", ""]

    def where(hearts):
        return "、".join(f"{h['folder']}" + ("(このフォルダ)" if h["points_at_folder"] else "")
                         for h in hearts)

    lines += ["## 両方にある", ""]
    lines += [f"- 「{e['name']}」 フォルダ {'/'.join(e['slots'])} / ハートの指す先: {where(e['hearts'])}"
              for e in c["both"]] or ["(なし)"]
    lines += ["", "## ハートだけ(フォルダにない)", ""]
    lines += [f"- 「{e['name']}」 ハートの指す先: {where(e['hearts'])}"
              for e in c["hearts_only"]] or ["(なし)"]
    lines += ["", f"## 「{c['folder']}」フォルダだけ(ハートなし)", ""]
    lines += [f"- 「{e['name']}」 {'/'.join(e['slots'])}" for e in c["folder_only"]] or ["(なし)"]
    return "\n".join(lines) + "\n"


# -- CLI ------------------------------------------------------------------------------------------

def _rows_text(rows: list[dict]) -> str:
    if not rows:
        return "(該当なし)"
    return "\n".join(f"{r['folder']:<18} {r['slot']:>4}  {r['name']}"
                     + ("  [保護]" if r["protected"] else "") for r in rows) + \
        f"\n({len(rows)} 件)"


def cmd_list(ctx, a):
    with ctx.rig("library") as rig:
        rows = search(index(rig.device, a.include_factory), "", a.folder, a.sort)
    ctx.out(rows, _rows_text(rows))


def cmd_search(ctx, a):
    with ctx.rig("library") as rig:
        rows = search(index(rig.device, a.include_factory), a.query, a.folder, a.sort)
    ctx.out(rows, _rows_text(rows))


def cmd_dupes(ctx, a):
    with ctx.rig("library") as rig:
        names = name_dupes(index(rig.device, include_factory=True))
    contents = content_dupes(ctx.mock)
    md = cleanup_markdown(names, contents)
    if a.markdown:
        with open(a.markdown, "w") as f:
            f.write(md)
        print(f"整理案を書き出しました: {a.markdown}", file=sys.stderr)
    ctx.out({"names": names, "contents": contents}, md)


def cmd_copy(ctx, a):
    with ctx.rig("library copy") as rig:
        items = [resolve(rig.device, r) for r in a.refs]
        names = {f"{items[0]['folder']}#{items[0]['position']}": a.rename} \
            if a.rename and len(items) == 1 else None
        if a.rename and len(items) != 1:
            raise QcsuiteError("--rename は 1 件をコピーするときだけ使えます。")
        plan = copy_plan(rig.device, items, a.to, names)
        result = ctx.run_plan(plan, rig.device, apply=a.apply)
    stored = [r.get("stored_name") for r in result.get("results", []) if r.get("stored_name")]
    ctx.out(result, "" if result.get("dry_run") else "コピーしました: " + "、".join(stored))


def cmd_show(ctx, a):
    with ctx.rig("library show") as rig:
        if a.current:
            view = show(rig.device, "", 0, "current")
        else:
            ref = resolve(rig.device, a.ref) if a.ref else None
            if ref is None:
                raise QcsuiteError("プリセットを「フォルダ/1A」で指定するか、--current を付けてください。")
            if a.load:
                print(f"注意: {LOAD_WARNING}", file=sys.stderr)
                if not ctx.ask("読み込みますか?"):
                    return
            view = show(rig.device, ref["folder"], ref["position"], "load" if a.load else "backup")
    ctx.out(view, show_text(view))


def _export(ctx, items, path, fmt):
    text = candidates(items, fmt)
    with open(path, "w", newline="" if fmt == "csv" else None) as f:
        f.write(text)
    print(f"候補リストを書き出しました: {path}", file=sys.stderr)


def cmd_captures(ctx, a):
    with ctx.rig("library captures") as rig:
        found = search_captures(rig.device, a.query or "", a.limit)
    if a.export:
        _export(ctx, found["items"], a.export, "csv" if a.export.endswith(".csv") else "md")
    ctx.out(found, "\n".join(c["name"] for c in found["items"])
            + f"\n({found['total']} 件中 {len(found['items'])} 件を表示)")


def cmd_irs(ctx, a):
    with ctx.rig("library irs") as rig:
        found = search_irs(rig.device, a.query or "", a.folder)
    if a.export:
        _export(ctx, found["items"], a.export, "csv" if a.export.endswith(".csv") else "md")
    ctx.out(found, "\n".join(i["name"] for i in found["items"]) or "(該当なし)")


def cmd_catalog(ctx, a):
    with ctx.rig("library catalog") as rig:
        models = search_catalog(rig.device, a.search or "", a.category or "")
    text = "\n".join(f"{m['id']:>6}  {m['category']:<22} {m['name']}"
                     + (f"  [未所有プラグイン: 素通し]" if m["inert_plugin"] else "")
                     + ("  (旧版)" if m["superseded"] else "") for m in models)
    ctx.out(models, text or "(該当なし)")


def cmd_usage(ctx, a):
    with ctx.rig("library usage") as rig:
        u = usage(rig.device)
    ctx.out(u, usage_text(u))


def cmd_favorites(ctx, a):
    with ctx.rig("library favorites") as rig:
        c = favorites_compare(rig.device, a.folder)
    md = favorites_markdown(c)
    if a.markdown:
        with open(a.markdown, "w") as f:
            f.write(md)
        print(f"比較を書き出しました: {a.markdown}", file=sys.stderr)
    ctx.out(c, md)


def add_cli(sub):
    p = sub.add_parser("library", help="プリセット管理(一覧・検索・重複・コピー・中身・キャプチャー検索)")
    s = p.add_subparsers(dest="library_cmd", required=True)

    def common(c, query=False):
        if query:
            c.add_argument("query")
        c.add_argument("--folder", help="このフォルダだけ")
        c.add_argument("--include-factory", action="store_true", help="Factory Library も含める")
        c.add_argument("--sort", choices=SORTS, default="folder")

    c = s.add_parser("list", help="全フォルダのプリセット一覧(本体の音は変わらない)")
    common(c)
    c.set_defaults(fn=cmd_list)
    c = s.add_parser("search", help="名前で検索")
    common(c, query=True)
    c.set_defaults(fn=cmd_search)
    c = s.add_parser("dupes", help="名前と中身の重複、整理案(Markdown)")
    c.add_argument("--markdown", metavar="FILE", help="整理案をファイルに書き出す")
    c.set_defaults(fn=cmd_dupes)
    c = s.add_parser("copy", help="プリセットを別フォルダへコピー(計画を表示、--apply で実行)")
    c.add_argument("refs", nargs="+", help="「フォルダ/1A」か「フォルダ/名前」")
    c.add_argument("--to", help="コピー先(既定: sandbox。他は --allow-folder が必要)")
    c.add_argument("--rename", help="新しい名前(1 件のときだけ)")
    c.add_argument("--apply", action="store_true", help="実行する(省略時は計画の表示だけ)")
    c.set_defaults(fn=cmd_copy)
    c = s.add_parser("show", help="チェーンとノブを表示(既定はバックアップから)")
    c.add_argument("ref", nargs="?", help="「フォルダ/1A」か「フォルダ/名前」")
    c.add_argument("--load", action="store_true", help="本体に読み込んで表示(音が切り替わる)")
    c.add_argument("--current", action="store_true", help="現在のグリッドを表示(副作用なし)")
    c.set_defaults(fn=cmd_show)
    c = s.add_parser("captures", help="キャプチャーを検索(本体で読み込む候補リストを作れる)")
    c.add_argument("query", nargs="?")
    c.add_argument("--limit", type=int, default=100)
    c.add_argument("--export", metavar="FILE", help="候補リストを .md / .csv に書き出す")
    c.set_defaults(fn=cmd_captures)
    c = s.add_parser("irs", help="IR を検索")
    c.add_argument("query", nargs="?")
    c.add_argument("--folder", help="例: 2_q(My IRs)")
    c.add_argument("--export", metavar="FILE")
    c.set_defaults(fn=cmd_irs)
    c = s.add_parser("catalog", help="ブロックの辞典(モデルとパラメーター)")
    c.add_argument("--search")
    c.add_argument("--category")
    c.set_defaults(fn=cmd_catalog)
    c = s.add_parser("usage", help="フォルダの使用状況とセットリストの上限")
    c.set_defaults(fn=cmd_usage)
    c = s.add_parser("favorites", help="本体のお気に入り(ハート)と Favorite フォルダを比べる(読むだけ)")
    c.add_argument("--folder", default=FAVORITE_FOLDER, help="比べるフォルダ(既定: Favorite)")
    c.add_argument("--markdown", metavar="FILE", help="比較を Markdown で書き出す")
    c.set_defaults(fn=cmd_favorites)
