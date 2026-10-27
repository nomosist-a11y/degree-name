"""Opening the rig: lock, connect (real or fake), wrap, and pair with a renderer.

    from qcsuite.device.session import open_rig

    with open_rig(mock=True) as rig:
        rig.device.presets("/media/p4/Presets/kami")
        result = rig.render(di)

``mock=True`` gives a :class:`~qcsuite.device.fake.FakeQuadCortex` and the fake
DSP renderer; its storage persists between runs in ``~/.qctone/mock/fake_state.pkl``
so separate CLI commands see the same fake unit (``qcsuite --mock mock-reset``
starts it over). ``mock=False`` opens the real unit through pyquadcortex.

The device lock is held for as long as the rig is open.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from qcsuite import paths
from qcsuite.audio import render as render_mod
from qcsuite.device import fake as fake_mod
from qcsuite.device.core import Device
from qcsuite.device.guard import WritePolicy
from qcsuite.device.lock import device_lock
from qcsuite.errors import DeviceUnavailable


def fake_config_from_env() -> fake_mod.FakeConfig:
    """``QCSUITE_FAKE_SPEED`` (0..1) makes the fake take realistic time."""
    speed = float(os.environ.get("QCSUITE_FAKE_SPEED", "0") or 0)
    return fake_mod.FakeConfig(speed=speed)


def state_path() -> Path:
    return paths.home(mock=True) / "fake_state.pkl"


@dataclass
class Rig:
    device: Device
    renderer: object
    mock: bool
    fake: fake_mod.FakeQuadCortex | None = None
    warned_monitor: bool = field(default=False)

    def render(self, di, sr: int = render_mod.SR) -> render_mod.RenderResult:
        """Reamp ``di`` through the live grid. On hardware, the unit makes sound."""
        if not self.mock and not self.warned_monitor:
            self.device.notice("sound_change", render_mod.MONITOR_WARNING)
            self.warned_monitor = True
        with self.device._lock:
            return render_mod.render(self.renderer, di, sr)


def connect_real():
    try:
        import pyquadcortex as pq
    except ImportError as exc:
        raise DeviceUnavailable("pyquadcortex が入っていません: pip install -e '.[device]'") from exc
    try:
        return pq.connect()
    except pq.DeviceNotFoundError as exc:
        raise DeviceUnavailable(
            "Quad Cortex に USB で接続できません。Cortex Control を終了しているか、"
            "USB ケーブル、本体の起動完了を確認してください。hidapi が無い場合は "
            "`brew install hidapi` と `export DYLD_LIBRARY_PATH=/opt/homebrew/lib`。"
            f"(詳細: {exc})") from exc
    except TimeoutError as exc:
        raise DeviceUnavailable(
            "Quad Cortex は見つかりましたが応答がありません。起動直後なら 30 秒待って再実行、"
            f"続くようなら電源を入れ直してください。(詳細: {exc})") from exc


@contextmanager
def open_rig(mock: bool, *, allow_folders=(), what: str = "qcsuite",
             fake_config: fake_mod.FakeConfig | None = None, persist: bool = True,
             lock_timeout: float = 10.0, fake=None):
    """Open the unit (or the fake) under the device lock; yield a :class:`Rig`."""
    with device_lock(paths.lock_file(mock), what=what, timeout=lock_timeout):
        if mock:
            qc = fake or fake_mod.connect(fake_config or fake_config_from_env())
            if fake is None and persist and state_path().exists():
                qc.load_state(state_path())
            renderer = render_mod.FakeRenderer(qc)
            delay = 0.0 if qc.config.speed == 0 else 1.0 * qc.config.speed
        else:
            qc = connect_real()
            renderer = render_mod.ReampRenderer()
            delay = 1.0
        policy = WritePolicy(allowed=set(allow_folders))
        device = Device(qc, mock=mock, policy=policy, retry_delay=delay)
        rig = Rig(device=device, renderer=renderer, mock=mock,
                  fake=qc if mock else None)
        try:
            yield rig
        finally:
            if mock and persist and fake is None:
                qc.save_state(state_path())
            qc.close()
