"""Blind comparison (feature 4): equal loudness, hidden names, the player's votes.

Comparing tones by ear is unfair when one is louder or when you know which is
which. This feature removes both: every candidate is loudness-matched
(:func:`qcsuite.audio.export.match_loudness`), renamed to a neutral code, and
presented two at a time as "A" and "B" in random order. The player votes A, B
or "同じくらい"; the votes are kept in a JSONL log and summarised as Elo
ratings. Nothing here picks a winner - the standings are only the tally of the
player's own votes, and the real names appear when the player asks to reveal
them.

Candidates come from anywhere that produced audio: rendered presets
(:func:`create_from_presets`, which drops renders that are the DI coming back
or hold blocks that do not sound from the host), sweep results, or plain files.

Formats:

* ``swiss`` (default): rounds in which everyone plays someone with a close
  rating whom they have not met yet; odd counts give one candidate a bye.
* ``knockout``: a bracket; a tie is not accepted, because someone must go on.

Optional hidden consistency checks present one candidate against itself (two
separately written copies, so the page cannot tell). "同じくらい" is the
consistent answer; the share is reported at the end as a hint about how
reliable the session's choices were - never used to change the ratings.

Storage, under ``<home>/tournaments/<id>/``::

    tournament.json   public: codes, audio paths, format, settings
    secret.json       code -> real name, source file, meta, gain (hidden)
    rounds.json       pairings made so far
    votes.jsonl       one line per vote
    state.json        the pair on screen, revealed or not
"""

from __future__ import annotations

import json
import math
import random
import shutil
import sys
import time
import uuid
from pathlib import Path

from qcsuite import jobs, paths
from qcsuite.audio import di as di_mod
from qcsuite.audio import export, lufs, reamp
from qcsuite.device import guard
from qcsuite.errors import QcsuiteError
from qctone import features

INITIAL_RATING = 1500.0
DEFAULT_K = 32.0
FORMATS = ("swiss", "knockout")
CHOICES = {"1": "a", "a": "a", "A": "a", "2": "b", "b": "b", "B": "b",
           "0": "tie", "tie": "tie", "=": "tie"}
RENDER_KIND = "tournament-render"


# -- storage ---------------------------------------------------------------------

def root(mock: bool) -> Path:
    return paths.sub("tournaments", mock)


def _dir(tid: str, mock: bool) -> Path:
    folder = root(mock) / tid
    if not (folder / "tournament.json").exists():
        raise FileNotFoundError(f"比較 {tid} が見つかりません。")
    return folder


def _read(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def _write(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def _rel(path: Path, mock: bool) -> str:
    return str(Path(path).resolve().relative_to(paths.home(mock).resolve()))


def _codes(n: int) -> list[str]:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    for i in range(n):
        code = ""
        j = i
        while True:
            code = letters[j % 26] + code
            j = j // 26 - 1
            if j < 0:
                break
        out.append(f"候補 {code}")
    return out


def _load_audio(path: str):
    data, sr = features.load(path)
    return data, sr


# -- creating --------------------------------------------------------------------

def create(candidates: list[dict], *, mock: bool, title: str, source: str,
           di: str | None = None, target_lufs: float | None = None,
           format: str = "swiss", rounds: int | None = None, k: float = DEFAULT_K,
           consistency_checks: int = 0, seed: int | None = None) -> dict:
    """Make a blind comparison from rendered audio.

    ``candidates``: ``[{"id", "label" (real name, hidden), "file", "meta"}]``.
    All are loudness-matched to ``target_lufs`` (default: the quietest one, so
    nothing is boosted into clipping) and written under neutral codes. Returns
    the public view (no labels).
    """
    if format not in FORMATS:
        raise ValueError(f"形式は {', '.join(FORMATS)} のどれかです: {format}")
    if len(candidates) < 2:
        raise ValueError("比べるには 2 つ以上の候補が必要です。")
    ids = [c["id"] for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("候補の id が重複しています。")
    tid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    rng = random.Random(seed if seed is not None else tid)
    order = list(candidates)
    rng.shuffle(order)
    audio, rates = [], set()
    for c in order:
        data, sr = _load_audio(c["file"])
        audio.append(data)
        rates.add(sr)
    sr = rates.pop()
    matched, target, gains = export.match_loudness(audio, sr, target_lufs)
    folder = paths.ensure(root(mock) / tid)
    audio_dir = paths.ensure(folder / "audio")
    public, secret = [], {}
    for code, c, x, gain in zip(_codes(len(order)), order, matched, gains):
        token = uuid.uuid4().hex[:10]           # file names say nothing about the candidate
        path = export.write(audio_dir / token, x, sr, "flac")
        public.append({"code": code, "audio": _rel(path, mock)})
        secret[code] = {"id": c["id"], "label": c.get("label", c["id"]), "file": c["file"],
                        "meta": c.get("meta", {}), "gain_db": round(float(gain), 2),
                        "lufs_matched": lufs.report(x, sr)["lufs"]}
    n = len(public)
    if rounds is None:
        rounds = (math.ceil(math.log2(n)) + 1) if format == "swiss" else math.ceil(math.log2(n))
    t = {"id": tid, "title": title, "source": source, "di": di, "format": format,
         "rounds_planned": rounds, "k": k, "target_lufs": round(float(target), 2),
         "created": time.strftime("%Y-%m-%d %H:%M:%S"), "candidates": public,
         "consistency_checks": int(consistency_checks), "mock": mock, "sr": sr}
    _write(folder / "tournament.json", t)
    _write(folder / "secret.json", secret)
    _write(folder / "rounds.json", [])
    _write(folder / "state.json", {"revealed": False, "pending": None, "checks_done": 0})
    (folder / "votes.jsonl").touch()
    return view(tid, mock)


def render_candidates(rig, presets: list[dict], di_name: str | None, out_dir: Path,
                      on_progress=None) -> tuple[list[dict], list[dict]]:
    """Render each preset through one DI; split usable candidates from excluded.

    Each recall changes the unit's sound. Excluded renders (the DI coming
    back, or blocks that do not sound from the host) are listed with reasons.
    """
    di_name = di_name or di_mod.SYNTH_NAME
    audio, sr = di_mod.load(di_name, rig.mock)
    di_features = features.analyze_array(audio[:, None], sr)
    candidates, excluded = [], []
    for i, p in enumerate(presets):
        key = guard.folder_key(p["folder"])
        if on_progress:
            on_progress(i, len(presets), f"{guard.folder_name(key)} {p['position']}")
        out_path = paths.ensure(out_dir) / f"{guard.folder_name(key)}_{int(p['position']):03d}.flac"
        info = reamp.render_preset(rig, key, int(p["position"]), audio, sr, di_features,
                                   out_path=out_path)
        entry = {"id": f"{key}#{int(p['position'])}",
                 "label": f"{guard.folder_name(key)} {info['preset']['slot']} {info['preset']['name']}",
                 "file": info["file"],
                 "meta": {"folder": key, "position": int(p["position"]),
                          "name": info["preset"]["name"], "lufs": info["loudness"]["lufs"]}}
        if info["excluded"]["excluded"]:
            excluded.append({**entry, "reasons": info["excluded"]["reasons"]})
        else:
            candidates.append(entry)
    return candidates, excluded


def create_from_presets(rig, presets: list[dict], di: str | None = None, *,
                        title: str = "", **options) -> dict:
    """Render presets through the same DI and start a comparison of the usable ones."""
    out_dir = paths.sub("tournaments", rig.mock) / "_renders" / uuid.uuid4().hex[:8]
    candidates, excluded = render_candidates(rig, presets, di, out_dir)
    if len(candidates) < 2:
        reasons = "; ".join(f"{e['label']}: {' / '.join(e['reasons'])}" for e in excluded)
        raise QcsuiteError(f"比べられる候補が 2 つ未満です(除外: {reasons or 'なし'})。")
    t = create(candidates, mock=rig.mock, title=title or f"プリセットの比較({len(candidates)} 件)",
               source="presets", di=di or di_mod.SYNTH_NAME, **options)
    folder = _dir(t["id"], rig.mock)
    meta = _read(folder / "tournament.json")
    meta["excluded"] = [{"label": e["label"], "reasons": e["reasons"]} for e in excluded]
    _write(folder / "tournament.json", meta)
    return view(t["id"], rig.mock)


# -- ratings and pairing -------------------------------------------------------------

def _votes(folder: Path) -> list[dict]:
    path = folder / "votes.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def elo(votes: list[dict], codes: list[str], k: float = DEFAULT_K) -> dict:
    """Ratings and records from the votes (consistency checks excluded)."""
    table = {c: {"code": c, "rating": INITIAL_RATING, "wins": 0, "losses": 0, "ties": 0,
                 "games": 0} for c in codes}
    for v in votes:
        if v.get("check"):
            continue
        a, b = table[v["a"]], table[v["b"]]
        score = {"a": 1.0, "b": 0.0, "tie": 0.5}[v["choice"]]
        ea = expected(a["rating"], b["rating"])
        a["rating"] += k * (score - ea)
        b["rating"] += k * ((1 - score) - (1 - ea))
        a["games"] += 1
        b["games"] += 1
        if score == 1.0:
            a["wins"] += 1
            b["losses"] += 1
        elif score == 0.0:
            b["wins"] += 1
            a["losses"] += 1
        else:
            a["ties"] += 1
            b["ties"] += 1
    for row in table.values():
        row["rating"] = round(row["rating"], 1)
    return table


def _played(votes: list[dict]) -> set:
    return {frozenset((v["a"], v["b"])) for v in votes if not v.get("check")}


def swiss_round(codes: list[str], ratings: dict, played: set, byes: set,
                rng: random.Random) -> tuple[list[tuple[str, str]], str | None]:
    """Pair close ratings without repeats; returns ``(pairs, bye)``."""
    order = sorted(codes, key=lambda c: (-ratings[c]["rating"], c))
    bye = None
    if len(order) % 2:
        candidates = [c for c in reversed(order) if c not in byes] or list(reversed(order))
        bye = candidates[0]
        order.remove(bye)
    pairs = []
    unpaired = list(order)
    while unpaired:
        a = unpaired.pop(0)
        partner = next((b for b in unpaired if frozenset((a, b)) not in played), None)
        if partner is None:
            continue                    # everyone left has met a; a sits this round out
        unpaired.remove(partner)
        pairs.append((a, partner) if rng.random() < 0.5 else (partner, a))
    return pairs, bye


def _knockout_round(alive: list[str], rng: random.Random) -> tuple[list[tuple], str | None]:
    order = list(alive)
    bye = order.pop() if len(order) % 2 else None
    pairs = [(order[i], order[i + 1]) if rng.random() < 0.5 else (order[i + 1], order[i])
             for i in range(0, len(order), 2)]
    return pairs, bye


def _alive(t: dict, rounds: list[dict], votes: list[dict]) -> list[str]:
    alive = [c["code"] for c in t["candidates"]]
    by_pair = {(v["round"], frozenset((v["a"], v["b"]))): v for v in votes if not v.get("check")}
    for r in rounds:
        winners = [] if r["bye"] is None else [r["bye"]]
        for a, b in r["pairs"]:
            v = by_pair.get((r["round"], frozenset((a, b))))
            if v is None:
                return alive
            winners.append(v["a"] if v["choice"] == "a" else v["b"])
        alive = [c for c in alive if c in winners]
    return alive


def _next_round(t: dict, folder: Path, rounds: list[dict], votes: list[dict]) -> dict | None:
    codes = [c["code"] for c in t["candidates"]]
    number = len(rounds) + 1
    rng = random.Random(f"{t['id']}/{number}")
    if t["format"] == "knockout":
        alive = _alive(t, rounds, votes)
        if len(alive) < 2:
            return None
        pairs, bye = _knockout_round(alive, rng)
    else:
        if number > t["rounds_planned"]:
            return None
        ratings = elo(votes, codes, t["k"])
        byes = {r["bye"] for r in rounds if r["bye"]}
        pairs, bye = swiss_round(codes, ratings, _played(votes), byes, rng)
        if not pairs:
            return None
    r = {"round": number, "pairs": [list(p) for p in pairs], "bye": bye}
    rounds.append(r)
    _write(folder / "rounds.json", rounds)
    return r


def _check_due(t: dict, state: dict, votes: list[dict]) -> bool:
    """Spread the consistency checks over the planned games."""
    wanted = t["consistency_checks"]
    if state.get("checks_done", 0) >= wanted:
        return False
    real = sum(1 for v in votes if not v.get("check"))
    n = len(t["candidates"])
    planned = max(1, (n // 2) * t["rounds_planned"])
    every = max(1, planned // (wanted + 1))
    return real >= every * (state.get("checks_done", 0) + 1)


def next_pair(tid: str, mock: bool) -> dict:
    """The pair to listen to now (creating the next round when one finishes)."""
    folder = _dir(tid, mock)
    t = _read(folder / "tournament.json")
    state = _read(folder / "state.json")
    if state.get("pending"):
        return _pair_view(t, state["pending"], mock)
    votes = _votes(folder)
    rounds = _read(folder / "rounds.json", [])
    if _check_due(t, state, votes):
        pending = _make_check(t, folder, state)
    else:
        done = {(v["round"], frozenset((v["a"], v["b"]))) for v in votes if not v.get("check")}
        pending = None
        current = rounds[-1] if rounds else None
        if current is not None:
            for a, b in current["pairs"]:
                if (current["round"], frozenset((a, b))) not in done:
                    pending = {"round": current["round"], "a": a, "b": b, "check": False}
                    break
        if pending is None:
            r = _next_round(t, folder, rounds, votes)
            if r is None:
                return {"done": True, "progress": progress(tid, mock)}
            a, b = r["pairs"][0]
            pending = {"round": r["round"], "a": a, "b": b, "check": False}
    pending["pair_id"] = len(votes) + 1
    state["pending"] = pending
    _write(folder / "state.json", state)
    return _pair_view(t, pending, mock)


def _make_check(t: dict, folder: Path, state: dict) -> dict:
    """One candidate against itself, through two separately written files."""
    rng = random.Random(f"{t['id']}/check/{state.get('checks_done', 0)}")
    cand = rng.choice(t["candidates"])
    src = paths.home(t["mock"]) / cand["audio"]
    copy = folder / "audio" / f"{uuid.uuid4().hex[:10]}.flac"
    shutil.copyfile(src, copy)
    files = [cand["audio"], _rel(copy, t["mock"])]
    rng.shuffle(files)
    return {"round": None, "a": cand["code"], "b": cand["code"], "check": True,
            "files": files}


def _pair_view(t: dict, pending: dict, mock: bool) -> dict:
    audio = {c["code"]: c["audio"] for c in t["candidates"]}
    files = pending.get("files") or [audio[pending["a"]], audio[pending["b"]]]
    return {"done": False, "pair_id": pending["pair_id"], "round": pending["round"],
            "a": {"audio": files[0], "path": str(paths.home(mock) / files[0])},
            "b": {"audio": files[1], "path": str(paths.home(mock) / files[1])},
            "allow_tie": t["format"] != "knockout" or pending.get("check", False),
            "progress": progress(t["id"], mock)}


def vote(tid: str, mock: bool, choice: str, pair_id: int | None = None) -> dict:
    """Record the player's choice for the pair on screen."""
    folder = _dir(tid, mock)
    t = _read(folder / "tournament.json")
    state = _read(folder / "state.json")
    pending = state.get("pending")
    if not pending:
        raise QcsuiteError("いま投票する組がありません(先に next を)。")
    if pair_id is not None and int(pair_id) != pending["pair_id"]:
        raise QcsuiteError("画面の組が古くなっています。読み込み直してください。")
    if choice not in CHOICES:
        raise QcsuiteError("選択は 1(A)/ 2(B)/ 0(同じくらい)です。")
    choice = CHOICES[choice]
    if choice == "tie" and t["format"] == "knockout" and not pending.get("check"):
        raise QcsuiteError("勝ち抜き戦では、どちらかを選んでください。")
    record = {"pair_id": pending["pair_id"], "round": pending["round"], "a": pending["a"],
              "b": pending["b"], "choice": choice, "check": bool(pending.get("check")),
              "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(folder / "votes.jsonl", "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if pending.get("check"):
        state["checks_done"] = state.get("checks_done", 0) + 1
    state["pending"] = None
    _write(folder / "state.json", state)
    return {"recorded": True, "progress": progress(tid, mock)}


def progress(tid: str, mock: bool) -> dict:
    folder = _dir(tid, mock)
    t = _read(folder / "tournament.json")
    votes = [v for v in _votes(folder) if not v.get("check")]
    n = len(t["candidates"])
    planned = (n - 1) if t["format"] == "knockout" else (n // 2) * t["rounds_planned"]
    rounds = _read(folder / "rounds.json", [])
    return {"votes": len(votes), "planned": planned, "round": len(rounds),
            "rounds_planned": t["rounds_planned"]}


# -- results -------------------------------------------------------------------------

def standings(tid: str, mock: bool) -> list[dict]:
    """The tally of the player's votes, highest rating first (codes only)."""
    folder = _dir(tid, mock)
    t = _read(folder / "tournament.json")
    table = elo(_votes(folder), [c["code"] for c in t["candidates"]], t["k"])
    rows = sorted(table.values(), key=lambda r: (-r["rating"], r["code"]))
    if t["format"] == "knockout":
        alive = set(_alive(t, _read(folder / "rounds.json", []), _votes(folder)))
        for r in rows:
            r["alive"] = r["code"] in alive
    return rows


def consistency(tid: str, mock: bool) -> dict:
    checks = [v for v in _votes(_dir(tid, mock)) if v.get("check")]
    same = sum(1 for v in checks if v["choice"] == "tie")
    return {"checks": len(checks), "answered_same": same,
            "share": round(same / len(checks), 2) if checks else None}


def view(tid: str, mock: bool) -> dict:
    folder = _dir(tid, mock)
    t = _read(folder / "tournament.json")
    state = _read(folder / "state.json")
    out = {k: t[k] for k in ("id", "title", "source", "di", "format", "rounds_planned", "k",
                             "target_lufs", "created", "consistency_checks", "mock")}
    out["candidates"] = [{"code": c["code"], "audio": c["audio"]} for c in t["candidates"]]
    out["excluded"] = t.get("excluded", [])
    out["progress"] = progress(tid, mock)
    out["standings"] = standings(tid, mock)
    out["revealed"] = state.get("revealed", False)
    if out["revealed"]:
        out["reveal"] = _reveal_rows(tid, mock)
        out["consistency"] = consistency(tid, mock)
    return out


def _reveal_rows(tid: str, mock: bool) -> list[dict]:
    secret = _read(_dir(tid, mock) / "secret.json")
    return [{**row, **{k: secret[row["code"]][k] for k in ("id", "label", "meta", "gain_db")}}
            for row in standings(tid, mock)]


def reveal(tid: str, mock: bool) -> dict:
    """Show the real names (from now on, this comparison is no longer blind)."""
    folder = _dir(tid, mock)
    state = _read(folder / "state.json")
    state["revealed"] = True
    state["revealed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(folder / "state.json", state)
    return view(tid, mock)


def export_results(tid: str, mock: bool, fmt: str = "md") -> str:
    v = view(tid, mock)
    if fmt == "json":
        return json.dumps(v, ensure_ascii=False, indent=2)
    lines = [f"# {v['title']}", "",
             f"- 形式: {v['format']} / 投票 {v['progress']['votes']} 回 / "
             f"音量 {v['target_lufs']} LUFS にそろえて比較 / {'モック' if v['mock'] else '実機'}",
             "- 順位はあなたの投票の集計です(機械の評価ではありません)。", ""]
    rows = v["reveal"] if v["revealed"] else v["standings"]
    header = "| 順 | 候補 | レーティング | 勝 | 負 | 分 |" + (" 正体 |" if v["revealed"] else "")
    lines += [header, "|---:|---|---:|---:|---:|---:|" + ("---|" if v["revealed"] else "")]
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | {r['code']} | {r['rating']} | {r['wins']} | {r['losses']} | "
                     f"{r['ties']} |" + (f" {r['label']} |" if v["revealed"] else ""))
    if v.get("consistency") and v["consistency"]["checks"]:
        c = v["consistency"]
        lines += ["", f"同じ音どうしの確認: {c['checks']} 回中 {c['answered_same']} 回「同じくらい」"]
    if v["excluded"]:
        lines += ["", "## 比較から除外したもの"]
        lines += [f"- {e['label']}: {' / '.join(e['reasons'])}" for e in v["excluded"]]
    if not v["revealed"]:
        lines += ["", "(名前はまだ伏せています。reveal で表示します)"]
    return "\n".join(lines) + "\n"


def listing(mock: bool) -> list[dict]:
    out = []
    for folder in sorted(root(mock).iterdir(), reverse=True):
        if (folder / "tournament.json").exists():
            t = _read(folder / "tournament.json")
            state = _read(folder / "state.json", {})
            out.append({"id": t["id"], "title": t["title"], "format": t["format"],
                        "candidates": len(t["candidates"]), "created": t["created"],
                        "revealed": state.get("revealed", False),
                        "progress": progress(t["id"], mock)})
    return out


# -- background rendering (web) --------------------------------------------------------

def start_render_job(presets: list[dict], *, di: str | None, mock: bool, title: str,
                     options: dict) -> jobs.Job:
    items = [{"key": f"{guard.folder_key(p['folder'])}#{int(p['position'])}",
              "folder": guard.folder_key(p["folder"]), "position": int(p["position"])}
             for p in presets]
    if len(items) < 2:
        raise QcsuiteError("比べるプリセットを 2 つ以上選んでください。")
    return jobs.create(RENDER_KIND, {"di": di or di_mod.SYNTH_NAME, "comparison_title": title,
                                     "options": options}, items, mock,
                       title=f"比較用のレンダリング({len(items)} 件)")


def render_job_work(rig, job: jobs.Job):
    spec = job.spec
    audio, sr = di_mod.load(spec["di"], rig.mock)
    di_features = features.analyze_array(audio[:, None], sr)
    out_dir = paths.ensure(job.dir / "renders")

    def work(item):
        info = reamp.render_preset(rig, item["folder"], item["position"], audio, sr,
                                   di_features, out_path=out_dir / f"{item['position']:03d}_"
                                   f"{guard.folder_name(item['folder'])}.flac")
        return {"id": item["key"],
                "label": f"{guard.folder_name(item['folder'])} {info['preset']['slot']} "
                         f"{info['preset']['name']}",
                "file": info["file"], "excluded": info["excluded"],
                "meta": {"folder": item["folder"], "position": item["position"],
                         "name": info["preset"]["name"], "lufs": info["loudness"]["lufs"]}}
    return work


def finish_render_job(job: jobs.Job, mock: bool) -> dict:
    """Make the comparison from a finished render job's usable renders."""
    spec = job.spec
    rendered = [r["result"] for r in job.results() if r.get("ok")]
    usable = [r for r in rendered if not r["excluded"]["excluded"]]
    excluded = [r for r in rendered if r["excluded"]["excluded"]]
    if len(usable) < 2:
        raise QcsuiteError("比べられる候補が 2 つ未満です(除外: " + "; ".join(
            f"{e['label']}: {' / '.join(e['excluded']['reasons'])}" for e in excluded) + ")。")
    candidates = [{k: r[k] for k in ("id", "label", "file", "meta")} for r in usable]
    t = create(candidates, mock=mock,
               title=spec.get("comparison_title") or f"プリセットの比較({len(usable)} 件)",
               source="presets", di=spec["di"], **spec.get("options", {}))
    folder = _dir(t["id"], mock)
    meta = _read(folder / "tournament.json")
    meta["excluded"] = [{"label": e["label"], "reasons": e["excluded"]["reasons"]} for e in excluded]
    _write(folder / "tournament.json", meta)
    return view(t["id"], mock)


# -- CLI ---------------------------------------------------------------------------------

def _options(a) -> dict:
    return {"format": a.format, "rounds": a.rounds, "consistency_checks": a.checks}


def _standings_text(v: dict) -> str:
    rows = v["reveal"] if v["revealed"] else v["standings"]
    lines = [f"{v['title']}({v['format']}、投票 {v['progress']['votes']}/{v['progress']['planned']})",
             "あなたの投票の集計:"]
    for i, r in enumerate(rows, 1):
        name = f"  {r['label']}" if v["revealed"] else ""
        lines.append(f"  {i:>2}. {r['code']:<8} {r['rating']:>7.1f}  "
                     f"{r['wins']}勝 {r['losses']}敗 {r['ties']}分{name}")
    for e in v["excluded"]:
        lines.append(f"  除外: {e['label']}({' / '.join(e['reasons'])})")
    return "\n".join(lines)


def cmd_new(ctx, a):
    labels = a.label or []
    candidates = []
    for i, f in enumerate(a.files):
        candidates.append({"id": f"file{i + 1}", "label": labels[i] if i < len(labels) else Path(f).stem,
                           "file": str(Path(f).resolve()), "meta": {"file": f}})
    v = create(candidates, mock=ctx.mock, title=a.title or "ファイルの比較", source="files",
               target_lufs=a.target, **_options(a))
    ctx.out(v, f"比較 {v['id']} を作りました({len(candidates)} 件、名前は伏せています)。"
               f"\n次: qcsuite tournament next {v['id']}")


def cmd_from_presets(ctx, a):
    from qcsuite.features.leveler import parse_preset
    presets = [parse_preset(p) for p in a.preset]
    with ctx.rig("tournament from-presets") as rig:
        for folder in a.folder:
            key = guard.folder_key(folder)
            presets += [{"folder": key, "position": p["index"]} for p in rig.device.presets(key)]
        print(f"{len(presets)} 件のプリセットを順に読み込みます(本体の音が切り替わります)。",
              file=sys.stderr)
        ctx.warn_monitor(rig.mock)
        v = create_from_presets(rig, presets, a.di, title=a.title or "", **_options(a))
    ctx.out(v, f"比較 {v['id']} を作りました。\n" + _standings_text(v)
            + f"\n次: qcsuite tournament next {v['id']}")


def cmd_next(ctx, a):
    p = next_pair(a.id, ctx.mock)
    if p["done"]:
        ctx.out(p, "予定の組はすべて終わりました。`qcsuite tournament standings` と `reveal` へ。")
        return
    ctx.out(p, f"組 {p['pair_id']}(第 {p['round'] or '確認'} 回戦)\n"
               f"  A: {p['a']['path']}\n  B: {p['b']['path']}\n"
               f"聴いたら: qcsuite tournament vote {a.id} 1|2|0  (1=A、2=B、0=同じくらい"
               f"{'' if p['allow_tie'] else '、この形式では不可'})")


def cmd_vote(ctx, a):
    r = vote(a.id, ctx.mock, a.choice, a.pair)
    ctx.out(r, f"記録しました({r['progress']['votes']}/{r['progress']['planned']})。")


def cmd_standings(ctx, a):
    v = view(a.id, ctx.mock)
    ctx.out(v, _standings_text(v))


def cmd_reveal(ctx, a):
    v = reveal(a.id, ctx.mock)
    text = export_results(a.id, ctx.mock, a.export or "md")
    if a.out:
        Path(a.out).write_text(text)
    ctx.out(v, text)


def cmd_list(ctx, a):
    rows = listing(ctx.mock)
    ctx.out(rows, "\n".join(f"{r['id']}  {r['title']}  {r['progress']['votes']}/"
                            f"{r['progress']['planned']}  {'公開済み' if r['revealed'] else '伏せ中'}"
                            for r in rows) or "(比較はまだありません)")


def add_cli(sub):
    p = sub.add_parser("tournament", help="ブラインド比較(音量をそろえ、名前を伏せて 2 つずつ聴き比べる)")
    s = p.add_subparsers(dest="tournament_cmd", required=True)

    def options(c):
        c.add_argument("--title")
        c.add_argument("--format", choices=FORMATS, default="swiss",
                       help="swiss(総当たりに近い対戦、既定)/ knockout(勝ち抜き)")
        c.add_argument("--rounds", type=int, help="スイス式の回戦数(既定: log2(候補数)+1)")
        c.add_argument("--checks", type=int, default=0,
                       help="同じ音どうしを比べる確認の回数(既定 0)")

    c = s.add_parser("new", help="音声ファイルから比較を作る")
    c.add_argument("files", nargs="+")
    c.add_argument("--label", action="append", help="ファイルの本当の名前(順番に。伏せて保存)")
    c.add_argument("--target", type=float, help="そろえる音量 LUFS(既定: 一番小さい候補)")
    options(c)
    c.set_defaults(fn=cmd_new)
    c = s.add_parser("from-presets", help="プリセットを同じ DI で鳴らして比較を作る")
    c.add_argument("--preset", action="append", default=[], metavar="FOLDER:SLOT")
    c.add_argument("--folder", action="append", default=[])
    c.add_argument("--di")
    options(c)
    c.set_defaults(fn=cmd_from_presets)
    c = s.add_parser("next", help="次に聴く組(A と B のファイル)")
    c.add_argument("id")
    c.set_defaults(fn=cmd_next)
    c = s.add_parser("vote", help="投票する(1=A、2=B、0=同じくらい)")
    c.add_argument("id")
    c.add_argument("choice", choices=["1", "2", "0"])
    c.add_argument("--pair", type=int, help="組の番号(確認用)")
    c.set_defaults(fn=cmd_vote)
    c = s.add_parser("standings", help="投票の集計(名前は伏せたまま)")
    c.add_argument("id")
    c.set_defaults(fn=cmd_standings)
    c = s.add_parser("reveal", help="名前を表示し、結果を書き出す")
    c.add_argument("id")
    c.add_argument("--export", choices=["md", "json"])
    c.add_argument("--out", help="書き出し先ファイル")
    c.set_defaults(fn=cmd_reveal)
    c = s.add_parser("list", help="比較の一覧")
    c.set_defaults(fn=cmd_list)
