"""Edit watcher (idea D) and linked takes (idea H): the player turns knobs on the
unit by ear; the machine writes down what moved, and which rig a take was
played on.

**Watching** reads the live grid with ``read_current_preset``, which has no side
effects (pyquadcortex docstring: unsaved edits survive it, the scene is not
touched), every few seconds, and logs what changed since the previous read:
knobs in real units, bypass, blocks added, removed or replaced, routing, row
levels, scene switches and scene names. It never recalls, never writes. A knob
that keeps moving across consecutive reads becomes ONE entry with its first and
last value. When the grid jumps as a whole - a different name, or many blocks
changing at once (a heuristic) - the unit has probably loaded another preset,
so a new segment starts with its own baseline.

Changes the computer cannot reproduce are marked: string parameters (mic, IR,
capture file) and capture-block knobs read back when written from the
computer but do not change the sound (measured on the player's unit).

A session lives in ``~/.qctone/watch/<id>/``::

    session.json            id, start/stop, interval, segments
    log.jsonl               start / change / scene / segment / note / take / pause / stop
    snapshots/seg_NNN_baseline.json, seg_NNN_final.json   normalized presets

**Exports**: a Markdown summary, and a replay - the numeric part of
baseline -> final as a qctone patch (plus row levels, gate and scene names)
that can be applied to the live grid (unsaved) or saved into the sandbox
through a plan. Two ways to save, both dry runs first:

* ``rebuild`` (default): the final snapshot is rebuilt from scratch on the grid
  and saved. Works anywhere, but captures, mics and IRs written from the
  computer do not sound - the plan says so.
* ``--source FOLDER:SLOT``: the stored preset the session started from is
  recalled (sound changes), the replay is computed against it and applied,
  and the result saved. Captures and mics built on the unit keep sounding;
  only numeric changes are carried over. This is the safer choice when the
  source is known.

**Takes**: one input stream records USB 1 (Input 1, dry) and USB 3/4 (Out 1/2,
wet) - confirmed on the player's unit with ``mapping=[1, 3, 4]``; dry and wet
are apart only by the unit's internal processing (a few ms). The dry track goes
into the DI library, the wet next to it under ``~/.qctone/takes/``, and the
take is linked to the watch log with the preset name, its fingerprint and the
knob values at that moment. The row must take its input from INPUT_1 (after a
reamp it may still be USB 5); ``record_take`` refuses otherwise.
"""

from __future__ import annotations

import json
import select
import sys
import threading
import time
import uuid
from pathlib import Path

import pyquadcortex as pq

from qcsuite import paths, presets
from qcsuite import plan as plan_mod
from qcsuite.audio import di as di_mod
from qcsuite.audio import export
from qcsuite.audio.render import record_take
from qcsuite.device import guard
from qcsuite.errors import QcsuiteError
from qcsuite.features import watch_diff
from qcsuite.rebuild import rebuild_grid

DEFAULT_INTERVAL = 2.0


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def watch_dir(mock: bool) -> Path:
    return paths.sub("watch", mock)


def takes_dir(mock: bool) -> Path:
    return paths.sub("takes", mock)


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def knob_values(n: dict) -> dict:
    """``{"行1 列2 Brit 2203": {"GAIN": "6.5", ...}}`` - the knobs a take was played on."""
    out = {}
    for b in n["blocks"]:
        knobs = dict(presets.main_knobs(b, limit=99))
        bp = b.get("bypass") or {}
        if bp.get("scenes") and all(bp["scenes"]):
            knobs["(バイパス)"] = "on"
        out[presets.block_label(b)] = knobs
    for r in n["rows"]:
        vol = r.get("lane", {}).get("VOLUME", {})
        if vol.get("db") is not None and (r["in_port"] != "EMPTY" or r["out_port"] != "EMPTY"):
            out[f"行{r['row'] + 1} 出力"] = {"VOLUME": f"{vol['db']} dB",
                                             "入力": r["in_port"], "出力": r["out_port"]}
    return out


# -- sessions ---------------------------------------------------------------------

class Session:
    """One watch session's files. Safe to read while another process writes."""

    def __init__(self, folder: Path):
        self.dir = folder
        self.id = folder.name

    @property
    def meta(self) -> dict:
        return json.loads((self.dir / "session.json").read_text())

    def save_meta(self, meta: dict):
        _write_json(self.dir / "session.json", meta)

    def log(self, entry: dict) -> dict:
        entry = {"t": _now(), **entry}
        with open(self.dir / "log.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def entries(self) -> list[dict]:
        path = self.dir / "log.jsonl"
        if not path.exists():
            return []
        out = []
        lines = path.read_text().splitlines()
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                if n != len(lines):          # only a torn LAST line is expected
                    raise
        return out

    def snapshot(self, segment: int, which: str) -> dict | None:
        path = self.dir / "snapshots" / f"seg_{segment:03d}_{which}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def save_snapshot(self, segment: int, which: str, n: dict):
        _write_json(self.dir / "snapshots" / f"seg_{segment:03d}_{which}.json", n)

    def pending(self) -> list[dict]:
        path = self.dir / "pending.json"
        return json.loads(path.read_text()) if path.exists() else []

    def summary(self) -> dict:
        meta = self.meta
        entries = self.entries()
        count = {k: sum(1 for e in entries if e["type"] == k)
                 for k in ("change", "note", "take", "segment", "scene")}
        return {"id": self.id, "started": meta["started"], "stopped": meta.get("stopped"),
                "interval": meta["interval"], "mock": meta["mock"],
                "segments": meta["segments"], "counts": count,
                "running": meta.get("stopped") is None}


def sessions(mock: bool) -> list[Session]:
    return [Session(d) for d in sorted(watch_dir(mock).iterdir(), reverse=True)
            if (d / "session.json").exists()]


def open_session(session_id: str, mock: bool) -> Session:
    if session_id in ("latest", "last"):
        found = sessions(mock)
        if not found:
            raise QcsuiteError("記録(ウォッチのセッション)がまだありません。")
        return found[0]
    folder = watch_dir(mock) / session_id
    if not (folder / "session.json").exists():
        raise QcsuiteError(f"セッション {session_id} が見つかりません(qcsuite watch list で確認)。")
    return Session(folder)


# -- the watcher ---------------------------------------------------------------------

class Watcher:
    """Reads the live grid on each :meth:`poll` and logs what changed."""

    def __init__(self, device, mock: bool, interval: float = DEFAULT_INTERVAL):
        self.device = device
        self.mock = mock
        self.interval = interval
        self.session: Session | None = None
        self.segment = 0
        self.prev: dict | None = None
        self.scene: int | None = None
        self.pending: dict[tuple, dict] = {}

    def _read(self) -> tuple[dict, int]:
        p = self.device.current_preset()
        n = presets.normalize(p, self.device.catalog)
        return n, self.device.active_scene()

    def start(self) -> dict:
        session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.session = Session(paths.ensure(watch_dir(self.mock) / session_id))
        n, scene = self._read()
        self.session.save_meta({"id": session_id, "started": _now(), "stopped": None,
                                "interval": self.interval, "mock": self.mock, "segments": []})
        self._new_segment(n, scene, "記録を開始しました")
        self.session.log({"type": "start", "preset": n["name"], "scene": presets.SCENES[scene],
                          "fingerprint": n["fingerprint"]})
        return self.session.summary()

    def _new_segment(self, n: dict, scene: int, note: str):
        self.segment += 1
        self.session.save_snapshot(self.segment, "baseline", n)
        self.session.save_snapshot(self.segment, "final", n)
        meta = self.session.meta
        meta["segments"].append({"n": self.segment, "preset": n["name"], "started": _now(),
                                 "fingerprint": n["fingerprint"], "note": note})
        self.session.save_meta(meta)
        if self.segment > 1:
            self.session.log({"type": "segment", "segment": self.segment, "preset": n["name"],
                              "note": note})
        self.prev, self.scene = n, scene

    def _flush(self, keys=None):
        for key in list(self.pending if keys is None else keys):
            entry = self.pending.pop(key)
            self.session.log(entry)
        self._save_pending()

    def _save_pending(self):
        _write_json(self.session.dir / "pending.json", list(self.pending.values()))

    def poll(self) -> list[dict]:
        """One read. Returns the entries written (or merged) by this poll."""
        if self.session is None:
            raise QcsuiteError("記録が始まっていません。")
        n, scene = self._read()
        written = []
        if scene != self.scene:
            self._flush()
            written.append(self.session.log({
                "type": "scene", "segment": self.segment,
                "before": presets.SCENES[self.scene], "after": presets.SCENES[scene]}))
            self.scene = scene
        found = watch_diff.changes(self.prev, n)
        if found and watch_diff.looks_like_another_preset(self.prev, n, found):
            self._flush()
            self._new_segment(n, scene, f"本体で別のプリセット「{n['name']}」が読み込まれたようです"
                                        "(名前が変わった、または一度に多くの変更)")
            written.append({"type": "segment", "segment": self.segment, "preset": n["name"]})
            return written
        moved = set()
        for c in found:
            key = tuple(c.pop("key"))
            entry = {"type": "change", "segment": self.segment, **c,
                     "first_t": _now(), "last_t": _now()}
            if c["manual"]:
                entry["manual_note"] = watch_diff.MANUAL_NOTE
            if c["kind"] in ("param", "lane", "gate"):
                if key in self.pending:
                    merged = self.pending[key]
                    merged.update(after=c["after"], last_t=_now(), polls=merged.get("polls", 1) + 1)
                    written.append(merged)
                else:
                    self.pending[key] = {**entry, "polls": 1}
                    written.append(self.pending[key])
                moved.add(key)
            else:
                written.append(self.session.log(entry))
        # A knob that did not move in this read has settled: write it down.
        self._flush([k for k in self.pending if k not in moved])
        if found:
            self.session.save_snapshot(self.segment, "final", n)
        self.prev = n
        return written

    def note(self, text: str) -> dict:
        self._flush()
        return self.session.log({"type": "note", "segment": self.segment, "text": text})

    def rebaseline(self, note: str):
        """Start a fresh segment from whatever is on the grid now."""
        self._flush()
        n, scene = self._read()
        self._new_segment(n, scene, note)

    def stop(self) -> dict:
        if self.session is None:
            raise QcsuiteError("記録が始まっていません。")
        self._flush()
        meta = self.session.meta
        meta["stopped"] = _now()
        self.session.save_meta(meta)
        self.session.log({"type": "stop"})
        return self.session.summary()


def add_note(session_id: str, text: str, mock: bool) -> dict:
    """A note from outside the watcher (another terminal, or after stopping)."""
    s = open_session(session_id, mock)
    segment = s.meta["segments"][-1]["n"] if s.meta["segments"] else 1
    return s.log({"type": "note", "segment": segment, "text": text})


# -- exports -------------------------------------------------------------------------

def _segment(s: Session, segment: int | None) -> int:
    segs = [x["n"] for x in s.meta["segments"]]
    if not segs:
        raise QcsuiteError("このセッションには記録がありません。")
    if segment is None:
        return segs[-1]
    if segment not in segs:
        raise QcsuiteError(f"区間 {segment} はありません({segs})。")
    return segment


def replay_for(s: Session, segment: int | None = None) -> dict:
    seg = _segment(s, segment)
    base, final = s.snapshot(seg, "baseline"), s.snapshot(seg, "final")
    r = watch_diff.replay(base, final)
    return {"segment": seg, "preset": final["name"], **r}


def show(s: Session) -> dict:
    return {"summary": s.summary(), "entries": s.entries(), "pending": s.pending()}


def markdown(s: Session) -> str:
    meta = s.meta
    entries = s.entries()
    lines = [f"# 編集の記録 {s.id}", "",
             f"- 開始: {meta['started']} / 終了: {meta.get('stopped') or '(記録中)'}",
             f"- 読み取り間隔: {meta['interval']} 秒(読み取りだけで、本体には何も書き込んでいません)",
             f"- {'モック' if meta['mock'] else '実機'}", ""]
    for seg in meta["segments"]:
        n = seg["n"]
        base, final = s.snapshot(n, "baseline"), s.snapshot(n, "final")
        lines += [f"## 区間 {n}: 「{seg['preset']}」({seg['started']} から)", ""]
        if seg.get("note"):
            lines += [f"> {seg['note']}", ""]
        rows = [e for e in entries if e.get("segment") == n and e["type"] != "segment"]
        rows.sort(key=lambda e: e.get("first_t", e["t"]))
        if rows:
            lines += ["| 時刻 | 内容 |", "|---|---|"]
        for e in rows:
            when = e.get("first_t", e["t"])[11:]
            if e["type"] == "change":
                text = f"{e['where']}: {e['before']} → {e['after']}"
                if e.get("polls", 1) > 1:
                    text += f"(連続 {e['polls']} 回の操作をまとめた、{e['last_t'][11:]} まで)"
                if e.get("manual"):
                    text += f" ※{watch_diff.MANUAL_NOTE}"
            elif e["type"] == "scene":
                text = f"シーン {e['before']} → {e['after']}"
            elif e["type"] == "note":
                text = f"**メモ**: {e['text']}"
            elif e["type"] == "take":
                text = f"**録音** 「{e['di']}」({e['seconds']} 秒、ウェット: {e['wet_file']})"
            elif e["type"] == "pause":
                text = f"(記録を一時停止: {e.get('reason', '')})"
            else:
                continue
            lines.append(f"| {when} | {text.replace('|', '/')} |")
        lines.append("")
        changes = watch_diff.changes(base, final)
        lines += ["### 開始時と最後の違い", ""]
        lines += [f"- {c['where']}: {c['before']} → {c['after']}"
                  + (f" ※{watch_diff.MANUAL_NOTE}" if c["manual"] else "") for c in changes] \
            or ["- (違いはありません)"]
        lines.append("")
    lines += ["---", "モックで確認済み・実機は未確認の機能で作成しました。音の判断は記録した人の耳です。", ""]
    return "\n".join(lines)


def apply_replay(device, s: Session, segment: int | None = None) -> dict:
    """Apply a segment's numeric changes to the live grid (unsaved)."""
    r = replay_for(s, segment)
    result = watch_diff.apply(device, r)
    return {"segment": r["segment"], **result}


# -- saving into the sandbox ------------------------------------------------------------

def parse_source(text: str) -> dict:
    """``"kami:1E"`` or ``"My Presets:30A"`` -> ``{"folder": key, "position": int}``."""
    if ":" not in text:
        raise QcsuiteError(f"元のプリセットは「フォルダ:スロット」で指定してください(例: kami:1E): {text}")
    folder, slot = text.rsplit(":", 1)
    return {"folder": guard.folder_key(folder.strip()),
            "position": pq.slot_to_position(slot.strip())}


def to_sandbox_plan(device, s: Session, *, segment: int | None = None, source: dict | None = None,
                    folder: str | None = None, slot: str | None = None,
                    name: str | None = None) -> plan_mod.Plan:
    """A one-step plan saving the segment's final state into the sandbox."""
    seg = _segment(s, segment)
    final = s.snapshot(seg, "final")
    key = device.ensure_sandbox() if folder is None else guard.folder_key(folder)
    position = pq.slot_to_position(slot) if slot else device.free_position(key)
    occupied = {p["index"]: p["name"] for p in device.presets(key)} \
        if key in {f["key"] for f in device.setlists()} else {}
    payload = dict(final)
    warnings = []
    if source is not None:
        payload["_watch"] = {"source": source}
        warnings.append(f"保存の前に、元のプリセット(「{guard.folder_name(source['folder'])}」"
                        f"{pq.position_to_slot(source['position'])})を本体に読み込みます。音が切り替わります。")
        warnings.append("元のプリセットから最後の状態までの数値の変更だけを再現します。"
                        "マイク・IR・キャプチャーの変更は本体で行ってください。")
    else:
        warnings.append("グリッドを JSON から組み立て直して保存します。キャプチャー・マイク・IR・未所有の"
                        "プラグインは音に反映されません(元のプリセットが分かるなら --source を使う方が安全です)。")
    manual = replay_for(s, seg)["manual"]
    warnings += [f"手作業: {m}" for m in manual]
    step = plan_mod.Step("rebuild", key, position=position,
                         name=name or f"{final['name']} watch",
                         overwrites=occupied.get(position), payload=payload,
                         note=f"編集の記録 {s.id} 区間 {seg}"
                              + ("(元のプリセット + 変更の再現)" if source else "(JSON から組み立て)"))
    return plan_mod.Plan(f"編集の記録を sandbox に保存({s.id})", steps=[step], warnings=warnings)


def rebuild_for_watch(device, payload: dict):
    """Plan rebuild hook: recall the source and replay onto it, else rebuild."""
    source = (payload.get("_watch") or {}).get("source")
    if not source:
        for w in rebuild_grid(device, payload):
            device.notice("inert_write", w)
        return
    stored = device.read_stored(source["folder"], source["position"])
    current = presets.normalize(stored, device.catalog)
    final = {k: v for k, v in payload.items() if k != "_watch"}
    r = watch_diff.replay(current, final)
    watch_diff.apply(device, r)
    for m in r["manual"]:
        device.notice("inert_write", f"手作業が必要: {m}")


def execute_plan(plan: plan_mod.Plan, device, confirm: bool, on_progress=None) -> dict:
    return plan_mod.execute(plan, device, confirm=confirm, rebuild=rebuild_for_watch,
                            on_progress=on_progress)


# -- takes ------------------------------------------------------------------------------

def _unique_name(name: str, mock: bool) -> str:
    taken = {m["name"] for m in di_mod.listing(mock)} | \
        {p.stem.removesuffix("_wet") for p in takes_dir(mock).glob("*_wet.wav")}
    if name not in taken:
        return name
    n = 2
    while f"{name}_{n}" in taken:
        n += 1
    return f"{name}_{n}"


def take(rig, name: str, seconds: float, *, session: Session | None = None,
         watcher: Watcher | None = None, note: str = "", performance=None) -> dict:
    """Record the player (dry + wet), file it, and link it to the watch log."""
    device = rig.device
    n = presets.normalize(device.current_preset(), device.catalog)
    scene = device.active_scene()
    recorded = record_take(rig, seconds, performance=performance)
    name = _unique_name(name, rig.mock)
    meta = di_mod.save(name, recorded.dry, recorded.sr, rig.mock, source="take",
                       note=note or f"「{n['name']}」で弾いた録音")
    folder = takes_dir(rig.mock)
    wet = export.write(folder / f"{export.safe_name(name)}_wet.wav", recorded.wet, recorded.sr, "wav")
    snapshot_file = folder / f"{export.safe_name(name)}_snapshot.json"
    _write_json(snapshot_file, n)
    home = paths.home(rig.mock)
    link = {"type": "take", "di": meta["name"], "di_file": f"di/{meta['file']}",
            "wet_file": str(wet.relative_to(home)), "snapshot_file": str(snapshot_file.relative_to(home)),
            "seconds": round(len(recorded.dry) / recorded.sr, 2),
            "offset_ms": None if recorded.offset_ms is None else round(float(recorded.offset_ms), 1),
            "preset": n["name"], "fingerprint": n["fingerprint"], "scene": presets.SCENES[scene],
            "knobs": knob_values(n), "note": note, "mock": rig.mock,
            "levels": recorded.summary()}
    if watcher is not None and watcher.session is not None:
        # Appended, not flushed through the watcher: a background poll may be
        # merging knob turns at the same moment.
        link = watcher.session.log({**link, "segment": watcher.segment, "session": watcher.session.id})
    elif session is not None:
        segs = session.meta["segments"]
        link = session.log({**link, "segment": segs[-1]["n"] if segs else 1, "session": session.id})
    else:
        link = {"t": _now(), **link, "session": None}
    _write_json(folder / f"{export.safe_name(name)}.json", link)
    return link


def takes(mock: bool) -> list[dict]:
    out = []
    for path in sorted(takes_dir(mock).glob("*.json")):
        if path.name.endswith("_snapshot.json"):
            continue
        out.append(json.loads(path.read_text()))
    return out


# -- CLI ---------------------------------------------------------------------------------

def _print_entry(e: dict):
    if e["type"] == "change":
        mark = f"  ※{watch_diff.MANUAL_NOTE}" if e.get("manual") else ""
        print(f"  {e.get('last_t', e['t'])[11:]} {e['where']}: {e['before']} → {e['after']}{mark}",
              file=sys.stderr)
    elif e["type"] == "scene":
        print(f"  シーン {e['before']} → {e['after']}", file=sys.stderr)
    elif e["type"] == "segment":
        print(f"-- 区間 {e['segment']}: 「{e['preset']}」", file=sys.stderr)
    elif e["type"] == "note":
        print(f"  メモ: {e['text']}", file=sys.stderr)
    elif e["type"] == "take":
        print(f"  録音: {e['di']}({e['seconds']} 秒、ずれ {e['offset_ms']} ms)", file=sys.stderr)


def cmd_start(ctx, a):
    with ctx.rig("watch") as rig:
        w = Watcher(rig.device, rig.mock, a.interval)
        summary = w.start()
        print(f"記録を始めました: {summary['id']}(本体を操作してください。読み取るだけで書き込みはしません)",
              file=sys.stderr)
        interactive = sys.stdin.isatty()
        if interactive:
            print("  n テキスト = メモ / t 名前 [秒] = 録音 / q = 終了(Ctrl-C でも終了)", file=sys.stderr)
        deadline = time.monotonic() + a.duration if a.duration else None
        try:
            while deadline is None or time.monotonic() < deadline:
                line = None
                if interactive:
                    ready, _, _ = select.select([sys.stdin], [], [], a.interval)
                    if ready:
                        line = sys.stdin.readline().strip()
                else:
                    time.sleep(a.interval)
                if line:
                    if line in ("q", "quit"):
                        break
                    if line.startswith("n "):
                        _print_entry(w.note(line[2:].strip()))
                    elif line.startswith("t "):
                        parts = line[2:].split()
                        seconds = float(parts[1]) if len(parts) > 1 else 20.0
                        print("録音します。弾いてください(行の入力が INPUT_1 である必要があります)。",
                              file=sys.stderr)
                        try:
                            _print_entry(take(rig, parts[0], seconds, watcher=w))
                        except RuntimeError as exc:
                            print(f"録音できませんでした: {exc}", file=sys.stderr)
                    else:
                        print("  n / t / q のどれかで始めてください。", file=sys.stderr)
                for e in w.poll():
                    _print_entry(e)
                ctx.notices(rig.device)
        except KeyboardInterrupt:
            pass
        summary = w.stop()
    ctx.out(summary, f"記録を終えました: {summary['id']}(変更 {summary['counts']['change']} 件、"
                     f"メモ {summary['counts']['note']} 件、録音 {summary['counts']['take']} 件)")


def cmd_note(ctx, a):
    entry = add_note(a.session, a.text, ctx.mock)
    ctx.out(entry, "メモを残しました。")


def cmd_list(ctx, a):
    rows = [s.summary() for s in sessions(ctx.mock)]
    ctx.out(rows, "\n".join(
        f"{r['id']}  {r['started']}  {'記録中' if r['running'] else '終了'}  "
        f"区間 {len(r['segments'])}  変更 {r['counts']['change']}  メモ {r['counts']['note']}  "
        f"録音 {r['counts']['take']}  {r['segments'][0]['preset'] if r['segments'] else ''}"
        for r in rows) or "(記録はまだありません)")


def cmd_show(ctx, a):
    s = open_session(a.session, ctx.mock)
    ctx.out(show(s), markdown(s))


def cmd_export(ctx, a):
    s = open_session(a.session, ctx.mock)
    if a.patch:
        data = replay_for(s, a.segment)
        text = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        data, text = {"markdown": markdown(s)}, markdown(s)
    if a.out:
        Path(a.out).write_text(text + "\n")
        print(f"書き出しました: {a.out}", file=sys.stderr)
    else:
        ctx.out(data, text)


def cmd_apply(ctx, a):
    s = open_session(a.session, ctx.mock)
    with ctx.rig("watch apply") as rig:
        result = apply_replay(rig.device, s, a.segment)
    text = f"{result['applied']} 件の操作をグリッドに適用しました(保存はしていません)。"
    if result["manual"]:
        text += "\n本体で手作業が必要:\n" + "\n".join(f"  - {m}" for m in result["manual"])
    ctx.out(result, text)


def cmd_to_sandbox(ctx, a):
    s = open_session(a.session, ctx.mock)
    source = parse_source(a.source) if a.source else None
    with ctx.rig("watch to-sandbox") as rig:
        plan = to_sandbox_plan(rig.device, s, segment=a.segment, source=source,
                               folder=a.folder, slot=a.slot, name=a.name)
        result = ctx.run_plan(plan, rig.device, apply=a.apply, rebuild=rebuild_for_watch)
    ctx.out(result, json.dumps(result.get("results", []), ensure_ascii=False, indent=1))


def cmd_take(ctx, a):
    session = open_session(a.session, ctx.mock) if a.session else None
    with ctx.rig("watch take") as rig:
        if not rig.mock:
            print(f"{a.seconds} 秒録音します。本体の音はいつもどおり出ます。行の入力が INPUT_1 で"
                  "ある必要があります。弾き始めてください。", file=sys.stderr)
        link = take(rig, a.name, a.seconds, session=session, note=a.note or "")
    ctx.out(link, f"録音しました: DI「{link['di']}」/ ウェット {link['wet_file']} "
                  f"(ずれ {link['offset_ms']} ms、プリセット「{link['preset']}」)")


def add_cli(sub):
    p = sub.add_parser("watch", help="本体での編集を読み取りだけで記録する(編集ウォッチャー)と、紐づけた録音")
    s = p.add_subparsers(dest="watch_cmd", required=True)
    c = s.add_parser("start", help="記録を始める(Ctrl-C で終了)")
    c.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="読み取り間隔(秒)")
    c.add_argument("--duration", type=float, help="この秒数で自動的に終える")
    c.set_defaults(fn=cmd_start)
    c = s.add_parser("note", help="セッションにメモを残す(latest で最新)")
    c.add_argument("session")
    c.add_argument("text")
    c.set_defaults(fn=cmd_note)
    c = s.add_parser("list", help="セッションの一覧")
    c.set_defaults(fn=cmd_list)
    c = s.add_parser("show", help="セッションの内容(Markdown)")
    c.add_argument("session")
    c.set_defaults(fn=cmd_show)
    c = s.add_parser("export", help="Markdown(既定)か、再現用のパッチを書き出す")
    c.add_argument("session")
    c.add_argument("--md", action="store_true", help="Markdown(既定)")
    c.add_argument("--patch", action="store_true", help="開始時から最後までの数値の変更をパッチに")
    c.add_argument("--segment", type=int)
    c.add_argument("--out")
    c.set_defaults(fn=cmd_export)
    c = s.add_parser("apply", help="区間の変更を現在のグリッドに適用する(保存しない)")
    c.add_argument("session")
    c.add_argument("--segment", type=int)
    c.set_defaults(fn=cmd_apply)
    c = s.add_parser("to-sandbox", help="区間の最後の状態を sandbox に保存する計画(--apply で実行)")
    c.add_argument("session")
    c.add_argument("--segment", type=int)
    c.add_argument("--source", help="元のプリセット「フォルダ:スロット」(例 kami:1E)。分かるならこちらが安全")
    c.add_argument("--folder", help="保存先(既定 sandbox。他は --allow-folder が必要)")
    c.add_argument("--slot", help="保存先のスロット(既定は空きの先頭)")
    c.add_argument("--name")
    c.add_argument("--apply", action="store_true")
    c.set_defaults(fn=cmd_to_sandbox)
    c = s.add_parser("take", help="弾いた音を DI とウェットで同時に録り、記録に紐づける")
    c.add_argument("name")
    c.add_argument("--seconds", type=float, default=20.0)
    c.add_argument("--session", help="紐づけるセッション(latest で最新)")
    c.add_argument("--note")
    c.set_defaults(fn=cmd_take)


# -- the web watcher (one per server) -------------------------------------------------------

class BackgroundWatch:
    """Polls in a thread for the web UI; steps aside while a job runs.

    It shares the device with every other request, so a poll is skipped when
    the device is busy, and after a job (which recalls presets) it starts a
    fresh segment instead of logging the job's edits as the player's.
    """

    def __init__(self, state, interval: float = DEFAULT_INTERVAL):
        self.state = state
        self.interval = interval
        self.watcher: Watcher | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.paused = ""
        self.error = ""
        self.last_poll = None
        self.polls = 0
        self._rebaseline = False
        self._mutex = threading.Lock()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> dict:
        if self.running:
            raise QcsuiteError("記録はすでに動いています。")
        device = self.state.device()
        self.watcher = Watcher(device, self.state.mock, self.interval)
        summary = self.watcher.start()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="qcsuite-watch", daemon=True)
        self.thread.start()
        return summary

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            if not self.state.connected:
                self.paused = "切断されたため止まりました"
                return
            self.tick()

    def tick(self) -> list[dict]:
        busy = self.state.jobs.busy()
        if busy:
            if self.paused != "job":
                self.watcher.session.log({"type": "pause", "segment": self.watcher.segment,
                                          "reason": f"ジョブ {busy} の実行中"})
            self.paused, self._rebaseline = "job", True
            return []
        device = self.watcher.device
        if not device._lock.acquire(blocking=False):
            self.paused = "busy"
            return []
        try:
            with self._mutex:
                if self._rebaseline:
                    self._rebaseline = False
                    self.watcher.rebaseline("ジョブが終わったので、今のグリッドから記録を再開しました")
                written = self.watcher.poll()
            self.paused, self.error = "", ""
            self.last_poll = _now()
            self.polls += 1
            return written
        except Exception as exc:
            # Recorded, shown in the status, and the loop keeps trying: one failed
            # read (the unit dropping a reply) should not end the session.
            self.error = f"{type(exc).__name__}: {exc}"
            self.watcher.session.log({"type": "error", "segment": self.watcher.segment,
                                      "error": self.error})
            return []
        finally:
            device._lock.release()

    def note(self, text: str) -> dict:
        with self._mutex:
            return self.watcher.note(text)

    def stop(self) -> dict:
        if self.watcher is None:
            raise QcsuiteError("記録は動いていません。")
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(5.0, self.interval * 3))
        with self._mutex:
            summary = self.watcher.stop()
        self.thread = None
        return summary

    def status(self) -> dict:
        w = self.watcher
        return {"running": self.running, "interval": self.interval,
                "session": w.session.id if w and w.session else None,
                "segment": w.segment if w else None, "paused": self.paused,
                "error": self.error, "last_poll": self.last_poll, "polls": self.polls,
                "pending": list(w.pending.values()) if w else []}
