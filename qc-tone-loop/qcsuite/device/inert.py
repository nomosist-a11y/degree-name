"""Blocks that read back fine from the host but do not change the sound.

Measured on the player's unit (CorOS 4.0.1, firmware d14e). Writes from the
computer to these succeed and read back correctly, yet the audio does not
follow until the same thing is loaded by hand on the unit's screen:

* a Neural Capture block's ``file_name`` (``set_capture``) and the capture
  block's own knobs (GAIN and the rest);
* string parameters such as a cab's ``ir selector`` (the mic choice);
* any block from a plugin the unit does not own - Plini, Gojira, Nolly,
  Petrucci, John Mayer, Rabea, Misha, Tim Henson, Fortin, Soldano SLO-100 X.
  Their audio passes straight through (the DI comes out). Cory Wong's The Amp
  Snob and Dumbbell ODS did sound.

The plugin list is matched by name against the model's name, ``based_on``,
``plugin_id`` and ``sku``, because how the real catalog spells a plugin is not
known here. A player who buys a plugin, or finds another that does not sound,
edits ``~/.qctone/inert_plugins.json`` (a JSON list of names).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from qcsuite import paths

DEFAULT_UNOWNED = ["Plini", "Gojira", "Nolly", "Petrucci", "John Mayer", "Rabea",
                   "Misha", "Tim Henson", "Fortin", "Soldano SLO-100 X"]
#: Model-name spellings seen or likely for those plugins (synthetic in the mock).
_ALIASES = {"John Mayer": ["Mayer"], "Tim Henson": ["Henson"],
            "Soldano SLO-100 X": ["SLO-100", "Soldano"]}
CAPTURE_MODELS = (14000, 14001)


def _config_path() -> Path:
    return Path(os.environ.get("QCSUITE_INERT_PLUGINS",
                               paths.home() / "inert_plugins.json"))


def unowned_plugins() -> list[str]:
    path = _config_path()
    if path.exists():
        names = json.loads(path.read_text())
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"{path} must hold a JSON list of plugin names")
        return names
    return list(DEFAULT_UNOWNED)


def _patterns() -> list[str]:
    out = []
    for name in unowned_plugins():
        out.append(name.lower())
        out.extend(a.lower() for a in _ALIASES.get(name, []))
    return out


def is_plugin(model) -> bool:
    return bool(getattr(model, "plugin_id", None) or getattr(model, "sku", None))


def inert_plugin(model) -> str | None:
    """The unowned plugin a model belongs to, or None."""
    if model is None or not is_plugin(model):
        return None
    text = " ".join(str(getattr(model, a, "") or "") for a in
                    ("name", "based_on", "plugin_id", "sku")).lower()
    for pattern in _patterns():
        if pattern in text:
            return pattern
    return None


def block_issues(block: dict) -> list[str]:
    """Why a block from a normalized preset may not sound as it reads.

    ``block`` is one entry of :func:`qcsuite.presets.normalize`'s ``blocks``.
    Only issues that hold for presets EDITED FROM THE COMPUTER are certain; a
    preset built on the unit's screen plays its captures and mics normally.
    """
    out = []
    if block.get("inert_plugin"):
        out.append(f"未所有のプラグイン({block['inert_plugin']}): 音は素通し")
    if block.get("model_id") in CAPTURE_MODELS:
        out.append("キャプチャー: パソコンから変更した場合は本体で読み込み直すまで鳴らない")
    return out
