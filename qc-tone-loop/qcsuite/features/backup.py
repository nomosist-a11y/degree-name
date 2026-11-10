"""Backup and change history (feature 2): every preset as JSON, kept in git.

A backup reads each preset in the chosen folders, normalizes it
(:func:`qcsuite.presets.normalize`) and writes one JSON file per slot into a git
repository of its own - ``~/.qctone/backup`` (``~/.qctone/mock/backup`` for the
fake unit), never the project repository::

    backup/
      My Presets/000_1A_Friedman BE50 X v.json
      kami/004_1E_Brit 2203.json
      _listings/kami.json           the folder listing at backup time
      index.json                    folder, slot, name, file, fingerprint per preset

**Reading a stored preset RECALLS it** (pyquadcortex ``read_preset``): the unit
switches to that preset, its sound changes, and unsaved edits on the unit are
lost. A full backup therefore walks the unit through every preset - about 10 to
25 s each on hardware per the library's docstring, so roughly half an hour for
the player's 107 user presets. It runs as a resumable job and commits once, at
the end; an interrupted run picks up where it stopped (``--resume``).

Restoring never writes back where a preset came from. It rebuilds the preset
on the live grid from JSON (:mod:`qcsuite.rebuild`) and saves it into the
sandbox (or a folder the player allowed), as a plan. What a rebuild cannot
bring back - captures, cab mics and IRs that do not sound when written from the
computer, unowned plugins, tags - is listed before anything runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pyquadcortex as pq

from qcsuite import jobs, paths, presets
from qcsuite import plan as plan_mod
from qcsuite import rebuild
from qcsuite.audio.export import safe_name
from qcsuite.device import guard
from qcsuite.errors import PlanError, QcsuiteError, WriteRefused

RECALL_WARNING = ("バックアップはプリセットを 1 つずつ本体に読み込みます。そのたびに本体の音が切り替わり、"
                  "本体で保存していない編集は消えます。実機では 1 件 10〜25 秒ほどかかります。")
INDEX = "index.json"
LISTINGS = "_listings"
#: The job's last item: writing the index and committing. Being an item, it
#: runs only after every preset was handled, and a resumed job still runs it.
COMMIT_KEY = "__commit__"


# -- the repository -------------------------------------------------------------

def backup_dir(mock: bool) -> Path:
    return paths.home(mock) / "backup"


def _git(repo: Path, *args: str, input_bytes: bytes | None = None, text: bool = True):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                            input=input_bytes if not text else None, text=text)
    if result.returncode != 0:
        err = result.stderr if text else result.stderr.decode(errors="replace")
        raise QcsuiteError(f"git {' '.join(args[:2])} に失敗しました: {err.strip()}")
    return result.stdout


def ensure_repo(mock: bool) -> Path:
    """The backup repository, created (``git init``) on first use."""
    repo = paths.ensure(backup_dir(mock))
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", "qcsuite")
        _git(repo, "config", "user.email", "qcsuite@localhost")
        _git(repo, "config", "commit.gpgsign", "false")
    return repo


def has_commits(mock: bool) -> bool:
    repo = backup_dir(mock)
    if not (repo / ".git").exists():
        return False
    result = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo,
                            capture_output=True, text=True)
    return result.returncode == 0


def preset_file(folder_key: str, position: int, name: str) -> str:
    """Repository-relative path of one preset's JSON."""
    return (f"{safe_name(guard.folder_name(folder_key))}/"
            f"{position:03d}_{pq.position_to_slot(position)}_{safe_name(name)}.json")


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# -- running a backup -------------------------------------------------------------

def default_folders(device, include_factory: bool = False) -> list[str]:
    out = []
    for f in device.setlists():
        if f["kind"] == "user" or (include_factory and f["kind"] == "factory"):
            out.append(f["key"])
    return out


def create_job(device, folders: list[str] | None, include_factory: bool, mock: bool) -> jobs.Job:
    """Fix the work list (one item per occupied slot) and the listings now."""
    keys = [guard.folder_key(f) for f in folders] if folders else \
        default_folders(device, include_factory)
    known = {f["key"] for f in device.setlists()}
    missing = [k for k in keys if k not in known]
    if missing:
        raise QcsuiteError("見つからないフォルダ: " + ", ".join(guard.folder_name(k) for k in missing))
    items, listings = [], {}
    for key in keys:
        entries = device.presets(key)
        listings[key] = entries
        for e in entries:
            items.append({"key": f"{key}#{e['index']}", "folder": key, "position": e["index"],
                          "slot": e["slot"], "name": e["name"],
                          "file": preset_file(key, e["index"], e["name"])})
    items.append({"key": COMMIT_KEY, "commit": True})
    title = f"バックアップ: {', '.join(guard.folder_name(k) for k in keys)}"
    return jobs.create("backup", {"folders": keys, "listings": listings}, items, mock,
                       title=title)


def run_job(device, job: jobs.Job, cancel=None, on_progress=None) -> dict:
    """Read every preset of ``job`` (recalling each), then commit once."""
    repo = ensure_repo(device.mock)
    catalog = device.catalog

    def work(item):
        if item.get("commit"):
            commit = finalize(job, device.mock)
            job.set_state(commit=commit)
            return {"commit": commit}
        stored = device.read_stored(item["folder"], item["position"])
        n = presets.normalize(stored, catalog, folder=item["folder"], position=item["position"])
        path = repo / item["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(n))
        return {"file": item["file"], "fingerprint": n["fingerprint"], "name": n["name"]}

    summary = jobs.run(job, work, cancel=cancel, on_progress=on_progress)
    commit = job.state().get("commit")
    failed = summary.get("failed") or 0
    job.set_state(message=(f"コミット {commit[:8]}" if commit else "変更なし(コミットなし)")
                  + (f" / 失敗 {failed} 件(results.jsonl を参照)" if failed else ""))
    saved = sum(1 for r in job.results() if r.get("ok") and r["key"] != COMMIT_KEY)
    return {"job": job.summary(), "commit": commit, "repo": str(repo),
            "files": saved, "failed": failed}


def finalize(job: jobs.Job, mock: bool) -> str | None:
    """Drop files of presets that are gone, write index and listings, commit."""
    repo = ensure_repo(mock)
    spec = job.spec
    items = [i for i in job.items if not i.get("commit")]
    expected = {i["file"] for i in items}
    for key in spec["folders"]:
        folder = repo / safe_name(guard.folder_name(key))
        if folder.is_dir():
            for path in folder.glob("*.json"):
                rel = f"{folder.name}/{path.name}"
                if rel not in expected:
                    path.unlink()
        listing = repo / LISTINGS / f"{safe_name(guard.folder_name(key))}.json"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text(_dump(spec["listings"][key]))
    (repo / INDEX).write_text(_dump(build_index(repo)))
    _git(repo, "add", "-A")
    if not _git(repo, "status", "--porcelain").strip():
        return None
    failed = sum(1 for r in job.results() if not r.get("ok"))
    folders = ", ".join(guard.folder_name(k) for k in spec["folders"])
    message = (f"backup {time.strftime('%Y-%m-%d %H:%M')}: {len(items)} presets ({folders})"
               + (f", {failed} failed" if failed else ""))
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def build_index(repo: Path) -> list[dict]:
    out = []
    for n, rel in _iter_working(repo):
        out.append({"folder": n.get("folder"), "position": n.get("position"),
                    "slot": n.get("slot"), "name": n.get("name"), "file": rel,
                    "fingerprint": n.get("fingerprint")})
    return out


def _iter_working(repo: Path):
    if not repo.is_dir():
        return
    for path in sorted(repo.glob("*/*.json")):
        if path.parent.name == LISTINGS:
            continue
        n = json.loads(path.read_text())
        if n.get("schema") == presets.SCHEMA:
            yield n, f"{path.parent.name}/{path.name}"


def load_latest(mock: bool) -> list[dict]:
    """Every preset of the backup's working tree (normalized); [] if none.

    For other features (library, sheets, health) that want preset contents
    without recalling anything on the unit.
    """
    out = []
    for n, rel in _iter_working(backup_dir(mock)):
        n = dict(n)
        n["_file"] = rel
        out.append(n)
    out.sort(key=lambda n: (n.get("folder") or "", n.get("position") or 0))
    return out


def latest_by_key(mock: bool) -> dict:
    """``{(folder_key, position): normalized preset}`` from the latest backup."""
    return {(n["folder"], n["position"]): n for n in load_latest(mock)}


def status(mock: bool) -> dict:
    repo = backup_dir(mock)
    latest = load_latest(mock)
    last = log(mock, limit=1)
    folders: dict[str, int] = {}
    for n in latest:
        name = guard.folder_name(n["folder"] or "?")
        folders[name] = folders.get(name, 0) + 1
    backups = [j for j in jobs.listing(mock) if j.get("kind") == "backup"]
    return {"repo": str(repo), "presets": len(latest), "folders": folders,
            "last_commit": last[0] if last else None,
            "last_job": backups[0] if backups else None,
            "recall_warning": RECALL_WARNING}


# -- history ----------------------------------------------------------------------

def log(mock: bool, file: str | None = None, limit: int = 50) -> list[dict]:
    if not has_commits(mock):
        return []
    args = ["log", f"-n{limit}", "--date=format:%Y-%m-%d %H:%M:%S",
            "--format=%H%x1f%ad%x1f%s"]
    if file:
        args += ["--", file]
    out = []
    for line in _git(backup_dir(mock), *args).splitlines():
        commit, date, subject = line.split("\x1f", 2)
        out.append({"commit": commit, "short": commit[:8], "date": date, "message": subject})
    return out


def _resolve_commit(mock: bool, rev: str) -> str:
    return _git(backup_dir(mock), "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def files_at(mock: bool, rev: str | None) -> dict[str, dict]:
    """``{file: normalized}`` at commit ``rev``; ``None`` = the working tree."""
    repo = backup_dir(mock)
    if rev is None:
        return {rel: n for n, rel in _iter_working(repo)}
    commit = _resolve_commit(mock, rev)
    names = [n for n in _git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()
             if n.endswith(".json") and "/" in n and not n.startswith(LISTINGS + "/")]
    if not names:
        return {}
    request = "".join(f"{commit}:{n}\n" for n in names).encode()
    raw = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, input=request,
                         capture_output=True, check=True).stdout
    out, pos = {}, 0
    for name in names:
        header_end = raw.index(b"\n", pos)
        _, kind, size = raw[pos:header_end].decode().split(" ")
        start = header_end + 1
        body = raw[start:start + int(size)]
        pos = start + int(size) + 1
        if kind == "blob":
            n = json.loads(body.decode())
            if n.get("schema") == presets.SCHEMA:
                out[name] = n
    return out


def _by_slot(files: dict[str, dict]) -> dict:
    return {(n["folder"], n["position"]): (rel, n) for rel, n in files.items()}


def diff(mock: bool, a: str | None = None, b: str | None = None,
         ref: str | None = None) -> dict:
    """Readable differences between two backups.

    No revisions: the last two commits (or the only commit against the working
    tree). ``a`` alone: ``a`` against the working tree. ``ref`` limits the
    comparison to one preset (see :func:`find`).
    """
    if not has_commits(mock):
        raise QcsuiteError("バックアップの履歴がまだありません。先に `qcsuite backup run` を実行してください。")
    if a is None and b is None:
        commits = log(mock, limit=2)
        a, b = (commits[1]["commit"], commits[0]["commit"]) if len(commits) == 2 \
            else (commits[0]["commit"], None)
    old, new = _by_slot(files_at(mock, a)), _by_slot(files_at(mock, b))
    keys = sorted(set(old) | set(new), key=lambda k: (k[0] or "", k[1] or 0))
    if ref is not None:
        wanted = _match_key(ref, {**old, **new})
        keys = [k for k in keys if k == wanted]
    out = []
    for key in keys:
        before, after = old.get(key), new.get(key)
        label = f"{guard.folder_name(key[0])} {pq.position_to_slot(key[1])}"
        if before is None:
            out.append({"slot": label, "kind": "added", "name": after[1]["name"],
                        "file": after[0], "changes": [], "text": "[追加]"})
            continue
        if after is None:
            out.append({"slot": label, "kind": "removed", "name": before[1]["name"],
                        "file": before[0], "changes": [], "text": "[削除]"})
            continue
        changes = presets.diff(before[1], after[1])
        if changes:
            out.append({"slot": label, "kind": "changed", "name": after[1]["name"],
                        "file": after[0], "changes": changes,
                        "text": presets.diff_text(changes)})
    repo = backup_dir(mock)
    stat_args = ["diff", "--stat", _resolve_commit(mock, a)] + \
        ([_resolve_commit(mock, b)] if b else [])
    return {"from": a, "to": b or "working", "presets": out,
            "stat": _git(repo, *stat_args).strip()}


def diff_report(d: dict) -> str:
    lines = [f"{d['from'][:8]} → {d['to'][:8] if d['to'] != 'working' else '作業中'}"]
    if not d["presets"]:
        lines.append("(違いはありません)")
    for p in d["presets"]:
        lines.append(f"■ {p['slot']}「{p['name']}」 {p['text'] if p['kind'] != 'changed' else ''}")
        if p["kind"] == "changed":
            lines.extend("  " + line for line in p["text"].splitlines())
    return "\n".join(lines)


def _match_key(ref: str, by_slot: dict):
    """A ``Folder/1A``, ``Folder/Name`` or file reference -> (folder_key, position)."""
    for key, (rel, n) in by_slot.items():
        if ref == rel:
            return key
    if ":" in ref and "/" not in ref.rsplit(":", 1)[1]:
        folder, rest = ref.rsplit(":", 1)          # "kami:1E", as the other commands take
    elif "/" in ref:
        folder, rest = ref.rsplit("/", 1)
    else:
        raise QcsuiteError(f"「{ref}」の指定が分かりません。「フォルダ/1A」「フォルダ:1A」"
                           "「フォルダ/名前」かファイル名で指定してください。")
    folder_key = guard.folder_key(folder)
    try:
        position = pq.slot_to_position(rest)
    except ValueError:
        position = None
    for key, (rel, n) in by_slot.items():
        if key[0] != folder_key and guard.folder_name(key[0]).casefold() != folder.casefold():
            continue
        if position is not None and key[1] == position:
            return key
        if position is None and n["name"] == rest:
            return key
    raise QcsuiteError(f"バックアップに「{ref}」が見つかりません。")


def find(mock: bool, ref: str, commit: str | None = None) -> tuple[str, dict]:
    """One preset from a backup: ``(file, normalized)``."""
    files = files_at(mock, commit)
    if not files:
        raise QcsuiteError("バックアップがありません。先に `qcsuite backup run` を実行してください。")
    by_slot = _by_slot(files)
    rel, n = by_slot[_match_key(ref, by_slot)]
    return rel, n


def preset_log(mock: bool, ref: str) -> list[dict]:
    rel, _ = find(mock, ref)
    return log(mock, file=rel)


# -- restore ------------------------------------------------------------------------

def restore_plan(device, normalized: dict, to: str | None = None, slot: str | None = None,
                 name: str | None = None) -> plan_mod.Plan:
    """A plan that rebuilds ``normalized`` and saves it into the sandbox (or ``to``).

    Refuses the preset's own folder: a restore never overwrites the original.
    """
    sandbox = guard.folder_key(guard.SANDBOX_NAME)
    target = guard.folder_key(to) if to else sandbox
    if normalized.get("folder") and guard.folder_key(normalized["folder"]) == target:
        raise WriteRefused("元のフォルダには復元できません。sandbox か、許可した別のフォルダを選んでください。")
    existing = {f["key"] for f in device.setlists()}
    steps = []
    taken: dict[int, str] = {}
    if target not in existing:
        if target != sandbox:
            raise QcsuiteError(f"フォルダ「{guard.folder_name(target)}」がありません。")
        steps.append(plan_mod.Step("create_folder", target, note="sandbox を作ります"))
    else:
        taken = {p["index"]: p["name"] for p in device.presets(target)}
    if slot:
        position = pq.slot_to_position(slot)
    else:
        position = next((i for i in range(256) if i not in taken), None)
        if position is None:
            raise PlanError(f"「{guard.folder_name(target)}」に空きスロットがありません。")
    title = name or normalized.get("name") or "restored"
    steps.append(plan_mod.Step("rebuild", target, position=position, name=title,
                               overwrites=taken.get(position), payload=normalized,
                               note=f"元: 「{guard.folder_name(normalized.get('folder') or '?')}」"
                                    f"{normalized.get('slot') or ''}"))
    _, _, warnings = rebuild.ops_for(normalized, {"blocks": []})
    warnings.append("同じ名前があると、本体が名前を 20 文字に切り詰めて _N を付けることがあります。")
    return plan_mod.Plan(f"復元: 「{normalized.get('name')}」→「{guard.folder_name(target)}」",
                         steps=steps, warnings=warnings)


# -- CLI --------------------------------------------------------------------------------

def _progress(done, total, key):
    label = "索引を書いてコミット" if key == COMMIT_KEY else key.replace("#", " #")
    print(f"  [{done + 1}/{total}] {label}", file=sys.stderr)


def cmd_run(ctx, a):
    print(f"注意: {RECALL_WARNING}", file=sys.stderr)
    if not a.resume and not ctx.ask("バックアップを始めますか?"):
        return
    folders = [f.strip() for f in a.folders.split(",")] if a.folders else None
    with ctx.rig("backup") as rig:
        job = jobs.open_job(a.resume, ctx.mock) if a.resume else \
            create_job(rig.device, folders, a.include_factory, ctx.mock)
        print(f"ジョブ {job.id}(中断したら --resume {job.id} で再開)", file=sys.stderr)
        result = run_job(rig.device, job, on_progress=_progress)
    commit = result["commit"]
    ctx.out(result, f"{result['files']} 件を保存しました。"
                    + (f"コミット {commit[:8]}" if commit else "前回から変更はありません。")
                    + f"\n保存先: {result['repo']}")


def cmd_status(ctx, a):
    s = status(ctx.mock)
    text = [f"保存先: {s['repo']}", f"プリセット: {s['presets']} 件"]
    text += [f"  {k}: {v}" for k, v in s["folders"].items()]
    if s["last_commit"]:
        text.append(f"最新: {s['last_commit']['date']} {s['last_commit']['short']} "
                    f"{s['last_commit']['message']}")
    else:
        text.append("まだバックアップがありません(qcsuite backup run)。")
    ctx.out(s, "\n".join(text))


def cmd_log(ctx, a):
    rows = preset_log(ctx.mock, a.preset) if a.preset else log(ctx.mock, limit=a.limit)
    ctx.out(rows, "\n".join(f"{r['short']}  {r['date']}  {r['message']}" for r in rows)
            or "(履歴はありません)")


def cmd_diff(ctx, a):
    d = diff(ctx.mock, a.a, a.b, a.preset)
    ctx.out(d, diff_report(d) + ("\n\n" + d["stat"] if a.stat and d["stat"] else ""))


def cmd_show(ctx, a):
    rel, n = find(ctx.mock, a.ref, a.commit)
    lines = [f"{guard.folder_name(n['folder'])} {n['slot']}「{n['name']}」({rel})"]
    lines += presets.chain_text(n)
    for b in n["blocks"]:
        knobs = ", ".join(f"{k} {v}" for k, v in presets.main_knobs(b))
        lines.append(f"  {presets.block_label(b)}: {knobs}")
    ctx.out(n, "\n".join(lines))


def cmd_restore(ctx, a):
    _, n = find(ctx.mock, a.ref, a.commit)
    with ctx.rig("backup restore") as rig:
        plan = restore_plan(rig.device, n, to=a.to, slot=a.slot, name=a.name)
        result = ctx.run_plan(plan, rig.device, apply=a.apply)
    ctx.out(result, "" if result.get("dry_run") else "復元しました。")


def add_cli(sub):
    p = sub.add_parser("backup", help="バックアップと変更履歴(JSON + git)、sandbox への復元")
    s = p.add_subparsers(dest="backup_cmd", required=True)
    c = s.add_parser("run", help="プリセットを読み込んで JSON に書き出し、git にコミット(音が切り替わる)")
    c.add_argument("--folders", help="カンマ区切りのフォルダ名(既定: ユーザーフォルダすべて)")
    c.add_argument("--include-factory", action="store_true", help="Factory Library も含める")
    c.add_argument("--resume", metavar="JOB_ID", help="中断したバックアップを再開")
    c.set_defaults(fn=cmd_run)
    c = s.add_parser("status", help="最新のバックアップの概要")
    c.set_defaults(fn=cmd_status)
    c = s.add_parser("log", help="履歴(コミット)の一覧")
    c.add_argument("--preset", help="このプリセットの履歴だけ(フォルダ/1A かフォルダ/名前)")
    c.add_argument("--limit", type=int, default=50)
    c.set_defaults(fn=cmd_log)
    c = s.add_parser("diff", help="2 つのバックアップの差分(既定: 直近の 2 回)")
    c.add_argument("a", nargs="?", help="比べる元のコミット")
    c.add_argument("b", nargs="?", help="比べる先のコミット(省略: 作業中のファイル)")
    c.add_argument("--preset", help="1 つのプリセットだけ")
    c.add_argument("--stat", action="store_true", help="git diff --stat も表示")
    c.set_defaults(fn=cmd_diff)
    c = s.add_parser("show", help="バックアップの 1 件を表示(本体に読み込まない)")
    c.add_argument("ref", help="フォルダ/1A、フォルダ/名前、またはファイル名")
    c.add_argument("--commit")
    c.set_defaults(fn=cmd_show)
    c = s.add_parser("restore", help="バックアップから sandbox へ復元(元のフォルダには書かない)")
    c.add_argument("ref")
    c.add_argument("--commit", help="この時点のバックアップから(省略: 最新)")
    c.add_argument("--to", help="復元先フォルダ(既定: sandbox。他は --allow-folder が必要)")
    c.add_argument("--slot", help="復元先のスロット(例: 3A。省略: 最初の空き)")
    c.add_argument("--name", help="保存する名前(省略: 元の名前)")
    c.add_argument("--apply", action="store_true", help="実行する(省略時は計画の表示だけ)")
    c.set_defaults(fn=cmd_restore)
