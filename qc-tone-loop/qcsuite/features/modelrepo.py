"""The unit's raw ModelRepo.xml: the display attributes pyquadcortex drops.

The unit sends its model list as gzip(tar(ModelRepo.xml)). pyquadcortex's
``catalog.parse_model_repo`` keeps what it needs to address parameters (name,
range, type, steps, wire index) and throws the rest away. Drawing a parameter
screen the way the unit's own editor does needs more of it:

* Model ``tm`` ("Based on ..."), ``hidden``, ``internal``, ``plugin_id``,
  ``sku`` and ``clones`` (a cab is a copy of "Default Cabsim" (12000, hidden):
  it carries only its two ``ir selector`` entries, each with ``replaces="N"``
  naming the source's wire index it overrides; everything else comes from the
  source);
* Parameter ``skew`` (a JUCE skew factor, "LIN_SKEW" = 1), ``stepNames``,
  ``displayPos`` (the order on screen, which is NOT the wire index),
  ``hidden``, ``min_string`` / ``max_string`` ("OFF"), ``showAsInteger``,
  ``toggleOn`` / ``toggleOff``.

Fetching the XML uses ``qc._fetch_model_repo()``, which is NOT public
pyquadcortex API. When it is missing or fails, :func:`load` returns ``None``
and the editor falls back to the catalog's own order with no extras. A
fetched XML is cached under ``~/.qctone/catalog/`` per firmware version, so it
is asked for once per firmware, never periodically.

The real XML is Neural DSP's data: it is never put in the repository. The mock
(``qcsuite/device/fake_model_repo.xml``) and the tests use small hand-written
files of the same shape.
"""

from __future__ import annotations

import gzip
import io
import logging
import re
import tarfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path

from qcsuite import paths

log = logging.getLogger("qcsuite.modelrepo")

#: Types drawn as a knob, as a choice, or not at all.
KNOB_TYPES = {"float", "int", "fader", "floatWithLed"}
SELECT_TYPES = {"switch", "toggleButton", "rotarySwitch", "comboBox"}
HIDDEN_TYPES = {"string", "empty", "meter", "grMeter", "text"}
#: Lower-case internal parameters, never shown as knobs.
INTERNAL_NAMES = {"bypass", "phi", "ir selector", "file_name"}

#: Category name -> colour key of the editor (the unit's own block colours).
CATEGORY_COLOR = {
    "Guitar Amplifier": "amp", "Bass Amplifier": "amp",
    "Guitar Overdrive": "od", "Bass Overdrive": "od",
    "Cabsim Guitar (M)": "cab", "Cabsim Guitar (ST)": "cab",
    "Cabsim Bass (M)": "cab", "Cabsim Bass (ST)": "cab", "IRLoaders": "cab",
    "Delay": "dly", "Reverb": "rev",
    "Compressor": "cmp", "Equalizer": "cmp", "Filter": "cmp", "Wah": "cmp",
    "Pitch": "pit", "Morph": "pit",
    "Modulation": "mod", "Synth": "mod",
    "Neural Capture": "cap", "Neural Capture V2": "cap", "Neural Capture Internal": "cap",
    "Loopers": "util", "Utility": "util", "FX Loop": "util",
}
CATEGORY_LABEL = {
    "Cabsim Guitar (M)": "Cab", "Cabsim Guitar (ST)": "Cab", "Cabsim Bass (M)": "Bass Cab",
    "Cabsim Bass (ST)": "Bass Cab", "IRLoaders": "IR Loader", "Loopers": "Looper",
    "Equalizer": "EQ", "Neural Capture V2": "Neural Capture",
}


def color_key(category: str | None) -> str:
    return CATEGORY_COLOR.get(category or "", "util")


def category_label(category: str | None) -> str:
    return CATEGORY_LABEL.get(category or "", category or "")


def _num(value) -> float | None:
    """A number, or None for missing / symbolic values ("MAX_CABSIM_DB")."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _skew(value) -> float:
    if value is None or str(value).strip().upper() == "LIN_SKEW":
        return 1.0
    number = _num(value)
    return number if number and number > 0 else 1.0


def _true(value) -> bool:
    return str(value).strip().lower() == "true"


@dataclass(frozen=True)
class RepoParam:
    """One ``<Parameter>``, at its wire index (its position among the model's parameters)."""

    index: int
    name: str
    type: str = ""
    minimum: float | None = 0.0
    maximum: float | None = 1.0
    default: float | None = None
    default_text: str = ""
    units: str = ""
    skew: float = 1.0
    steps: int | None = None
    step_names: tuple[str, ...] = ()
    display_pos: float | None = None
    hidden: bool = False
    min_string: str | None = None
    max_string: str | None = None
    show_as_integer: bool = False
    toggle_on: str | None = None
    toggle_off: str | None = None
    replaces: int | None = None

    @property
    def kind(self) -> str | None:
        """"knob", "select", or None (not drawn)."""
        if self.type in SELECT_TYPES:
            return "select"
        if self.type in HIDDEN_TYPES:
            return None
        if self.type in KNOB_TYPES or self.type == "":
            return "knob"
        return "knob"          # unknown numeric types (the mock's "slider") draw as knobs

    @property
    def internal(self) -> bool:
        return self.name.lower() in INTERNAL_NAMES or self.name.lower().startswith("ir selector")

    @property
    def range_known(self) -> bool:
        return self.minimum is not None and self.maximum is not None

    def labels(self) -> list[str]:
        """Display labels of a choice: stepNames, else toggleOff/On, else OFF/ON."""
        if self.step_names:
            return list(self.step_names)
        if self.toggle_off or self.toggle_on:
            return [self.toggle_off or "OFF", self.toggle_on or "ON"]
        return ["OFF", "ON"]


@dataclass(frozen=True)
class RepoModel:
    id: int
    name: str
    category: str
    category_id: int | None = None
    based_on: str = ""
    hidden: bool = False
    internal: bool = False
    category_hidden: bool = False
    plugin_id: str | None = None
    sku: str | None = None
    clones: int | None = None
    params: tuple[RepoParam, ...] = ()


@dataclass
class Repo:
    models: dict[int, RepoModel] = field(default_factory=dict)
    source: str = "none"          # "device", "cache", "file" or "none"
    firmware: str = ""

    def get(self, model_id) -> RepoModel | None:
        return self.models.get(int(model_id))

    def __len__(self):
        return len(self.models)

    def wire_params(self, model_id) -> list[RepoParam]:
        """A model's parameters in wire order, with a clone's source filled in.

        A clone starts from its source's list; each own parameter carrying
        ``replaces="N"`` takes the place of the source's index N, and own
        parameters without it are appended after the source's.
        """
        model = self.get(model_id)
        if model is None:
            return []
        source = self.get(model.clones) if model.clones is not None else None
        if source is None:
            return list(model.params)
        merged = list(source.params)
        extra = []
        for p in model.params:
            if p.replaces is not None and 0 <= p.replaces < len(merged):
                merged[p.replaces] = replace(p, index=p.replaces)
            else:
                extra.append(p)
        for p in extra:
            merged.append(replace(p, index=len(merged)))
        return merged

    def display_params(self, model_id) -> list[RepoParam]:
        """What the screen shows, in screen order.

        Drops hidden parameters, internal lower-case ones (bypass, phi,
        ir selector, file_name) and types not drawn (string, empty, meters);
        a name seen before (a cab's second set, for mic B) is shown once.
        Sorted by ``displayPos``; parameters without one keep wire order after.
        """
        return display_order(self.wire_params(model_id))

    def mic_params(self, model_id) -> list[RepoParam]:
        """The ``ir selector`` entries of a cab (mic A, mic B), in wire order."""
        return [p for p in self.wire_params(model_id)
                if p.name.lower().startswith("ir selector")][:2]


def display_order(params) -> list[RepoParam]:
    """Visible parameters (see :meth:`Repo.display_params`) in screen order."""
    out, seen = [], set()
    for p in params:
        if p.hidden or p.internal or p.kind is None or p.name in seen:
            continue
        seen.add(p.name)
        out.append(p)
    out.sort(key=lambda p: (p.display_pos if p.display_pos is not None else 1000 + p.index,
                            p.index))
    return out


def from_catalog(model) -> list[RepoParam]:
    """The fallback without a ModelRepo: the catalog's parameters, no extras.

    ``RESERVED ...`` placeholders (the fake IR loader's padding) are hidden.
    """
    out = []
    for spec in getattr(model, "parameters", ()):
        out.append(RepoParam(
            index=spec.index, name=spec.name, type=spec.type or "", minimum=spec.minimum,
            maximum=spec.maximum, default=spec.default, units=spec.units or "",
            steps=spec.steps, hidden=spec.name.upper().startswith("RESERVED")))
    return out


# -- parsing ------------------------------------------------------------------------

def extract_xml(payload: bytes) -> bytes:
    """The XML out of gzip(tar(xml)), a bare tar, gzip(xml) or plain XML."""
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    if payload.lstrip()[:1] == b"<":
        return payload
    try:
        with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
            member = next((m for m in tf.getmembers()
                           if m.name.endswith(".xml") and m.isfile()), None)
            if member is None:
                raise ValueError("ModelRepo payload contains no .xml file")
            data = tf.extractfile(member)
            if data is None:
                raise ValueError("ModelRepo .xml member is not readable")
            return data.read()
    except tarfile.TarError as exc:
        raise ValueError(f"ModelRepo payload is neither XML nor a tar archive: {exc}") from None


def _param(index: int, el) -> RepoParam:
    a = el.attrib
    names = a.get("stepNames")
    default = a.get("defaultValue")
    return RepoParam(
        index=index, name=a.get("name", ""), type=a.get("type", "") or "",
        minimum=_num(a.get("min", "0")), maximum=_num(a.get("max", "1")),
        default=_num(default), default_text=default or "",
        units=(a.get("units") or "").strip(), skew=_skew(a.get("skew")),
        steps=_int(a.get("steps")),
        step_names=tuple(s.strip() for s in names.split(",")) if names else (),
        display_pos=_num(a.get("displayPos")), hidden=_true(a.get("hidden")),
        min_string=a.get("min_string") or None, max_string=a.get("max_string") or None,
        show_as_integer=_true(a.get("showAsInteger")) or a.get("type") == "int",
        toggle_on=a.get("toggleOn") or None, toggle_off=a.get("toggleOff") or None,
        replaces=_int(a.get("replaces")),
    )


def parse(payload: bytes, source: str = "file", firmware: str = "") -> Repo:
    """Parse ModelRepo XML (or the device's gzip/tar payload) into a :class:`Repo`."""
    root = ET.fromstring(extract_xml(payload))
    repo = Repo(source=source, firmware=firmware)
    for category in root.iter("Category"):
        cat_name = category.get("name", "")
        cat_hidden = _true(category.get("hidden"))
        for el in category.findall("Model"):
            model_id = _int(el.get("id"))
            if model_id is None:
                continue
            params = tuple(_param(i, p) for i, p in enumerate(el.findall("Parameter")))
            repo.models[model_id] = RepoModel(
                id=model_id, name=el.get("name", ""), category=cat_name,
                category_id=_int(category.get("id")), based_on=el.get("tm", "") or "",
                hidden=_true(el.get("hidden")), internal=_true(el.get("internal")),
                category_hidden=cat_hidden, plugin_id=el.get("plugin_id") or None,
                sku=el.get("sku") or None, clones=_int(el.get("clones")), params=params)
    return repo


# -- fetching and caching -----------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_memory: dict[tuple, Repo | None] = {}
_memory_lock = threading.Lock()


def cache_dir(mock: bool) -> Path:
    return paths.sub("catalog", mock)


def cache_path(mock: bool, firmware: str) -> Path:
    name = _SAFE.sub("_", firmware or "unknown").strip("._") or "unknown"
    return cache_dir(mock) / f"ModelRepo-{name}.xml"


def load(device, firmware: str | None = None) -> Repo | None:
    """The unit's Repo: memory, then the disk cache, then one fetch. None on failure.

    ``firmware`` may be passed when the caller already knows it, to save the
    version query.
    """
    mock = bool(getattr(device, "mock", False))
    if firmware is None:
        try:
            firmware = device.version()["firmware"]
        except Exception as exc:                     # noqa: BLE001 - display extras are optional
            log.warning("firmware version unavailable (%s); no ModelRepo extras", exc)
            return None
    key = (mock, firmware)
    with _memory_lock:
        if key in _memory:
            return _memory[key]
        repo = _load_uncached(device, mock, firmware)
        _memory[key] = repo
        return repo


def forget():
    """Drop the in-memory copies (tests; a firmware change is a new key anyway)."""
    with _memory_lock:
        _memory.clear()


def _load_uncached(device, mock: bool, firmware: str) -> Repo | None:
    path = cache_path(mock, firmware)
    if path.exists():
        try:
            return parse(path.read_bytes(), source="cache", firmware=firmware)
        except (ET.ParseError, ValueError, OSError) as exc:
            log.warning("cached ModelRepo %s unreadable (%s); fetching again", path, exc)
    try:
        payload = device.model_repo_payload()
    except Exception as exc:                         # noqa: BLE001 - private API, may vanish
        log.warning("ModelRepo fetch failed (%s); the editor uses the catalog order", exc)
        return None
    if not payload:
        return None
    try:
        xml = extract_xml(payload)
        repo = parse(xml, source="device", firmware=firmware)
    except (ET.ParseError, ValueError, tarfile.TarError, OSError) as exc:
        log.warning("ModelRepo payload unreadable (%s)", exc)
        return None
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(xml)
        tmp.replace(path)
    except OSError as exc:
        log.warning("could not cache ModelRepo at %s (%s)", path, exc)
    return repo
