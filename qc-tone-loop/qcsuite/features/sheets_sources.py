"""Where sheets, setlists and health checks get preset contents from.

Three sources, cheapest first:

* **the backup** (``qcsuite backup run``) - normalized JSON on disk, no
  contact with the unit at all;
* **the live grid** (``read_current_preset``) - no side effects;
* **reading stored presets** - which RECALLS each one on the unit: the sound
  changes and unsaved edits there are lost. Callers say so before doing it.

References to presets are written ``フォルダ:スロット`` (``kami:1E``,
``"My Presets:2B"``, ``Factory Library:1A``) everywhere in the CLI and web UI.
"""

from __future__ import annotations

import importlib

import pyquadcortex as pq

from qcsuite import presets
from qcsuite.device import guard

FACTORY_KEY = "/opt/neuraldsp/Factory Library"
_FACTORY_NAMES = {"factory library", "factory", "工場出荷"}


def folder_key(name_or_key: str) -> str:
    """A setlist key from a folder name; the factory library by its usual names."""
    text = name_or_key.strip()
    if text.lower() in _FACTORY_NAMES or text.rstrip("/") == FACTORY_KEY:
        return FACTORY_KEY
    return guard.folder_key(text)


def parse_ref(text: str) -> tuple[str, int]:
    """``"kami:1E"`` -> (folder key, position). The slot may be a number too."""
    if ":" not in text:
        raise ValueError(f"プリセットは「フォルダ:スロット」で指定してください(例 kami:1E): {text!r}")
    folder, slot = text.rsplit(":", 1)
    slot = slot.strip()
    position = int(slot) if slot.isdigit() else pq.slot_to_position(slot)
    return folder_key(folder), position


def ref_text(folder: str | None, position: int | None) -> str:
    if folder is None or position is None:
        return "(現在のグリッド)"
    return f"{guard.folder_name(folder)}:{pq.position_to_slot(position)}"


def backup_presets(mock: bool) -> list[dict]:
    """The latest backup's presets, or [] while no backup (or no backup feature) exists."""
    try:
        backup = importlib.import_module("qcsuite.features.backup")
    except ImportError:
        return []
    load = getattr(backup, "load_latest", None)
    return load(mock) if load is not None else []


def backup_by_key(mock: bool) -> dict:
    return {(n.get("folder"), n.get("position")): n for n in backup_presets(mock)}


def leveler_measurements(mock: bool) -> dict | None:
    """The leveler's latest LUFS per (folder key, position), or None if unavailable."""
    try:
        leveler = importlib.import_module("qcsuite.features.leveler")
    except ImportError:
        return None
    latest = getattr(leveler, "latest_measurements", None)
    return latest(mock) if latest is not None else None


def current(device) -> dict:
    """The live grid as a normalized preset (no side effects on the unit)."""
    return presets.normalize(device.current_preset(), device.catalog)


def read(device, folder: str, position: int, *, mock: bool, prefer_backup: bool = True) -> dict:
    """One preset: from the backup when it has it, else by recalling it."""
    if prefer_backup:
        found = backup_by_key(mock).get((folder, position))
        if found is not None:
            return found
    return presets.normalize(device.read_stored(folder, position), device.catalog,
                             folder=folder, position=position)


def needs_recall(refs: list[tuple[str, int]], mock: bool) -> int:
    """How many of ``refs`` would have to be recalled (not in the backup)."""
    have = backup_by_key(mock)
    return sum(1 for ref in refs if ref not in have)


def folder_refs(device, folder: str) -> list[tuple[str, int]]:
    """Every occupied slot of a folder, from its listing (no recall)."""
    key = folder_key(folder)
    return [(key, p["index"]) for p in device.presets(key)]


def collect(device, *, mock: bool, refs=(), folder: str | None = None,
            current_grid: bool = False, from_backup: bool = False) -> list[dict]:
    """Normalized presets for a selection (see the module docstring for the cost)."""
    if from_backup:
        found = backup_presets(mock)
        if folder:
            key = folder_key(folder)
            found = [n for n in found if n.get("folder") == key]
        if refs:
            wanted = set(refs)
            found = [n for n in found if (n.get("folder"), n.get("position")) in wanted]
        return found
    out = []
    if current_grid:
        out.append(current(device))
    targets = list(refs)
    if folder:
        targets += folder_refs(device, folder)
    for key, position in targets:
        out.append(read(device, key, position, mock=mock))
    return out
