"""A Quad Cortex that lives in memory: same methods, same return types, same traps.

:class:`FakeQuadCortex` implements the part of ``pyquadcortex.QuadCortex`` that
qcsuite uses, with the same signatures, returning the same protobuf types
(``BinaryPreset``, listing ``ProductData`` entries, :class:`pyquadcortex.Folder`),
so code written against it runs unchanged on the real unit. The composite
operations (``copy_preset``, ``set_capture``, ``set_ir``, ``set_param_option``,
``find_preset``) are the library's OWN code, called on the fake - so they compose
exactly as they do on hardware.

What it reproduces, and where each behaviour comes from:

* **pyquadcortex docstrings**: ``read_preset`` recalls (it replaces the live grid,
  resets the scene, and counts an audio interruption); ``read_current_preset``
  has no side effects; listings and file operations are eventually consistent,
  so ``wait_for_listing`` is needed; a save into a setlist that already holds the
  name is de-duplicated (base truncated, ``_N`` appended, 20 characters total);
  saves drop tags; a DSP-overfull ``set_block`` is refused; a favourite that does
  not match the device's record is ignored silently (so ``verify`` times out);
  the first read after connecting is sometimes dropped.
* **Facts measured on the player's unit (CorOS 4.0.1, firmware d14e)**:
  the catalog fetch times out now and then (``catalog_timeouts``); ``set_block``
  of a capture model (14000/14001) raises ``BlockRefused`` although the block
  IS placed; a capture's ``file_name``, the capture block's own knobs, and
  string parameters such as a cab's ``ir selector`` read back as written but do
  NOT change the sound; unowned plugin blocks pass audio through; a new folder
  lists as 0/256.

The difference between "what reads back" and "what sounds" is kept explicitly:
:meth:`audible_chain` returns what the DSP in :mod:`qcsuite.audio.dsp` should
play, which is how the fake reamp reproduces those traps.

**Guesses, labelled as such** (no fact either way): whether host-written
captures/strings start sounding after save + recall (``strings_apply_on_recall``,
default False = they stay inert); that ``move_preset`` onto an occupied slot is
ignored; that a save into the slot already holding the same name is not renamed;
that ``set_ir`` is inert like other string parameters (``ir_is_inert``); the DSP
budget numbers.
"""

from __future__ import annotations

import copy
import pickle
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyquadcortex as pq
from pyquadcortex.client import QuadCortex as _RealQC
from pyquadcortex.proto import Preset_pb2
from pyquadcortex.proto import ProductionAutomation_pb2 as pa

from qcsuite.device import fake_data as data
from qcsuite.device.guard import FORBIDDEN_METHODS, forbid

ProductData = type(pa.FileMessage().folder.files.add())
FavoriteItem = type(pa.RecentsFavoritesMessage().items.add())

SCENES = 8
ROWS, COLUMNS = 4, 8
NAME_LIMIT = 20


@dataclass
class FakeConfig:
    """How the fake behaves. ``speed`` scales every simulated delay (0 = instant)."""

    speed: float = 0.0
    seed: int = 7
    catalog_timeouts: int = 1
    listing_drop_rate: float = 0.0
    first_read_dropped: bool = True
    file_lag_listings: int = 1
    cortex_control_open: bool = False
    owned_plugins: tuple = data.OWNED_PLUGINS
    strings_apply_on_recall: bool = False
    ir_is_inert: bool = True


@dataclass
class Stored:
    """One occupied slot: what a listing shows plus the preset itself."""

    name: str
    preset: object = None              # BinaryPreset, built lazily
    recipe: dict | None = None
    instrument: int = 1
    tags: list = field(default_factory=list)
    taint: dict = field(default_factory=dict)   # cell -> audible override


class FakeQuadCortex:
    """In-memory stand-in for :class:`pyquadcortex.QuadCortex`."""

    CAPTURES_LIBRARY = data.CAPTURES_KEY
    IR_LIBRARY = data.IR_LIBRARY
    USER_IRS = data.USER_IRS
    CAPTURE_FILE_NAME_PARAM = 5
    IR_PATH_PARAMS = (2, 10)
    IR_NAME_PARAMS = (22, 23)
    LANE_OUTPUT_CONTROL = data.LANE_OUTPUT
    INPUT_GATE_CONTROL = data.INPUT_GATE

    def __init__(self, config: FakeConfig | None = None):
        self.config = config or FakeConfig()
        self._rng = random.Random(self.config.seed)
        self._lock = threading.RLock()
        self._catalog_obj = data.build_catalog()
        self._catalog_failures = 0
        self._first_read_pending = self.config.first_read_dropped
        self.calls: list[tuple] = []
        self.audio_interruptions = 0
        self.closed = False
        self.folders: dict[str, dict[int, Stored]] = {}
        self.folder_names: dict[str, str] = {}
        self._stale: dict[str, list] = {}       # key -> [entries snapshot, remaining reads]
        self._captures = [(f, k, n) for f, k, n in data.capture_entries(self._catalog_obj)]
        self._irs = data.ir_entries()
        self._seed_folders()
        self.favorites_list = [
            ("Two Rock Mayer", f"{data.USER_ROOT}/My Presets", "My Presets", False),
            ("Cory Wong Funk Tour", f"{data.USER_ROOT}/Cory Wong", "Cory Wong", False),
            ("Fuzz This", data.FACTORY_KEY, "Factory Library", True),
        ]
        self.recents_list: list[tuple] = []
        self.scene = 0
        self.live = self._empty_preset("New Preset")
        self.live_audible: dict = {}
        self.loaded_from = None
        self.dirty = False
        self._recall(data.FACTORY_KEY, 0, count=False)

    # -- seeding -----------------------------------------------------------

    def _seed_folders(self):
        for name, presets in data.USER_FOLDERS.items():
            key = f"{data.USER_ROOT}/{name}"
            self.folder_names[key] = name
            slots = {}
            position = 0
            for preset_name in presets:
                if name == "My Presets" and position == 12:
                    position += 1          # a gap, like a deleted preset leaves
                source = data.IDENTICAL.get((name, preset_name), (name, preset_name))
                slots[position] = Stored(name=preset_name, recipe={
                    "folder": source[0], "name": source[1]})
                position += 1
            self.folders[key] = slots
        self.folder_names[data.FACTORY_KEY] = "Factory Library"
        self.folders[data.FACTORY_KEY] = {
            i: Stored(name=n, recipe={"folder": "Factory Library", "name": n},
                      tags=["Factory", ["Clean", "Crunch", "Lead"][i % 3]])
            for i, n in enumerate(data.factory_names(self._catalog_obj))
        }

    def _materialize(self, stored: Stored):
        if stored.preset is None:
            r = data.recipe(stored.recipe["folder"], stored.recipe["name"],
                            self._catalog_obj, self._captures)
            stored.preset = self._build(stored.name, r)
            stored.instrument = r["instrument"]
        return stored.preset

    def _empty_preset(self, name: str):
        p = Preset_pb2.BinaryPreset()
        p.name = name
        lane = self._catalog_obj[data.LANE_OUTPUT]
        gate = self._catalog_obj[data.INPUT_GATE]
        for row in range(ROWS):
            chain = p.chains.add()
            chain.in_portid = int(pq.Input.INPUT_1) if row == 0 else int(pq.Input.EMPTY)
            chain.out_portid = int(pq.Output.XLR_1_2) if row == 0 else int(pq.Output.EMPTY)
            for _ in range(COLUMNS):
                chain.models.add()
            oc = chain.output_control.add()
            oc.hash = data.LANE_OUTPUT
            for spec in lane.parameters:
                self._fill_param(oc.params.add(), spec, spec.default)
            self._fill_param(oc.params.add(), None, 0.0)       # unidentified index 4
            ic = chain.input_control.add()
            ic.hash = data.INPUT_GATE
            for spec in gate.parameters:
                self._fill_param(ic.params.add(), spec, self._default_value(spec))
            scp = chain.split_control_points.add() if row % 2 == 0 else None
            if scp is not None:
                scp.split = -1
                scp.mix = -1
            bp = p.bypass.add()
            for _ in range(COLUMNS):
                cb = bp.colBypass.add()
                for _ in range(SCENES):
                    cb.sceneBypass.add().bypass = False
        for _ in range(SCENES):
            p.scene_labels.append(pq.SCENE_UNLABELLED)
        p.default_scene = 0
        return p

    @staticmethod
    def _default_value(spec):
        if spec.option_count or spec.range_is_placeholder or spec.type in ("text", "empty"):
            return spec.default
        return spec.to_normalized(spec.default)

    @staticmethod
    def _fill_param(prm, spec, value, text: str | None = None):
        del prm.param_values[:]
        for _ in range(SCENES):
            pv = prm.param_values.add()
            if text is not None or (spec is not None and spec.type == "text"):
                pv.string_value = text or ""
            else:
                pv.float_value = float(value)
        if spec is not None:
            options = data.OPTION_NAMES.get(spec.name)
            if options and spec.option_count:
                del prm.dynamic_steps[:]
                prm.dynamic_steps.extend(options[: spec.option_count])

    def _place(self, p, row, col, model_id, knobs=None, texts=None):
        model = self._catalog_obj[model_id]
        m = p.chains[row].models[col]
        m.Clear()
        m.hash = model_id
        for spec in model.parameters:
            text = (texts or {}).get(spec.name)
            if spec.type == "text":
                self._fill_param(m.params.add(), spec, 0.0, text=text or "")
            else:
                value = (knobs or {}).get(spec.name, self._default_value(spec))
                self._fill_param(m.params.add(), spec, value)

    def _build(self, name: str, r: dict):
        p = self._empty_preset(name)
        for row, col, model_id, knobs in r["blocks"]:
            texts = {}
            model = self._catalog_obj[model_id]
            if model.category.startswith("Cabsim"):
                short = model.name.split(" ")[0]
                texts = {"ir selector": f"NG_{short}_Dynamic 57",
                         "ir selector 2": f"NG_{short}_Condenser U47"}
            if r["capture"] and r["capture"][:2] == (row, col):
                _, key, cap_name = r["capture"][2]
                texts = {"file_name": f"{key}{cap_name}"}
            self._place(p, row, col, model_id, knobs, texts)
        p.chains[0].in_portid = int(pq.Input[r["in"]])
        p.chains[0].out_portid = int(pq.Output[r["out"]])
        vol = p.chains[0].output_control[0].params[0]
        for pv in vol.param_values:
            pv.float_value = pq.db_to_lane_level(r["lane_db"])
        del p.scene_labels[:]
        p.scene_labels.extend(r["scene_labels"])
        for (row, col), states in r["scene_bypass"].items():
            cb = p.bypass[row].colBypass[col]
            cb.sceneMode = True
            for s, state in enumerate(states):
                cb.sceneBypass[s].bypass = state
        return p

    # -- timing and faults ---------------------------------------------------

    def _wait(self, seconds: float):
        if self.config.speed > 0:
            time.sleep(seconds * self.config.speed)

    def _log(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def _maybe_drop_first_read(self, what: str, timeout: float):
        if self._first_read_pending:
            self._first_read_pending = False
            self._wait(timeout)
            raise TimeoutError(f"no {what} broadcast within {timeout}s (first read after "
                               "connecting dropped - fake)")

    # -- lifecycle -----------------------------------------------------------

    def close(self):
        self.closed = True

    def disconnect(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        if name in FORBIDDEN_METHODS:
            return lambda *a, **k: forbid(name)
        raise AttributeError(name)

    def save_state(self, path: Path):
        """Persist the fake's storage so separate CLI runs see the same unit."""
        with self._lock:
            state = {"folders": self.folders, "names": self.folder_names,
                     "favorites": self.favorites_list, "live": self.live.SerializeToString(),
                     "audible": self.live_audible, "scene": self.scene,
                     "loaded_from": self.loaded_from}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump(state, f)
            tmp.replace(path)

    def load_state(self, path: Path):
        with open(path, "rb") as f:
            state = pickle.load(f)
        with self._lock:
            self.folders = state["folders"]
            self.folder_names = state["names"]
            self.favorites_list = state["favorites"]
            self.live = Preset_pb2.BinaryPreset.FromString(state["live"])
            self.live_audible = state["audible"]
            self.scene = state["scene"]
            self.loaded_from = state["loaded_from"]

    # -- read ----------------------------------------------------------------

    @property
    def catalog(self):
        if self._catalog_failures < self.config.catalog_timeouts:
            self._catalog_failures += 1
            self._wait(25.0)
            raise TimeoutError("no ModelRepoMessage broadcast within 25.0s")
        self._wait(2.0)
        return self._catalog_obj

    def version(self, timeout: float = 10.0):
        msg = pa.VersionMessage(app_fw_version=data.FIRMWARE)
        msg.device_serial_number = "FAKE-QC-0001"
        return msg

    def list_folders(self, seconds: float = 20.0) -> list:
        self._log("list_folders")
        self._wait(min(seconds, 15.0))
        with self._lock:
            out = []
            for key, slots in self.folders.items():
                out.append(pq.Folder(key=key, name=self.folder_names[key], slots=256,
                                     occupied=len(slots),
                                     is_factory=key == data.FACTORY_KEY))
            groups: dict[str, list] = {}
            for folder, _, name in self._captures:
                groups.setdefault(folder, []).append(name)
            out.append(pq.Folder(key=data.CAPTURES_KEY, name="Captures Library",
                                 slots=len(self._captures), occupied=len(self._captures),
                                 is_factory=True))
            for folder, names in groups.items():
                out.append(pq.Folder(key=folder, name=names[0].rsplit(" ", 1)[0],
                                     slots=len(names), occupied=len(names), is_factory=True))
            out.append(pq.Folder(key="/opt/neuraldsp/impulse_responses", name="impulse_responses",
                                 slots=588, occupied=588, is_factory=True))
            return sorted(out, key=lambda f: f.key)

    def _entries(self, key: str, include_empty: bool) -> list:
        if key == data.CAPTURES_KEY:
            return [ProductData(index=i, name=n, key=k)
                    for i, (_, k, n) in enumerate(self._captures)]
        if key.endswith("_f"):
            return [ProductData(index=i, name=n, key=k) for i, (_, k, n) in
                    enumerate(c for c in self._captures if c[0] == key)]
        slots = self.folders[key]
        out = []
        for i in range(256):
            if i in slots:
                s = slots[i]
                out.append(ProductData(index=i, name=s.name, instrument=s.instrument,
                                       key=f"{key}/{s.name}.pb"))
            elif include_empty:
                out.append(ProductData(index=i))
        return out

    def list_presets(self, setlist: str = pq.Setlist.USER, timeout: float = 25.0,
                     include_empty: bool = False) -> list:
        self._log("list_presets", str(setlist))
        key = str(setlist).rstrip("/")
        with self._lock:
            known = key in self.folders or key == data.CAPTURES_KEY or (
                key.endswith("_f") and any(c[0] == key for c in self._captures))
            drop = self._rng.random() < self.config.listing_drop_rate
        if not known or drop:
            # The device sends nothing for an unknown key, and sometimes for a known one.
            self._wait(timeout)
            raise TimeoutError(f"no FileMessage broadcast within {timeout}s")
        self._wait(1.0)
        with self._lock:
            stale = self._stale.get(key)
            if stale is not None:
                stale[1] -= 1
                if stale[1] <= 0:
                    del self._stale[key]
                entries = stale[0]
            else:
                entries = self._entries(key, include_empty=True)
            if include_empty:
                return [copy.deepcopy(e) for e in entries]
            return [copy.deepcopy(e) for e in entries if e.HasField("name") and e.name]

    def find_preset(self, name: str, setlist: str = pq.Setlist.USER, timeout: float = 25.0):
        return _RealQC.find_preset(self, name, setlist, timeout)

    def wait_for_listing(self, setlist: str = pq.Setlist.USER, until=None,
                         timeout: float = 45.0, interval: float = 2.0):
        """Same contract as the library's: poll, ride out missed pushes, two diagnoses."""
        polls = max(2, int(timeout / max(interval, 0.01)))
        previous, seen, missed = None, 0, 0
        for _ in range(polls + 1):
            try:
                entries = self.list_presets(setlist)
            except TimeoutError:
                missed += 1
            else:
                seen += 1
                if until is not None:
                    if until(entries):
                        return entries
                else:
                    signature = [(e.index, e.name) for e in entries]
                    if previous is not None and signature == previous:
                        return entries
                    previous = signature
            self._wait(interval)
        if seen == 0:
            raise TimeoutError(
                f"the device stopped pushing listings for {str(setlist)!r}: {missed} "
                f"attempt(s) timed out. The condition was never evaluated.")
        raise TimeoutError(f"the condition never became true for {str(setlist)!r} "
                           f"within {timeout}s ({seen} listing(s) checked)")

    def read_preset(self, setlist_path: str, position, is_factory: bool = None,
                    timeout: float = 40.0):
        self._log("read_preset", setlist_path, position)
        self._wait(12.0)
        with self._lock:
            self._recall(str(setlist_path).rstrip("/"), _as_position(position))
            return copy.deepcopy(self.live)

    def recall_preset(self, setlist_path: str, position, is_factory: bool = None,
                      request_id: int = None):
        self._log("recall_preset", setlist_path, position)
        self._wait(1.0)
        with self._lock:
            self._recall(str(setlist_path).rstrip("/"), _as_position(position))

    def _recall(self, key: str, position: int, count: bool = True):
        slots = self.folders.get(key, {})
        stored = slots.get(position)
        if stored is None:
            return                          # an empty slot recalls nothing
        p = copy.deepcopy(self._materialize(stored))
        p.name = stored.name
        self.live = p
        self.scene = int(p.default_scene)
        self.loaded_from = (key, position)
        self.dirty = False
        self.live_audible = self._audible_from_preset(p)
        if not self.config.strings_apply_on_recall:
            self.live_audible.update(copy.deepcopy(stored.taint))
        if count:
            self.audio_interruptions += 1
            entry = (stored.name, key, self.folder_names.get(key, ""), key == data.FACTORY_KEY)
            self.recents_list = [entry] + [e for e in self.recents_list if e != entry][:49]

    def read_current_preset(self, timeout: float = 15.0):
        self._log("read_current_preset")
        self._maybe_drop_first_read("RecallPreset", timeout)
        self._wait(0.3)
        with self._lock:
            return copy.deepcopy(self.live)

    def active_scene(self, timeout: float = 10.0):
        return pq.Scene(self.scene)

    def preset_dirty(self, timeout: float = 5.0) -> bool:
        return self.dirty

    # -- audibility ----------------------------------------------------------

    def _audible_from_preset(self, p) -> dict:
        """What each occupied cell plays, taken from the preset as stored."""
        out = {}
        for b in pq.blocks(p):
            out[(b.row, b.column)] = self._cell_sound(p, b.row, b.column, b.model_id)
        return out

    def _cell_sound(self, p, row, col, model_id) -> dict:
        model = self._catalog_obj.get(model_id)
        params, texts = {}, {}
        if model is not None:
            for spec in model.parameters:
                try:
                    values = pq.param_state(p, row, col, spec.index).values
                except IndexError:
                    continue
                value = values[self.scene] if self.scene < len(values) else values[0]
                if isinstance(value, str):
                    texts[spec.name] = value
                elif value is not None:
                    params[spec.name] = value
        sound = {"model_id": model_id, "params": params, "texts": texts,
                 "passthrough": False, "reason": ""}
        if model is not None and data.is_unowned_plugin(model) and \
                data.plugin_owner(model) not in self.config.owned_plugins:
            sound.update(passthrough=True, reason=f"未所有のプラグイン({model.plugin_id})")
        return sound

    def _touch(self, row, col, *, audible: bool):
        """Record a host write. Inaudible writes leave the cell's sound as it was."""
        self.dirty = True
        model_id = self.live.chains[row].models[col].hash
        if not model_id:
            self.live_audible.pop((row, col), None)
            return
        if audible:
            current = self.live_audible.get((row, col))
            fresh = self._cell_sound(self.live, row, col, model_id)
            if current and current.get("inert_since_host_write"):
                # Strings and capture knobs written earlier stay as they sounded.
                fresh["texts"] = current["texts"]
                fresh["inert_since_host_write"] = True
            self.live_audible[(row, col)] = fresh

    def audible_chain(self) -> dict:
        """What the DSP should play: routing, blocks as they SOUND, scene bypass."""
        with self._lock:
            p = self.live
            rows = []
            for i, chain in enumerate(p.chains):
                lane = chain.output_control[0] if len(chain.output_control) else None
                lane_values = {}
                if lane is not None:
                    names = [s.name for s in self._catalog_obj[data.LANE_OUTPUT].parameters]
                    for idx, prm in enumerate(lane.params[: len(names)]):
                        lane_values[names[idx]] = prm.param_values[0].float_value
                blocks = []
                for b in sorted((b for b in pq.blocks(p) if b.row == i), key=lambda b: b.column):
                    sound = copy.deepcopy(self.live_audible.get(
                        (b.row, b.column),
                        self._cell_sound(p, b.row, b.column, b.model_id)))
                    state = pq.bypass_state(p, b.row, b.column)
                    model = self._catalog_obj.get(b.model_id)
                    sound.update(col=b.column, bypassed=state.scenes[self.scene],
                                 category=model.category if model else "",
                                 name=model.name if model else str(b.model_id))
                    blocks.append(sound)
                rows.append({"row": i, "in_port": pq.Input(chain.in_portid).name,
                             "out_port": pq.Output(chain.out_portid).name,
                             "lane": lane_values, "blocks": blocks})
            return {"scene": self.scene, "rows": rows}

    # -- grid edits (unsaved) -----------------------------------------------

    def _spec(self, row, col, param_index=None, param=None, model=None):
        model_id = self.live.chains[row].models[col].hash
        if not model_id:
            return None, None
        resolved = self._catalog_obj.get(model_id)
        if param is not None and not isinstance(param, int):
            spec = resolved.parameter(param)
        else:
            index = param if isinstance(param, int) else param_index
            spec = resolved.parameters[index] if resolved and index < len(resolved.parameters) else None
        return resolved, spec

    def set_param(self, row: int, column: int, param_index=None, value: float = 0.0,
                  scene=None, param=None, model=None, real=None, promote: bool = True,
                  text: str = None):
        self._log("set_param", row, column, param_index=param_index, param=param,
                  value=value, real=real, text=text, scene=scene)
        if text is not None and real is not None:
            raise TypeError("pass text= or real=, not both")
        with self._lock:
            resolved, spec = self._spec(row, column, param_index, param, model)
            if real is not None:
                if param is None or model is None:
                    raise TypeError("real= needs param= and model= so the parameter's "
                                    "range is known; pass value= for a normalized 0..1 float")
                given = model if hasattr(model, "parameter") else self._catalog_obj[int(model)]
                spec = given.parameter(param) if isinstance(param, str) else given.parameters[param]
                value = spec.to_normalized(real)
            if spec is None:
                if param_index is None and param is None:
                    raise TypeError("set_param needs either param_index or param=<name> with model=")
                return None       # the device ignores a write to an empty cell
            index = spec.index
            if scene is not None:
                if promote:
                    self.set_param_scene_mode(row, column, index, True)
                self.switch_scene(scene)
            prm = self.live.chains[row].models[column].params[index]
            targets = [self.scene] if prm.scene_mode else range(SCENES)
            for s in targets:
                if text is not None:
                    prm.param_values[s].string_value = text
                else:
                    prm.param_values[s].float_value = float(value)
            inert = text is not None or (resolved and resolved.id in data.CAPTURE_MODELS)
            if inert:
                self.dirty = True
                sound = self.live_audible.get((row, column))
                if sound is not None:
                    sound["inert_since_host_write"] = True
            else:
                self._touch(row, column, audible=True)
        return None

    def set_param_scene_mode(self, row: int, column: int, param_index: int,
                             enabled: bool = True):
        with self._lock:
            self.live.chains[row].models[column].params[param_index].scene_mode = enabled

    def set_param_option(self, row: int, column: int, param, option, source):
        return _RealQC.set_param_option(self, row, column, param, option, source)

    def switch_scene(self, scene: int):
        self._log("switch_scene", int(scene))
        with self._lock:
            self.scene = int(scene)
            for (row, col), sound in list(self.live_audible.items()):
                if sound.get("passthrough") or sound.get("inert_since_host_write"):
                    continue
                self.live_audible[(row, col)] = self._cell_sound(
                    self.live, row, col, sound["model_id"])

    def set_scene_label(self, scene_index: int, label):
        with self._lock:
            self.live.scene_labels[scene_index] = label if label else pq.SCENE_UNLABELLED
            self.dirty = True

    def _dsp_used(self, exclude=None) -> int:
        total = 0
        for b in pq.blocks(self.live):
            if (b.row, b.column) == exclude:
                continue
            model = self._catalog_obj.get(b.model_id)
            total += data.DSP_COST.get(model.category if model else "", 5)
        return total

    def set_block(self, row: int, column: int, model, verify: bool = True,
                  timeout: float = 5.0):
        model_id = int(getattr(model, "id", model))
        self._log("set_block", row, column, model_id)
        with self._lock:
            found = self._catalog_obj.get(model_id)
            cost = data.DSP_COST.get(found.category if found else "", 5)
            if found is None or self._dsp_used(exclude=(row, column)) + cost > data.DSP_BUDGET:
                placed = False
            else:
                self._place(self.live, row, column, model_id)
                sound = self._cell_sound(self.live, row, column, model_id)
                if model_id in data.CAPTURE_MODELS:
                    sound.update(passthrough=True, reason="パソコンから置いたキャプチャー(未読み込み)")
                self.live_audible[(row, column)] = sound
                self.dirty = True
                placed = True
        if not verify:
            return None
        self._wait(0.3)
        if not placed or model_id in data.CAPTURE_MODELS:
            # Fact 3: a capture block IS placed, but its echo never arrives.
            self._wait(timeout)
            raise pq.BlockRefused(
                f"the device did not accept {model_id} at row {row} column {column}: "
                f"no Grid echo within {timeout}s. The usual cause is that the preset "
                f"has no DSP capacity left for this block - try a cheaper one, or free "
                f"a block. Pass verify=False to send without checking.")
        return None

    def remove_block(self, row: int, column: int):
        self._log("remove_block", row, column)
        with self._lock:
            self.live.chains[row].models[column].Clear()
            self.live_audible.pop((row, column), None)
            self.dirty = True

    def set_bypass(self, row: int, column: int, bypassed: bool, scene=None):
        self._log("set_bypass", row, column, bypassed)
        with self._lock:
            if scene is not None:
                self.switch_scene(scene)
            cb = self.live.bypass[row].colBypass[column]
            targets = [self.scene] if cb.sceneMode else range(SCENES)
            for s in targets:
                cb.sceneBypass[s].bypass = bool(bypassed)
            self.dirty = True

    def set_chain_input(self, row: int, in_portid: int):
        self._log("set_chain_input", row, int(in_portid))
        with self._lock:
            self.live.chains[row].in_portid = int(in_portid)
            self.dirty = True

    def set_chain_output(self, row: int, out_portid: int):
        self._log("set_chain_output", row, int(out_portid))
        with self._lock:
            self.live.chains[row].out_portid = int(out_portid)
            self.dirty = True

    def set_lane_output(self, row: int, param, value: float = None, real=None,
                        scene=None, promote: bool = True):
        self._log("set_lane_output", row, param, value)
        lane = self._catalog_obj[data.LANE_OUTPUT]
        spec = lane.parameter(param) if isinstance(param, str) else lane.parameters[param]
        if real is not None:
            value = spec.to_normalized(real)
        if value is None:
            raise TypeError("set_lane_output needs value= (0..1) or real= (own units)")
        with self._lock:
            if scene is not None:
                self.switch_scene(scene)
            prm = self.live.chains[row].output_control[0].params[spec.index]
            targets = [self.scene] if prm.scene_mode else range(SCENES)
            for s in targets:
                prm.param_values[s].float_value = float(value)
            self.dirty = True

    def set_input_gate(self, row: int, param, value: float = None, real=None,
                       scene=None, promote: bool = True):
        gate = self._catalog_obj[data.INPUT_GATE]
        spec = gate.parameter(param) if isinstance(param, str) else gate.parameters[param]
        if real is not None:
            value = spec.to_normalized(real)
        with self._lock:
            prm = self.live.chains[row].input_control[0].params[spec.index]
            for pv in prm.param_values:
                pv.float_value = float(value)
            self.dirty = True

    # -- captures and IRs ----------------------------------------------------

    def captures(self, timeout: float = 30.0) -> list:
        return _RealQC.captures(self, timeout)

    def set_capture(self, row: int, column: int, capture, model: int = 14000,
                    params: dict = None):
        return _RealQC.set_capture(self, row, column, capture, model, params)

    def list_irs(self, folder: str = None, timeout: float = 20.0) -> list:
        wanted = (folder or self.IR_LIBRARY).rstrip("/")
        self._wait(1.0)
        entries = [(f, k, n) for f, k, n in self._irs if wanted == self.IR_LIBRARY or f == wanted]
        return [ProductData(index=i, key=k, name=n) for i, (_, k, n) in enumerate(entries)]

    def set_ir(self, row: int, column: int, ir, slot: int = 0, model: int = 29001):
        result = _RealQC.set_ir(self, row, column, ir, slot, model)
        if not self.config.ir_is_inert:
            with self._lock:
                sound = self.live_audible.get((row, column))
                if sound:
                    sound.pop("inert_since_host_write", None)
                    sound["texts"] = {}
        return result

    # -- favourites ----------------------------------------------------------

    def favorites(self, timeout: float = 20.0, attempts: int = 3):
        self._wait(0.5)
        return [FavoriteItem(name=n, folder_key=k, folder_name=fn, is_factory=f)
                for n, k, fn, f in self.favorites_list]

    def recents(self, timeout: float = 10.0):
        msg = pa.RecentsFavoritesMessage()
        for n, k, fn, f in self.recents_list:
            msg.items.add(name=n, folder_key=k, folder_name=fn, is_factory=f)
        return msg

    def _favorite(self, entry, remove, verify, timeout):
        name = getattr(entry, "name", None)
        key = (getattr(entry, "folder_key", None) or getattr(entry, "key", None) or "").rstrip("/")
        is_factory = bool(getattr(entry, "is_factory", False))
        with self._lock:
            slots = self.folders.get(key, {})
            matches = any(s.name == name for s in slots.values()) and \
                is_factory == (key == data.FACTORY_KEY)
            if matches:
                record = (name, key, self.folder_names[key], is_factory)
                if remove:
                    self.favorites_list = [f for f in self.favorites_list if f != record]
                elif record not in self.favorites_list:
                    self.favorites_list.append(record)
        if verify and not matches:
            self._wait(timeout)
            raise TimeoutError(
                f"the device did not acknowledge {'un-' if remove else ''}favouriting "
                f"{name!r}: a wrong folder_key or is_factory is IGNORED SILENTLY.")
        return None

    def add_favorite(self, entry, verify: bool = True, timeout: float = 10.0):
        self._log("add_favorite", getattr(entry, "name", None))
        return self._favorite(entry, False, verify, timeout)

    def remove_favorite(self, entry, verify: bool = True, timeout: float = 10.0):
        self._log("remove_favorite", getattr(entry, "name", None))
        return self._favorite(entry, True, verify, timeout)

    # -- file operations (eventually consistent) -----------------------------

    def _file_op(self, key: str, apply):
        """Apply ``apply()`` now, but keep listings stale for a few reads."""
        with self._lock:
            if key in self.folders and self.config.file_lag_listings > 0:
                stale = self._stale.get(key)
                if stale is None:
                    self._stale[key] = [self._entries(key, include_empty=True),
                                        self.config.file_lag_listings]
                else:
                    stale[1] += self.config.file_lag_listings
            apply()
        self._wait(0.5)

    def _dedup(self, slots: dict, name: str, position: int) -> str:
        names = {s.name for i, s in slots.items() if i != position}
        if name not in names:
            return name
        n = 1
        while True:
            suffix = f"_{n}"
            candidate = name[: NAME_LIMIT - len(suffix)] + suffix
            if candidate not in names:
                return candidate
            n += 1

    def save_current_preset(self, setlist_path: str, position, name: str,
                            instrument: int = pq.Instrument.NONE, default_scene=None,
                            confirm: bool = False, confirm_timeout: float = 20.0):
        self._log("save_current_preset", setlist_path, position, name)
        key = str(setlist_path).rstrip("/")
        index = _as_position(position)
        if default_scene is not None:
            self.switch_scene(default_scene)
        if key in self.folders and key != data.FACTORY_KEY:
            def apply():
                slots = self.folders[key]
                stored_name = self._dedup(slots, name, index)
                p = copy.deepcopy(self.live)
                p.name = stored_name
                p.default_scene = self.scene
                del p.tags[:]                 # a save carries no tags
                taint = {cell: copy.deepcopy(s) for cell, s in self.live_audible.items()
                         if s.get("passthrough") or s.get("inert_since_host_write")}
                slots[index] = Stored(name=stored_name, preset=p, instrument=int(instrument),
                                      taint=taint)
                self.dirty = False
            self._file_op(key, apply)
        if not confirm:
            return name
        try:
            entries = self.wait_for_listing(
                setlist_path, until=lambda es: any(e.index == index and e.name for e in es),
                timeout=confirm_timeout)
        except TimeoutError:
            return None
        return next((e.name for e in entries if e.index == index), None)

    def delete_preset(self, setlist_path: str, name: str):
        self._log("delete_preset", setlist_path, name)
        key = str(setlist_path).rstrip("/")
        if key not in self.folders or key == data.FACTORY_KEY:
            return None

        def apply():
            slots = self.folders[key]
            for i, s in list(slots.items()):
                if s.name == name:
                    del slots[i]
                    break
        self._file_op(key, apply)
        return None

    def move_preset(self, setlist_path: str, name: str, to_position):
        self._log("move_preset", setlist_path, name, to_position)
        key = str(setlist_path).rstrip("/")
        target = _as_position(to_position)
        if key not in self.folders or key == data.FACTORY_KEY:
            return None

        def apply():
            slots = self.folders[key]
            source = next((i for i, s in slots.items() if s.name == name), None)
            if source is None or target in slots:
                return              # guess: the unit ignores a move onto an occupied slot
            slots[target] = slots.pop(source)
        self._file_op(key, apply)
        return None

    def create_setlist(self, name: str):
        self._log("create_setlist", name)
        key = f"{data.USER_ROOT}/{name}"
        with self._lock:
            if key not in self.folders:
                self.folders[key] = {}
                self.folder_names[key] = name
        self._wait(0.5)
        return key

    def delete_setlist(self, name: str):
        self._log("delete_setlist", name)
        key = f"{data.USER_ROOT}/{name}"
        with self._lock:
            self.folders.pop(key, None)
            self.folder_names.pop(key, None)
        self._wait(0.5)
        return None

    def copy_preset(self, from_setlist: str, position, to_setlist: str, to_position=None,
                    name: str = None, instrument: int = pq.Instrument.NONE):
        return _RealQC.copy_preset(self, from_setlist, position, to_setlist,
                                   to_position, name, instrument)


def _as_position(position) -> int:
    if isinstance(position, str):
        return pq.slot_to_position(position)
    return int(position)


def connect(config: FakeConfig | None = None) -> FakeQuadCortex:
    """The fake's counterpart of :func:`pyquadcortex.connect`."""
    config = config or FakeConfig()
    if config.cortex_control_open:
        raise pq.DeviceNotFoundError(
            "could not open the Quad Cortex over USB. Check that: Cortex Control is "
            "quit (it holds the USB interface exclusively) ... (fake)")
    return FakeQuadCortex(config)
