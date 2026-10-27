"""The one door to the unit: :class:`Device`, over a real or a fake Quad Cortex.

``Device`` wraps either ``pyquadcortex.QuadCortex`` or
:class:`~qcsuite.device.fake.FakeQuadCortex` - they share an interface, so
nothing above this layer knows which it has. On top of the raw client it adds:

* **retries** for the reads the unit is known to drop (:mod:`.retry`);
* **the safety rules** for every write (:mod:`.guard`): writes outside the
  allowlist are refused before anything is sent, and destructive writes
  (delete, move, overwrite) only run as a step of a :mod:`qcsuite.plan`;
* **notices** for side effects the player must know about - a recall that
  changes the sound, a capture that will not play - collected in
  :attr:`Device.notices` for the CLI and web UI to show;
* **the known capture quirk**: ``set_block`` of a capture model raises
  ``BlockRefused`` although the block is placed (measured on CorOS 4.0.1), so a
  refusal is checked against the live grid before it is believed.

It deliberately exposes nothing that touches whole-unit settings (I/O, Global
EQ, firmware): there is no pass-through to the raw client's other methods.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import pyquadcortex as pq

from qcsuite.device import guard, inert
from qcsuite.device.guard import WritePolicy
from qcsuite.device.retry import retry
from qcsuite.errors import PlanError, VerifyFailed, WriteRefused
from qctone import device as qdevice

log = logging.getLogger("qcsuite.device")

SOUND_CHANGE = "sound_change"
INERT_WRITE = "inert_write"
RETRY = "retry"
INFO = "info"

#: Capture block models on the player's unit: 14000 is V1 ("Eltron 30" in the
#: catalog), 14001 is V2 ("Capture 2").
CAPTURE_MODELS = (14000, 14001)


@dataclass
class Notice:
    kind: str
    message: str
    at: float

    def to_dict(self):
        return {"kind": self.kind, "message": self.message,
                "at": time.strftime("%H:%M:%S", time.localtime(self.at))}


def folder_kind(key: str, is_factory: bool) -> str:
    if guard.is_user_folder(key):
        return "sandbox" if guard.is_sandbox(key) else "user"
    if key.startswith(guard.FACTORY_ROOT) and "Factory Library" in key:
        return "factory"
    if key == "local_nc_root" or key.endswith("_f"):
        return "captures"
    if "impulse" in key or key.endswith("_q") or key == "local_ir_root":
        return "irs"
    return "other"


class Device:
    """Retrying, guarded access to one Quad Cortex (real or fake)."""

    def __init__(self, qc, *, mock: bool, policy: WritePolicy | None = None,
                 retry_delay: float = 1.0):
        self.qc = qc
        self.mock = mock
        self.policy = policy or WritePolicy()
        self.retry_delay = retry_delay
        self.notices: list[Notice] = []
        self._lock = threading.RLock()

    # -- plumbing ------------------------------------------------------------

    def notice(self, kind: str, message: str):
        n = Notice(kind, message, time.time())
        self.notices.append(n)
        del self.notices[:-200]
        log.info("%s: %s", kind, message)
        return n

    def _retry(self, what, fn, attempts=3):
        def on_retry(_what, exc, attempt):
            self.notice(RETRY, f"{what}: 応答なし({exc})。再試行します({attempt}/{attempts})")
        with self._lock:
            return retry(fn, what=what, attempts=attempts, delay=self.retry_delay,
                         on_retry=on_retry)

    @staticmethod
    def setlist_arg(key: str) -> str:
        """The key a recall needs: the factory path carries its trailing slash."""
        if key.rstrip("/") == str(pq.Setlist.FACTORY).rstrip("/"):
            return str(pq.Setlist.FACTORY)
        return key

    # -- reads ----------------------------------------------------------------

    @property
    def catalog(self):
        return self._retry("カタログの取得", lambda: self.qc.catalog, attempts=4)

    def version(self) -> dict:
        v = self._retry("バージョンの取得", self.qc.version)
        return {"firmware": str(getattr(v, "app_fw_version", "?")),
                "serial": str(getattr(v, "device_serial_number", "")),
                "mock": self.mock}

    def folders(self, seconds: float = 20.0) -> list[dict]:
        with self._lock:
            raw = self.qc.list_folders(seconds)
        out = []
        for f in raw:
            key = f.key.rstrip("/") if f.key.startswith("/") else f.key
            out.append({"key": key, "name": f.name or guard.folder_name(key),
                        "slots": f.slots, "occupied": f.occupied,
                        "is_factory": f.is_factory, "kind": folder_kind(key, f.is_factory),
                        "protected": guard.folder_name(key) in guard.PROTECTED
                        and guard.is_user_folder(key)})
        return out

    def setlists(self) -> list[dict]:
        """User folders, sandbox folders and the Factory Library only."""
        return [f for f in self.folders() if f["kind"] in ("user", "sandbox", "factory")]

    def presets(self, key: str, include_empty: bool = False) -> list[dict]:
        entries = self._retry(f"一覧の取得({guard.folder_name(key)})",
                              lambda: self.qc.list_presets(self.setlist_arg(key),
                                                           include_empty=include_empty),
                              attempts=4)
        out = []
        for e in entries:
            index = e.index if e.HasField("index") else -1
            out.append({"index": index,
                        "slot": pq.position_to_slot(index) if 0 <= index < 256 else "",
                        "name": e.name if e.HasField("name") else "",
                        "instrument": e.instrument if e.HasField("instrument") else 0,
                        "key": e.key if e.HasField("key") else ""})
        return out

    def wait_for_listing(self, key: str, until, timeout: float = 45.0):
        interval = 2.0 if not self.mock else 0.01
        with self._lock:
            return self.qc.wait_for_listing(self.setlist_arg(key), until=until,
                                            timeout=timeout, interval=interval)

    def free_position(self, key: str) -> int:
        taken = {p["index"] for p in self.presets(key)}
        for i in range(256):
            if i not in taken:
                return i
        raise PlanError(f"「{guard.folder_name(key)}」に空きスロットがありません。")

    def current_preset(self):
        """The live grid; no side effects (read_current_preset)."""
        return self._retry("現在のグリッドの読み取り", self.qc.read_current_preset)

    def grid(self) -> dict:
        """qctone's JSON snapshot of the live grid."""
        return self._retry("グリッドの読み取り", lambda: qdevice.snapshot(self.qc))

    def active_scene(self) -> int:
        return int(self._retry("シーンの読み取り", self.qc.active_scene))

    def read_stored(self, key: str, position: int):
        """A stored preset. RECALLS it: the sound changes and unsaved edits are lost."""
        self.notice(SOUND_CHANGE,
                    f"「{guard.folder_name(key)}」{pq.position_to_slot(position)} を本体に読み込みます"
                    "(音が切り替わり、保存していない編集は消えます)")
        return self._retry("プリセットの読み込み",
                           lambda: self.qc.read_preset(self.setlist_arg(key), position),
                           attempts=2)

    def recall(self, key: str, position: int):
        self.notice(SOUND_CHANGE, f"「{guard.folder_name(key)}」{pq.position_to_slot(position)}"
                                  " を呼び出しました(音が切り替わります)")
        with self._lock:
            self.qc.recall_preset(self.setlist_arg(key), position)

    def switch_scene(self, scene: int):
        with self._lock:
            self.qc.switch_scene(int(scene))

    def captures(self) -> list[dict]:
        entries = self._retry("キャプチャー一覧", lambda: self.qc.captures(), attempts=3)
        return [{"index": e.index, "name": e.name, "key": e.key} for e in entries]

    def irs(self, folder: str | None = None) -> list[dict]:
        entries = self._retry("IR 一覧", lambda: self.qc.list_irs(folder), attempts=3)
        return [{"name": e.name, "key": e.key} for e in entries]

    def favorites(self) -> list[dict]:
        items = self._retry("お気に入りの取得", self.qc.favorites, attempts=2)
        return [{"name": i.name, "folder_key": i.folder_key, "folder_name": i.folder_name,
                 "is_factory": i.is_factory} for i in items]

    # -- grid edits (unsaved; the stored presets are not touched) -------------

    def apply_patch(self, patch: dict) -> dict:
        """Validate a qctone patch against the live grid, then apply it.

        Nothing is sent if any op is invalid. Capture placements that the unit
        reports as refused are checked against the grid (see module docstring).
        Returns the applied ops and any notices about writes that will not sound.
        """
        with self._lock:
            grid = self.grid()
            catalog = self.catalog
            ops = qdevice.validate(patch, grid, catalog)
            warnings = self._inert_warnings(ops, grid, catalog)
            explicit = {(op["row"], op["col"]) for op in ops if op["op"] == "bypass"}
            for op in ops:
                if op["op"] == "set_block":
                    self.place_block(op["row"], op["col"], op["model_id"])
                    if (op["row"], op["col"]) not in explicit:
                        # The bypass table outlives the block: a new block inherits
                        # whatever the cell last held (pyquadcortex bypass_state), so
                        # a block placed where a bypassed one sat would be silent.
                        self.qc.set_bypass(op["row"], op["col"], False)
                else:
                    qdevice.apply(self.qc, [op])
            for w in warnings:
                self.notice(INERT_WRITE, w)
            return {"applied": len(ops), "warnings": warnings}

    def _inert_warnings(self, ops, grid, catalog) -> list[str]:
        """Writes that will read back fine but not change the sound."""
        cells = {(b["row"], b["col"]): b["model_id"] for b in grid["blocks"]}
        out = []
        capture_knobs: dict = {}
        for op in ops:
            if op["op"] == "set_block":
                cells[(op["row"], op["col"])] = op["model_id"]
                model = catalog.get(op["model_id"])
                if op["model_id"] in CAPTURE_MODELS:
                    out.append(f"行{op['row'] + 1} 列{op['col'] + 1}: パソコンから置いたキャプチャー"
                               "ブロックは、本体の画面でキャプチャーを読み込むまで鳴りません。")
                elif inert.inert_plugin(model):
                    out.append(f"行{op['row'] + 1} 列{op['col'] + 1}: {model.name} は未所有の"
                               f"プラグイン({inert.inert_plugin(model)})なので、音は素通しになります。")
            elif op["op"] in ("set_param", "set_option"):
                model_id = cells.get((op["row"], op["col"]))
                if model_id in CAPTURE_MODELS:
                    capture_knobs.setdefault((op["row"], op["col"]), []).append(op["param"])
        for (row, col), names in capture_knobs.items():
            out.append(f"行{row + 1} 列{col + 1}: キャプチャーブロックのパラメーター"
                       f"({', '.join(names)})は、パソコンから書いても音に反映されません。")
        return out

    def place_block(self, row: int, col: int, model_id: int) -> dict:
        """``set_block``, believing a refusal only if the grid agrees."""
        with self._lock:
            try:
                self.qc.set_block(row, col, model_id)
                return {"placed": True, "refused_but_placed": False}
            except pq.BlockRefused:
                live = self.current_preset()
                here = next((b for b in pq.blocks(live)
                             if b.row == row and b.column == col), None)
                if here is not None and here.model_id == model_id:
                    if model_id not in CAPTURE_MODELS:
                        self.notice(INFO, f"行{row + 1} 列{col + 1}: 確認待ちがタイムアウトしましたが、"
                                          "ブロックは置かれていました。")
                    return {"placed": True, "refused_but_placed": True}
                raise

    def set_capture(self, row: int, col: int, capture: dict, model: int = 14000) -> dict:
        """Point a capture block at a library capture. Reads back; does NOT sound."""
        entry = type("CaptureEntry", (), {"key": capture["key"], "name": capture["name"]})
        with self._lock:
            self.place_block(row, col, model)
            self.qc.set_capture(row, col, entry, model=None)
        message = (f"行{row + 1} 列{col + 1} に「{capture['name']}」を書き込みました。読み戻しは正しく"
                   "見えますが、パソコンから読み込んだキャプチャーは音に反映されません。"
                   "本体の画面で同じキャプチャーを読み込んでください。")
        self.notice(INERT_WRITE, message)
        return {"written": True, "audible": False, "message": message}

    def set_text(self, row: int, col: int, param_index: int, text: str):
        """Write a string parameter (mic, IR, capture reference). Reads back; does
        not change the sound when written from the computer."""
        with self._lock:
            self.qc.set_param(row, col, param_index=param_index, text=text)

    def set_scene_label(self, scene: int, label: str):
        with self._lock:
            self.qc.set_scene_label(scene, label or None)

    def lane_volume_db(self, row: int, preset=None) -> float:
        p = preset if preset is not None else self.current_preset()
        value = p.chains[row].output_control[0].params[0].param_values[0].float_value
        return pq.lane_level_db(value)

    def set_lane_volume_db(self, row: int, db: float):
        """Row output VOLUME in dB (-40..+12). Edits the live grid only."""
        with self._lock:
            self.qc.set_lane_output(row, "VOLUME", value=pq.db_to_lane_level(db))

    def output_rows(self, preset=None) -> list[int]:
        """Rows whose output reaches Out 1/2 (directly or as MULTIPLE)."""
        p = preset if preset is not None else self.current_preset()
        reach = {int(pq.Output.XLR_1_2), int(pq.Output.XLR_1), int(pq.Output.XLR_2),
                 int(pq.Output.MULTIPLE)}
        return [i for i, c in enumerate(p.chains) if c.out_portid in reach]

    # -- guarded writes ---------------------------------------------------------

    def _existing_keys(self) -> set[str]:
        return {f["key"] for f in self.setlists()}

    def setlist_room(self) -> dict:
        """How many user setlists exist against the unit's limit.

        Neural DSP's support page numbers setlists 0 (Factory) and 1 (My
        Presets) to 12 and says up to 12 custom setlists can be stored; read
        conservatively, at most :data:`guard.SETLIST_LIMIT` user folders in all,
        My Presets included. The real limit is the first thing the hardware
        test plan checks; ``QCSUITE_SETLIST_LIMIT`` overrides it once known.
        """
        user = [f for f in self.setlists() if f["kind"] in ("user", "sandbox")]
        limit = guard.setlist_limit()
        return {"user_folders": len(user), "limit": limit, "free": limit - len(user),
                "names": [f["name"] for f in user]}

    def ensure_sandbox(self, name: str = guard.SANDBOX_NAME) -> str:
        """The sandbox folder's key, creating it the first time.

        Refuses to create it when the unit has no room for another setlist:
        the sandbox must never take the last slot the player might need.
        """
        key = guard.folder_key(name)
        if not guard.is_sandbox(key):
            raise WriteRefused(f"sandbox の名前は {guard.SANDBOX_PREFIX} で始めてください: {name}")
        with self._lock:
            if key in self._existing_keys():
                return key
            room = self.setlist_room()
            if room["free"] < 1:
                raise WriteRefused(
                    f"ユーザーのフォルダが {room['user_folders']} 個あり、上限({room['limit']} 個と"
                    "想定)に余裕がないため、sandbox を作らずに止めました。上限は実機テスト手順書の"
                    "最初の項目で確認してください(QCSUITE_SETLIST_LIMIT で変更できます)。")
            self.policy.check(key, "sandbox の作成")
            self.qc.create_setlist(guard.folder_name(key))
            self.notice(INFO, f"sandbox フォルダ「{guard.folder_name(key)}」を作りました。")
            deadline = time.monotonic() + (30 if not self.mock else 1)
            while key not in self._existing_keys():
                if time.monotonic() > deadline:
                    raise VerifyFailed(f"sandbox「{name}」を作りましたが、一覧に現れません。")
                time.sleep(1.0 if not self.mock else 0.01)
        return key

    def create_folder(self, name: str, *, step=None) -> str:
        if step is None:
            raise PlanError("フォルダの作成は計画(plan)経由で実行してください。")
        with self._lock:
            self.policy.check_create_folder(name, self._existing_keys())
            return self.qc.create_setlist(name)

    def delete_folder(self, name: str, *, step=None):
        if step is None:
            raise PlanError("フォルダの削除は計画(plan)経由で実行してください。")
        with self._lock:
            self.policy.check_delete_folder(name)
            return self.qc.delete_setlist(guard.folder_name(guard.folder_key(name)))

    def save_current(self, key: str, position: int, name: str, *, step=None,
                     instrument: int = 1) -> str | None:
        """Save the live grid into ``key``/``position``. Returns the stored name.

        An occupied slot is an overwrite, which only a plan step may do.
        """
        with self._lock:
            self.policy.check(key, "保存")
            occupied = {p["index"]: p["name"] for p in self.presets(key)}
            if position in occupied and step is None:
                raise PlanError(
                    f"{pq.position_to_slot(position)} には「{occupied[position]}」があります。"
                    "上書きは計画(plan)経由でのみ実行できます。")
            stored = self.qc.save_current_preset(key, position, name, instrument=instrument,
                                                 confirm=True)
        if stored is None:
            raise VerifyFailed(f"{pq.position_to_slot(position)} への保存が一覧で確認できません。")
        if stored != name:
            self.notice(INFO, f"同じ名前があったため、本体が「{name}」を「{stored}」として保存しました。")
        return stored

    def copy_preset(self, src_key: str, position: int, dst_key: str,
                    to_position: int | None = None, name: str | None = None, *,
                    step=None, adjust: dict | None = None) -> str | None:
        """Copy a preset. Recalls the source (the sound changes) and drops its tags.

        ``adjust`` edits the recalled grid before it is saved - so far only
        ``{"lane_db": {row: dB}}``, the row output levels the leveler corrects.
        The source preset itself is never written.
        """
        with self._lock:
            self.policy.check(dst_key, "コピー")
            if to_position is None:
                to_position = self.free_position(dst_key)
            occupied = {p["index"] for p in self.presets(dst_key)}
            if to_position in occupied and step is None:
                raise PlanError("コピー先のスロットが埋まっています。上書きは計画経由でのみ実行できます。")
            p = self.read_stored(src_key, position)
            source_name = p.name if pq.field_present(p, "name") else "copy"
            for row, db in ((adjust or {}).get("lane_db") or {}).items():
                self.set_lane_volume_db(int(row), float(db))
            return self.save_current(dst_key, to_position, name or source_name, step=step)

    def prepare_reamp(self, preset=None) -> list[int]:
        """Point the live grid's guitar-input rows at USB 5 so a reamp reaches them.

        Unsaved, like every grid edit: recalling the preset undoes it, and
        nothing here ever saves it. Returns the rows re-pointed.
        """
        p = preset if preset is not None else self.current_preset()
        guitar = {int(pq.Input.INPUT_1), int(pq.Input.INPUT_2), int(pq.Input.INPUT_1_2)}
        rows = [i for i, c in enumerate(p.chains) if c.in_portid in guitar]
        with self._lock:
            for row in rows:
                self.qc.set_chain_input(row, int(pq.Input.USB_5))
        return rows

    def delete_preset(self, key: str, name: str, *, step=None):
        if step is None:
            raise PlanError("削除は計画(plan)経由でのみ実行できます。")
        with self._lock:
            self.policy.check(key, "削除")
            self.qc.delete_preset(key, name)

    def move_preset(self, key: str, name: str, to_position: int, *, step=None):
        if step is None:
            raise PlanError("移動は計画(plan)経由でのみ実行できます。")
        with self._lock:
            self.policy.check(key, "移動")
            occupied = {p["index"]: p["name"] for p in self.presets(key)}
            if to_position in occupied:
                raise PlanError(f"移動先 {pq.position_to_slot(to_position)} には"
                                f"「{occupied[to_position]}」があります。")
            self.qc.move_preset(key, name, to_position)
