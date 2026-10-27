"""Research tools for docs/research-host-limits.md - looking, never changing.

Captures, cab mics and unowned plugins read back correctly when written from the
computer but do not change the sound (measured on the player's unit). Finding
out why starts with watching what the unit itself says, so this module is a
small set of instruments:

* ``sniff``: record every message the unit sends for N seconds while the player
  does something BY HAND on its screen (loads a capture, changes a mic, places a
  plugin block). Nothing is sent during the window; only the library's own
  keepalive keeps running. Undecodable pushes (pyquadcortex drops them before
  dispatch - License and CloudLogin bodies are not protobuf) are recorded too,
  as type, length and hash.
* ``outbound``: what pyquadcortex WOULD send for ``set_capture`` / a string
  write / ``set_block``, built offline with a recording transport - no unit
  needed. Diffing that against a sniff of the same change made by hand is
  experiment E1 in the memo.
* ``diff``: compare two logs by message type, field paths and values.
* ``dependencies``: the per-preset dependency records the unit attaches to
  listing entries (``ProductData.product_dependencies``), read-only.
* ``read-state``: ask for one read-only state type (License, ModuleStats, ...)
  exactly as the connect handshake already does, and record the reply.
* ``measure``: the audibility yardstick for the experiments - reamp the LIVE
  grid with a DI and log loudness, flatness, crest and the third-octave shape
  under a label, then compare two labels. Not read-only in the strict sense:
  the unit makes sound, and guitar-input rows are pointed at USB 5 (unsaved,
  undone by the next recall).

How the sniffer hooks in is NOT public API: it shadows three methods of
pyquadcortex 0.40.0's ``Transport`` (``_handle_message``, ``_dispatch``,
``send``) on the instance for the duration of the window and removes them after.
A different pyquadcortex version gets a clear error instead of a silent miss.

Under ``--mock`` there is no USB traffic at all. The log then holds the fake
unit's recorded method calls, every line marked ``"synthetic": true`` and the
header saying so - it exercises the tooling, it is not the unit's traffic.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

from qcsuite import paths

#: Message types left out of the log by default because they are periodic
#: chatter (their counts are still kept in the footer).
DEFAULT_EXCLUDE = ("KeepAlive", "IOMeter", "GridModelMeter", "CPULoad")
#: State types ``read-state`` may ask for. All of them are READ by the connect
#: handshake itself (pyquadcortex ``_SUBSCRIBE_TYPES``), so asking again changes
#: nothing on the unit.
READABLE_STATES = ("License", "ModuleStats", "CompilerInhibitedModules", "NewModels",
                   "PinnedModels")
STRING_LIMIT = 256
LIST_LIMIT = 256
SUPPORTED_PYQUADCORTEX = "0.40.0"


# -- turning messages into JSON lines ------------------------------------------------

def _digest(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()[:16]


def truncate(obj, string_limit: int = STRING_LIMIT, list_limit: int = LIST_LIMIT):
    """Shorten long strings and lists so a log stays readable, keeping a hash.

    A truncated string becomes ``{"_truncated": "string", "len", "sha256",
    "head"}``; a long list keeps its first ``list_limit`` items followed by
    ``{"_truncated": "list", "len"}``. Two logs can still be compared on the
    hashes even when the content was cut.
    """
    if isinstance(obj, str):
        if len(obj) <= string_limit:
            return obj
        return {"_truncated": "string", "len": len(obj), "sha256": _digest(obj),
                "head": obj[:48]}
    if isinstance(obj, list):
        items = [truncate(x, string_limit, list_limit) for x in obj[:list_limit]]
        if len(obj) > list_limit:
            items.append({"_truncated": "list", "len": len(obj)})
        return items
    if isinstance(obj, dict):
        return {k: truncate(v, string_limit, list_limit) for k, v in obj.items()}
    return obj


def type_name(message) -> str:
    """``GridMessage`` -> ``Grid`` (the CortexMessageType name)."""
    name = type(message).__name__
    return name[:-len("Message")] if name.endswith("Message") else name


def message_record(message, t: float, direction: str, **extra) -> dict:
    from google.protobuf import json_format
    data = json_format.MessageToDict(message, preserving_proto_field_name=True)
    record = {"kind": "msg", "t": round(t, 4), "dir": direction, "type": type_name(message),
              "request_id": data.pop("request_id", None), "action": data.get("action"),
              "data": truncate(data)}
    record.update(extra)
    return record


def _enum_name(number: int) -> str:
    from pyquadcortex.proto import ProductionAutomation_pb2 as pa
    enum = pa.CortexMessageType.DESCRIPTOR.enum_types_by_name["Enum"]
    value = enum.values_by_number.get(number)
    return value.name if value is not None else f"#{number}"


# -- the sniffer ------------------------------------------------------------------------

class Sniffer:
    """Record inbound (and outbound) messages of a real pyquadcortex client.

    Use as a context manager around the window to record. It never sends
    anything itself; ``outbound`` lines appear only for messages someone else
    sends through the same client during the window (the keepalive, or the
    writes of an experiment run in the same Python session).
    """

    def __init__(self, qc, *, exclude=DEFAULT_EXCLUDE, raw_dir: Path | None = None):
        self.transport = getattr(qc, "_t", None)
        missing = [a for a in ("_handle_message", "_dispatch", "send")
                   if not hasattr(self.transport, a)]
        if self.transport is None or missing:
            raise RuntimeError(
                "この pyquadcortex では通信の記録ができません(Transport の内部が想定と違います: "
                f"{missing or '_t'})。研究ツールは pyquadcortex {SUPPORTED_PYQUADCORTEX} 向けです。")
        self.exclude = set(exclude)
        self.raw_dir = raw_dir
        self.records: list[dict] = []
        self.excluded_counts: dict[str, int] = {}
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._current_raw = None
        self._dispatched = False

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _add(self, record: dict):
        with self._lock:
            self.records.append(record)

    def _skip(self, name: str) -> bool:
        if name in self.exclude:
            with self._lock:
                self.excluded_counts[name] = self.excluded_counts.get(name, 0) + 1
            return True
        return False

    def __enter__(self):
        from pyquadcortex import framing
        t = self.transport
        original_handle, original_dispatch, original_send = \
            t._handle_message, t._dispatch, t.send
        self._t0 = time.monotonic()

        def handle(reports):
            try:
                msg_type, payload = framing.decode_reports(reports)
                if payload[:2] == b"\x1f\x8b":
                    payload = gzip.decompress(payload)
                self._current_raw = (msg_type, payload)
            except (ValueError, OSError):
                self._current_raw = None
            self._dispatched = False
            try:
                return original_handle(reports)
            finally:
                if not self._dispatched and self._current_raw is not None:
                    self._record_raw(*self._current_raw)

        def dispatch(message):
            self._dispatched = True
            if not self._skip(type_name(message)):
                self._add(message_record(message, self._now(), "in"))
            return original_dispatch(message)

        def send(message):
            if not self._skip(type_name(message)):
                self._add(message_record(message, self._now(), "out"))
            return original_send(message)

        t._handle_message, t._dispatch, t.send = handle, dispatch, send
        return self

    def _record_raw(self, msg_type: int, payload: bytes):
        name = _enum_name(msg_type)
        if self._skip(name):
            return
        record = {"kind": "raw", "t": round(self._now(), 4), "dir": "in", "type": name,
                  "decoded": False, "len": len(payload), "sha256": _digest(payload),
                  "head_hex": payload[:32].hex()}
        if self.raw_dir is not None:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            path = self.raw_dir / f"{len(self.records):05d}_{name}.bin"
            path.write_bytes(payload)
            record["file"] = str(path)
        self._add(record)

    def __exit__(self, *exc):
        for name in ("_handle_message", "_dispatch", "send"):
            self.transport.__dict__.pop(name, None)
        return False


class FakeSniffer:
    """The mock's stand-in: the fake unit's recorded calls, marked synthetic."""

    def __init__(self, fake, *, exclude=DEFAULT_EXCLUDE, raw_dir=None):
        self.fake = fake
        self.records: list[dict] = []
        self.excluded_counts: dict[str, int] = {}
        self._start = 0
        self._t0 = 0.0

    def __enter__(self):
        self._start = len(self.fake.calls)
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        elapsed = round(time.monotonic() - self._t0, 4)
        for name, args, kwargs in self.fake.calls[self._start:]:
            self.records.append({
                "kind": "call", "synthetic": True, "t": elapsed, "dir": "host-call",
                "type": f"fake.{name}",
                "data": truncate(json.loads(json.dumps({"args": list(args), "kwargs": kwargs},
                                                       default=str)))})
        return False


def _header(seconds, mock: bool, exclude, note: str) -> dict:
    import pyquadcortex
    return {"kind": "header", "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": seconds, "mock": mock, "synthetic": mock,
            "pyquadcortex": pyquadcortex.__version__, "excluded_types": sorted(exclude),
            "note": note}


def _footer(sniffer) -> dict:
    by_type: dict[str, int] = {}
    for r in sniffer.records:
        key = f"{r['dir']}:{r['type']}"
        by_type[key] = by_type.get(key, 0) + 1
    return {"kind": "footer", "count": len(sniffer.records), "by_type": by_type,
            "excluded_counts": dict(sniffer.excluded_counts)}


MOCK_NOTE = ("モック: 実機の通信ではありません。FakeQuadCortex に対して呼ばれたメソッドの"
             "記録で、道具の動作確認のためのものです。")
REAL_NOTE = ("実機から受け取ったメッセージ(dir=in)と、同じ接続で送られたメッセージ(dir=out、"
             "キープアライブなど)。記録中、このツール自身は何も送っていません。")


def write_log(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_log(path) -> list[dict]:
    out = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{n} が JSON として読めません: {exc}") from exc
    return out


def _simulate(rig, action: str):
    """Mock only: a host-side change on the fake, standing in for the player's hand."""
    device = rig.device
    grid = device.grid()
    used = {(b["row"], b["col"]) for b in grid["blocks"]}
    free = next((r, c) for r in range(4) for c in range(8) if (r, c) not in used)
    if action == "capture":
        device.set_capture(free[0], free[1], device.captures()[0])
    elif action == "mic":
        cab = next((b for b in grid["blocks"] if (b["category"] or "").startswith("Cabsim")), None)
        if cab is None:
            raise RuntimeError("現在のグリッドにキャビネットがありません。")
        device.set_text(cab["row"], cab["col"], 0, "NG_412 Ribbon R121")
    elif action == "plugin":
        device.place_block(free[0], free[1], device.catalog.find("Plini Lead").id)
    else:
        raise ValueError(f"未知の --simulate: {action}")


def sniff(rig, seconds: float, *, exclude=DEFAULT_EXCLUDE, raw_dir: Path | None = None,
          simulate: str | None = None, on_tick=None) -> list[dict]:
    """Record for ``seconds``; returns the log lines (header, records, footer)."""
    if rig.mock:
        sniffer = FakeSniffer(rig.fake, exclude=exclude)
        note = MOCK_NOTE
    else:
        if simulate:
            raise ValueError("--simulate はモック専用です(実機では本体を手で操作してください)。")
        sniffer = Sniffer(rig.device.qc, exclude=exclude, raw_dir=raw_dir)
        note = REAL_NOTE
    with sniffer:
        if simulate:
            _simulate(rig, simulate)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if on_tick:
                on_tick(max(0.0, deadline - time.monotonic()))
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return [_header(seconds, rig.mock, exclude, note), *sniffer.records, _footer(sniffer)]


# -- what the library would send (offline) ----------------------------------------------

class RecordingTransport:
    """Enough of pyquadcortex's Transport for a client to build messages offline."""

    def __init__(self):
        self.sent = []
        self._ids = iter(range(1, 1_000_000))

    def next_request_id(self):
        return next(self._ids)

    def send(self, message):
        self.sent.append(message)

    def request(self, message, timeout=5.0):
        message.request_id = self.next_request_id()
        self.sent.append(message)
        raise TimeoutError("offline: no unit to answer")

    def await_broadcast(self, expected_class, trigger, timeout=40.0, match=None):
        trigger()
        raise TimeoutError("offline: no unit to answer")


def outbound(action: str, **kw) -> list[dict]:
    """The messages pyquadcortex 0.40.0 sends for one write, built without a unit."""
    from pyquadcortex.client import QuadCortex
    t = RecordingTransport()
    qc = QuadCortex(t)
    row, col = int(kw.get("row", 0)), int(kw.get("col", 0))
    if action == "set-capture":
        entry = type("Capture", (), {"key": kw["key"], "name": kw["name"]})
        qc.set_capture(row, col, entry, model=None)
    elif action == "set-text":
        qc.set_param(row, col, param_index=int(kw["index"]), text=kw["text"])
    elif action == "set-block":
        qc.set_block(row, col, int(kw["model"]), verify=False)
    elif action == "set-value":
        qc.set_param(row, col, param_index=int(kw["index"]), value=float(kw["value"]))
    else:
        raise ValueError(f"未知の操作: {action}")
    header = {"kind": "header", "offline": True, "mock": False, "synthetic": False,
              "note": "pyquadcortex がこの操作で送るメッセージ(本体なしで組み立てたもの)",
              "action": action, "args": {k: v for k, v in kw.items()}}
    return [header] + [message_record(m, 0.0, "out", offline=True) for m in t.sent]


# -- comparing two logs ---------------------------------------------------------------------

def _flatten(obj, prefix: str = "", out=None) -> dict:
    """``{"preset.chains[].models[].hash": {values...}}`` - indices collapsed."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        if "_truncated" in obj:
            out.setdefault(prefix, set()).add(f"<{obj['_truncated']} {obj.get('sha256', obj.get('len'))}>")
            return out
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten(v, f"{prefix}[]", out)
    else:
        out.setdefault(prefix, set()).add(json.dumps(obj, ensure_ascii=False))
    return out


def _by_type(records) -> dict:
    out: dict[str, list] = {}
    for r in records:
        if r.get("kind") in ("msg", "raw", "call"):
            out.setdefault(f"{r['dir']}:{r['type']}", []).append(r)
    return out


def diff(a: list[dict], b: list[dict], types: list[str] | None = None,
         max_values: int = 8) -> dict:
    """Message types, field paths and values that differ between two logs."""
    ta, tb = _by_type(a), _by_type(b)
    if types:
        keep = set(types)
        ta = {k: v for k, v in ta.items() if k.split(":", 1)[1] in keep}
        tb = {k: v for k, v in tb.items() if k.split(":", 1)[1] in keep}
    result = {
        "synthetic": any(r.get("synthetic") for r in a + b if r.get("kind") == "header"),
        "only_in_a": sorted(set(ta) - set(tb)),
        "only_in_b": sorted(set(tb) - set(ta)),
        "counts": {k: [len(ta.get(k, [])), len(tb.get(k, []))] for k in sorted(set(ta) | set(tb))},
        "fields": {},
    }
    for key in sorted(set(ta) & set(tb)):
        fa, fb = {}, {}
        for r in ta[key]:
            _flatten(r.get("data", {k: r[k] for k in ("len", "sha256") if k in r}), "", fa)
        for r in tb[key]:
            _flatten(r.get("data", {k: r[k] for k in ("len", "sha256") if k in r}), "", fb)
        changed = {}
        for path in sorted(set(fa) & set(fb)):
            if fa[path] != fb[path]:
                changed[path] = {"a": sorted(fa[path])[:max_values],
                                 "b": sorted(fb[path])[:max_values]}
        entry = {"paths_only_in_a": sorted(set(fa) - set(fb)),
                 "paths_only_in_b": sorted(set(fb) - set(fa)), "values_differ": changed}
        if any(entry.values()):
            result["fields"][key] = entry
    return result


def diff_text(d: dict) -> str:
    lines = []
    if d["synthetic"]:
        lines.append("注意: どちらかのログはモック(合成)です。実機の通信ではありません。")
    lines.append("メッセージの種類と件数(A / B):")
    for key, (na, nb) in d["counts"].items():
        mark = "  " if na and nb else ("A のみ" if na else "B のみ")
        lines.append(f"  {key:<34} {na:>4} / {nb:<4} {mark}")
    for key, entry in d["fields"].items():
        lines.append(f"[{key}]")
        for path in entry["paths_only_in_a"]:
            lines.append(f"  A にだけある項目: {path}")
        for path in entry["paths_only_in_b"]:
            lines.append(f"  B にだけある項目: {path}")
        for path, values in entry["values_differ"].items():
            lines.append(f"  値が違う: {path}: A={values['a']} B={values['b']}")
    return "\n".join(lines)


# -- read-only device queries -----------------------------------------------------------------

def dependencies(rig, folder: str) -> list[dict]:
    """Each preset's dependency records from the folder listing (read-only)."""
    from google.protobuf import json_format
    from qcsuite.device import guard
    key = guard.folder_key(folder)
    device = rig.device
    entries = device._retry("一覧の取得", lambda: device.qc.list_presets(
        device.setlist_arg(key)), attempts=4)
    out = []
    for e in entries:
        data = json_format.MessageToDict(e, preserving_proto_field_name=True)
        out.append({"index": data.get("index"), "name": data.get("name"),
                    "product_dependencies": data.get("product_dependencies", []),
                    "factory_dependencies": data.get("factory_dependencies", []),
                    "other_fields": sorted(k for k in data if k not in (
                        "index", "name", "product_dependencies", "factory_dependencies"))})
    return out


def read_state(rig, name: str, seconds: float = 5.0, raw_dir: Path | None = None) -> list[dict]:
    """Ask for one state type the connect handshake already reads; log the reply."""
    if name not in READABLE_STATES:
        raise ValueError(f"{name} は読み取り対象外です。使えるもの: {', '.join(READABLE_STATES)}")
    if rig.mock:
        return [_header(seconds, True, (), MOCK_NOTE),
                {"kind": "note", "synthetic": True, "type": name,
                 "text": "モックの本体はこの状態を持っていません(実機でのみ意味があります)。"}]
    from pyquadcortex.proto import ProductionAutomation_pb2 as pa
    cls = getattr(pa, f"{name}Message")
    sniffer = Sniffer(rig.device.qc, exclude=DEFAULT_EXCLUDE, raw_dir=raw_dir)
    with sniffer:
        rig.device.qc._t.send(cls(action=pa.MessageAction.READ))
        time.sleep(seconds)
    records = [r for r in sniffer.records if r["type"] == name or r["dir"] == "out"]
    return [_header(seconds, False, DEFAULT_EXCLUDE, REAL_NOTE), *records, _footer(sniffer)]


# -- the audibility yardstick ------------------------------------------------------------------

#: A shape change below this (RMS dB, 100 Hz-5 kHz) is read as "no audible change"
#: for the experiments. A judgement aid, not a fact: confirm by ear.
CHANGED_SHAPE_DB = 1.0
CHANGED_LUFS = 1.0


def measure_log(mock: bool) -> Path:
    return paths.sub("research", mock) / "measure.jsonl"


def measure(rig, label: str, di_name: str = "synthetic-riff", route: bool = True) -> dict:
    """Reamp the live grid and log what came back under ``label``."""
    from qcsuite.audio import di as di_mod
    from qcsuite.audio import reamp
    from qctone import features
    audio, sr = di_mod.load(di_name, rig.mock)
    rows = rig.device.prepare_reamp() if route else []
    dry = features.analyze_array(audio[:, None], sr)
    info = reamp.render_current(rig, audio, sr, dry)
    feat = info["features"]
    record = {"label": label, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "mock": rig.mock,
              "di": di_name, "rerouted_rows": rows, "loudness": info["loudness"],
              "flatness_1k_5k": feat["spectral"]["flatness_1k_5k"],
              "crest_db": feat["dynamics"]["crest_db"],
              "third_octave": feat["third_octave"], "passthrough": info["passthrough"],
              "render": info["render"]}
    with open(measure_log(rig.mock), "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def compare_measurements(mock: bool, label_a: str, label_b: str) -> dict:
    """Did the sound change between two labelled measurements? (latest of each)"""
    from qcsuite.audio.score import shape_rms_db
    rows = read_log(measure_log(mock)) if measure_log(mock).exists() else []
    found = {}
    for r in rows:
        if r["label"] in (label_a, label_b):
            found[r["label"]] = r
    missing = [x for x in (label_a, label_b) if x not in found]
    if missing:
        raise ValueError(f"測定 {missing} がありません(qcsuite research measure --label で記録)")
    a, b = found[label_a], found[label_b]
    shape = shape_rms_db({"third_octave": a["third_octave"]}, {"third_octave": b["third_octave"]})
    la, lb = a["loudness"]["lufs"], b["loudness"]["lufs"]
    lufs_delta = None if la is None or lb is None else round(lb - la, 2)
    changed = shape >= CHANGED_SHAPE_DB or (lufs_delta is not None and abs(lufs_delta) >= CHANGED_LUFS)
    return {"a": label_a, "b": label_b, "shape_diff_db": round(shape, 2),
            "lufs_delta": lufs_delta,
            "flatness_delta": round(b["flatness_1k_5k"] - a["flatness_1k_5k"], 4),
            "crest_delta_db": round(b["crest_db"] - a["crest_db"], 2),
            "passthrough": {label_a: a["passthrough"]["passthrough"],
                            label_b: b["passthrough"]["passthrough"]},
            "changed": changed, "mock": a["mock"] or b["mock"],
            "note": (f"形の差 {CHANGED_SHAPE_DB} dB 以上、または音量差 {CHANGED_LUFS} LU 以上を"
                     "「変わった」とみなす目安です。最後は耳で確かめてください。")}


# -- CLI -------------------------------------------------------------------------------------------

def _out_path(a, mock: bool, stem: str) -> Path:
    if a.out:
        return Path(a.out)
    return paths.sub("research", mock) / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"


def cmd_sniff(ctx, a):
    exclude = () if a.include_meters else DEFAULT_EXCLUDE
    with ctx.rig("research sniff") as rig:
        if not rig.mock:
            print(f"{a.seconds:g} 秒間、本体から届くメッセージを記録します。記録中に本体の画面で"
                  "操作してください(このツールは何も送りません)。", file=sys.stderr)
        records = sniff(rig, a.seconds, exclude=exclude,
                        raw_dir=Path(a.raw_dir) if a.raw_dir else None, simulate=a.simulate)
    path = write_log(_out_path(a, ctx.mock, "sniff"), records)
    footer = records[-1]
    ctx.out({"file": str(path), **footer},
            f"{footer['count']} 件を記録しました: {path}\n"
            + "\n".join(f"  {k}: {v}" for k, v in sorted(footer["by_type"].items())))


def cmd_outbound(ctx, a):
    records = outbound(a.action, row=a.row, col=a.col, key=a.key, name=a.name, index=a.index,
                       text=a.text, model=a.model, value=a.value)
    path = write_log(_out_path(a, ctx.mock, f"outbound-{a.action}"), records)
    ctx.out({"file": str(path), "messages": records[1:]},
            f"{len(records) - 1} 件のメッセージ: {path}")


def cmd_diff(ctx, a):
    d = diff(read_log(a.a), read_log(a.b), types=a.type)
    ctx.out(d, diff_text(d))


def cmd_dependencies(ctx, a):
    with ctx.rig("research dependencies") as rig:
        rows = dependencies(rig, a.folder)
    ctx.out(rows, "\n".join(
        f"{r['index']:>3} {r['name']}: product {len(r['product_dependencies'])} 件 / "
        f"factory {len(r['factory_dependencies'])} 件" for r in rows))


def cmd_read_state(ctx, a):
    with ctx.rig("research read-state") as rig:
        records = read_state(rig, a.name, a.seconds,
                             raw_dir=Path(a.raw_dir) if a.raw_dir else None)
    path = write_log(_out_path(a, ctx.mock, f"state-{a.name}"), records)
    ctx.out({"file": str(path), "records": records}, f"記録しました: {path}")


def cmd_measure(ctx, a):
    if a.compare:
        d = compare_measurements(ctx.mock, a.compare[0], a.compare[1])
        ctx.out(d, f"{d['a']} → {d['b']}: 形の差 {d['shape_diff_db']} dB、音量差 "
                   f"{d['lufs_delta']} LU、素通し {d['passthrough']} → "
                   f"{'変わった' if d['changed'] else '変わっていない'}(目安)")
        return
    if not a.label:
        raise SystemExit("--label か --compare A B を指定してください")
    with ctx.rig("research measure") as rig:
        ctx.warn_monitor(rig.mock)
        r = measure(rig, a.label, a.di, route=not a.no_route)
    ctx.out(r, f"[{r['label']}] {r['loudness']['lufs']} LUFS / flatness {r['flatness_1k_5k']} / "
               f"crest {r['crest_db']} dB / 素通し {r['passthrough']['passthrough']}"
               + ("(モック)" if r["mock"] else ""))


def add_cli(sub):
    p = sub.add_parser("research", help="研究用ツール(通信の記録・比較、鳴っているかの測定)")
    s = p.add_subparsers(dest="research_cmd", required=True)
    c = s.add_parser("sniff", help="本体から届くメッセージを N 秒間記録する(何も送らない)")
    c.add_argument("--seconds", type=float, default=30.0)
    c.add_argument("--out")
    c.add_argument("--include-meters", action="store_true",
                   help="メーター類とキープアライブも記録する")
    c.add_argument("--raw-dir", help="読めなかったメッセージの中身を保存するフォルダ(手元でのみ)")
    c.add_argument("--simulate", choices=["capture", "mic", "plugin"],
                   help="モック専用: 手の操作の代わりにモックへ書き込む")
    c.set_defaults(fn=cmd_sniff)
    c = s.add_parser("outbound", help="pyquadcortex が送るメッセージを本体なしで組み立てる")
    c.add_argument("action", choices=["set-capture", "set-text", "set-block", "set-value"])
    c.add_argument("--row", type=int, default=0)
    c.add_argument("--col", type=int, default=0)
    c.add_argument("--key", default="0" * 64, help="set-capture: キャプチャーの 64 文字のキー")
    c.add_argument("--name", default="Capture", help="set-capture: キャプチャーの名前")
    c.add_argument("--index", type=int, default=0, help="set-text / set-value: パラメーター番号")
    c.add_argument("--text", default="", help="set-text: 文字列")
    c.add_argument("--model", type=int, default=14000, help="set-block: モデル ID")
    c.add_argument("--value", type=float, default=0.5, help="set-value: 0..1")
    c.add_argument("--out")
    c.set_defaults(fn=cmd_outbound)
    c = s.add_parser("diff", help="2 つの記録を比べる")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--type", action="append", help="比べるメッセージの種類(例: Grid)。複数可")
    c.set_defaults(fn=cmd_diff)
    c = s.add_parser("dependencies", help="一覧に付いている依存情報を表示(読み取りのみ)")
    c.add_argument("folder")
    c.set_defaults(fn=cmd_dependencies)
    c = s.add_parser("read-state", help="接続時にも読む状態を 1 つ読み直して記録する")
    c.add_argument("name", choices=list(READABLE_STATES))
    c.add_argument("--seconds", type=float, default=5.0)
    c.add_argument("--raw-dir", help="読めなかった返事(License など)の中身を保存するフォルダ")
    c.add_argument("--out")
    c.set_defaults(fn=cmd_read_state)
    c = s.add_parser("measure", help="今のグリッドを DI でリアンプして測る(本体から音が出ます)")
    c.add_argument("--label", help="この測定の名前(例: before, after-save-recall)")
    c.add_argument("--di", default="synthetic-riff", help="DI ライブラリの名前かファイル")
    c.add_argument("--no-route", action="store_true",
                   help="行の入力を USB 5 に付け替えない(すでに USB 5 のとき)")
    c.add_argument("--compare", nargs=2, metavar=("A", "B"), help="2 つの測定を比べる")
    c.set_defaults(fn=cmd_measure)


__all__ = ["Sniffer", "FakeSniffer", "sniff", "outbound", "diff", "diff_text", "truncate",
           "read_log", "write_log", "dependencies", "read_state", "measure",
           "compare_measurements", "add_cli"]
