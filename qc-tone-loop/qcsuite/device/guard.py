"""The safety rules for writes, enforced in one place.

The player's existing presets and folders are off limits by default. A write -
save, copy-into, delete, move, overwrite, create or delete a folder - is checked
here before anything is sent, and refused unless its target folder is on the
allowlist:

* the tool's own sandbox folders (``_qcsuite_sandbox`` and any other name that
  starts with ``_qcsuite``) are always allowed;
* ``Live``, ``Artist``, ``Song`` and ``Workshop`` - empty folders the player made
  for this - are allowed only when the player names them explicitly
  (``--allow-folder Live`` on the CLI, a checkbox in the web UI);
* ``My Presets``, ``Favorite``, ``kami`` and ``Cory Wong`` are protected: they
  cannot be put on the allowlist at all from the CLI or the web UI. Unlocking
  them is a decision left to the player (see the final report's open items).
* the Factory Library is read-only, always.

Any other user folder that exists when the tool runs is treated like the
protected ones until the player allowlists it. ``QCSUITE_READONLY=1`` refuses
every write, whatever the allowlist says.

A write the rules let through is still not executed directly: destructive ones
go through :mod:`qcsuite.plan` (plan, confirm, back up, run, read back).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from qcsuite.errors import ForbiddenCall, WriteRefused

USER_ROOT = "/media/p4/Presets"
FACTORY_ROOT = "/opt/neuraldsp"
SANDBOX_NAME = "_qcsuite_sandbox"
SANDBOX_PREFIX = "_qcsuite"
PROTECTED = ("My Presets", "Favorite", "kami", "Cory Wong")
#: User setlists the unit holds, My Presets included. Neural DSP's support
#: page says "up to 12 custom Setlists"; whether My Presets counts among them is
#: not stated, so the smaller reading is used until the hardware test confirms.
SETLIST_LIMIT = 12
OPT_IN = ("Live", "Artist", "Song", "Workshop")

#: Device methods qcsuite never calls: whole-unit I/O, Global EQ, firmware and
#: global settings. :class:`~qcsuite.device.core.Device` exposes none of them,
#: and the fake raises :class:`ForbiddenCall` if anything reaches for one.
FORBIDDEN_METHODS = frozenset({
    "set_input_port", "set_output_port", "set_usb_port", "set_output_mute",
    "set_output_pairing", "set_input_level", "set_output_level", "update_settings",
    "set_global_eq", "set_global_eq_band", "set_global_eq_output",
    "set_global_eq_bypassed", "set_global_bypass", "set_master_volume",
    "set_master_volume_assignment", "set_midi_thru", "set_hold_timing",
    "set_scene_bypass_behavior", "set_mode", "set_mode_cycle", "set_tuner_input",
    "set_tuner_mute", "set_tuner_reference", "show_capture_dialog",
    "write_preset", "restore_audio", "set_gig_view", "pin_model", "unpin_model",
})


_LIBRARY_KEY = re.compile(r"^(\d+_[a-z]|[a-z_]+_root)$")


def folder_key(name_or_key: str) -> str:
    """A user folder's device key from its name; keys pass through.

    Device keys are paths (``/media/p4/Presets/kami``, the Factory Library under
    ``/opt/neuraldsp``) or library ids (``local_nc_root``, ``106_f``, ``2_q``).
    Anything else is taken as the name of a user folder. Trailing slashes are
    dropped, since the device lists the factory folder without one.
    """
    if "/" in name_or_key:
        return name_or_key.rstrip("/")
    if _LIBRARY_KEY.match(name_or_key):
        return name_or_key
    return f"{USER_ROOT}/{name_or_key}"


def folder_name(key: str) -> str:
    return key.rstrip("/").rsplit("/", 1)[-1]


def is_user_folder(key: str) -> bool:
    return key.startswith(USER_ROOT + "/")


def is_sandbox(key: str) -> bool:
    return is_user_folder(key) and folder_name(key).startswith(SANDBOX_PREFIX)


def setlist_limit() -> int:
    raw = os.environ.get("QCSUITE_SETLIST_LIMIT")
    return int(raw) if raw else SETLIST_LIMIT


def readonly_env() -> bool:
    return os.environ.get("QCSUITE_READONLY", "") not in ("", "0", "false", "no")


@dataclass
class WritePolicy:
    """Which folders writes may target. Build it once per session."""

    allowed: set[str] = field(default_factory=set)
    readonly: bool = field(default_factory=readonly_env)

    def __post_init__(self):
        names = set(self.allowed)
        self.allowed = set()
        for name in names:
            self.allow(name)

    def allow(self, name_or_key: str):
        """Put a folder on the allowlist, or refuse with the reason."""
        key = folder_key(name_or_key)
        name = folder_name(key)
        if not is_user_folder(key):
            raise WriteRefused(f"{name} は工場出荷やライブラリのフォルダなので書き込めません。")
        if name in PROTECTED:
            raise WriteRefused(
                f"「{name}」は保護フォルダです。qcsuite からの書き込みは許可できません"
                "(解禁するかどうかは保留事項です)。")
        self.allowed.add(key)

    def describe(self) -> dict:
        return {
            "readonly": self.readonly,
            "sandbox": f"{USER_ROOT}/{SANDBOX_NAME}",
            "allowed": sorted(self.allowed),
            "protected": [f"{USER_ROOT}/{n}" for n in PROTECTED],
            "opt_in": [f"{USER_ROOT}/{n}" for n in OPT_IN],
        }

    def check(self, key: str, action: str):
        """Raise :class:`WriteRefused` unless ``action`` on ``key`` is allowed."""
        key = folder_key(key)
        name = folder_name(key)
        if self.readonly:
            raise WriteRefused(f"QCSUITE_READONLY が設定されているため、{action} を拒否しました。")
        if key.startswith(FACTORY_ROOT) or not is_user_folder(key):
            raise WriteRefused(f"「{name}」は読み取り専用です({action} を拒否)。")
        if is_sandbox(key) or key in self.allowed:
            return
        if name in PROTECTED:
            raise WriteRefused(f"「{name}」は保護フォルダです({action} を拒否)。")
        if name in OPT_IN:
            raise WriteRefused(
                f"「{name}」への {action} は、明示的に許可したときだけ行えます"
                f"(CLI: --allow-folder {name})。")
        raise WriteRefused(
            f"「{name}」は許可リストにないため {action} を拒否しました。"
            f"書き込み先は sandbox({SANDBOX_NAME})が既定です。")

    def check_create_folder(self, name: str, existing: set[str]):
        """Folders may be created only when new, and only sandbox-named or allowed."""
        key = folder_key(name)
        if key in existing:
            raise WriteRefused(f"フォルダ「{name}」はすでにあります。")
        if folder_name(key) in PROTECTED:
            raise WriteRefused(f"「{name}」は保護フォルダの名前です。")
        self.check(key, "フォルダ作成")

    def check_delete_folder(self, name: str):
        """Only the tool's own sandbox folders may ever be deleted."""
        key = folder_key(name)
        if self.readonly:
            raise WriteRefused("QCSUITE_READONLY が設定されているため、フォルダ削除を拒否しました。")
        if not is_sandbox(key):
            raise WriteRefused(
                f"「{folder_name(key)}」は qcsuite が作った sandbox ではないので削除できません。")


def forbid(name: str):
    raise ForbiddenCall(
        f"{name} は本体全体の設定(I/O、Global EQ、ファームウェアなど)に触るため、"
        "qcsuite からは呼びません。")
