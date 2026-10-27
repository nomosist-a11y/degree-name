"""One process at a time on the unit: a file lock around every device session.

There is one Quad Cortex and its USB control link is exclusive, so two tools
talking to it at once would interleave writes and steal each other's replies.
Every qcsuite entry point that opens the device takes this lock first; a second
process waits up to ``timeout`` seconds and then fails with a message naming
who holds it.

The lock is re-entrant within a process (a web request and a background job in
the same server share it), and released when the outermost holder exits.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from qcsuite.errors import DeviceBusy

_held: dict[str, list] = {}   # path -> [file handle, depth]
_guard = threading.RLock()


def _owner(path: Path) -> str:
    try:
        info = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return "不明なプロセス"
    return f"pid {info.get('pid', '?')} ({info.get('what', '?')})"


@contextmanager
def device_lock(path: Path, what: str = "qcsuite", timeout: float = 10.0,
                poll: float = 0.2):
    """Hold the device lock at ``path`` for the duration of the block."""
    key = str(path)
    with _guard:
        if key in _held:
            _held[key][1] += 1
            try:
                yield
            finally:
                _held[key][1] -= 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise DeviceBusy(
                        f"Quad Cortex は別のプロセスが使用中です({_owner(path)})。"
                        f"そちらを終了してから再実行してください。ロック: {path}"
                    ) from None
                time.sleep(poll)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "what": what,
                                 "since": time.strftime("%Y-%m-%d %H:%M:%S")}))
        handle.flush()
        _held[key] = [handle, 1]
    try:
        yield
    finally:
        with _guard:
            entry = _held[key]
            entry[1] -= 1
            if entry[1] == 0:
                del _held[key]
                fcntl.flock(entry[0].fileno(), fcntl.LOCK_UN)
                entry[0].close()


def is_locked(path: Path) -> bool:
    """Whether some process (this one included) holds the lock right now."""
    if str(path) in _held:
        return True
    if not path.exists():
        return False
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False
