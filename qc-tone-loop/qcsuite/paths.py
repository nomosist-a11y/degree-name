"""Where qcsuite keeps its state.

Everything lives under one home directory, ``~/.qctone`` by default (the lock
file the spec names, ``~/.qctone/device.lock``, sits there too). Mock runs get a
separate tree under ``~/.qctone/mock`` so a rehearsal against the fake unit can
never mix its backups, renders or job logs with the real unit's.

``QCSUITE_HOME`` moves the whole tree, which is what the tests do.
"""

from __future__ import annotations

import os
from pathlib import Path


def home(mock: bool = False) -> Path:
    base = Path(os.environ.get("QCSUITE_HOME", Path.home() / ".qctone")).expanduser()
    return base / "mock" if mock else base


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_file(mock: bool = False) -> Path:
    return home(mock) / "device.lock"


def sub(name: str, mock: bool = False) -> Path:
    """A named subdirectory of the home (created on demand)."""
    return ensure(home(mock) / name)
