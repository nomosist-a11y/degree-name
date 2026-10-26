"""First-run checks: is everything the loop needs actually reachable?

Each check yields a status of ``ok``, ``warn`` or ``fail`` and, when not ok,
the fix. Checks after a failure that they depend on are skipped rather than
reported as more failures.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

VERIFIED_LIBRARY = "0.40.0"
VERIFIED_COROS = "4.0.1"
REACHES_OUT_1_2 = {"XLR_1_2", "XLR_1", "XLR_2", "MULTIPLE"}


class Check(NamedTuple):
    name: str
    status: str
    detail: str
    fix: str = ""


def test_burst(sr: int, seconds: float = 1.0, level_dbfs: float = -20.0):
    """A plucked low-E-ish tone: enough harmonics to excite any amp."""
    t = np.arange(int(seconds * sr)) / sr
    note = sum(np.sin(2 * np.pi * 82.4 * k * t) / k for k in range(1, 16))
    note *= np.exp(-t * 2.5)
    return (note / np.max(np.abs(note)) * 10 ** (level_dbfs / 20)).astype(np.float32)


def routing_checks(grid: dict) -> list[Check]:
    """Whether some row is fed from USB 5 and reaches Out 1/2."""
    usb_rows = [r for r in grid["rows"] if r["in_port"] in ("USB_5", "USB_5_6")]
    if not usb_rows:
        return [Check(
            "grid routing", "warn",
            "no row takes its input from USB 5, so a reamp would be silent",
            'start the first patch with {"op": "set_input", "row": 0, "port": "USB_5"}')]
    # A row feeding the next one (NEXT_ROW_*) reaches the jacks through a later row.
    reaching = [r for r in grid["rows"] if r["out_port"] in REACHES_OUT_1_2]
    if not reaching:
        return [Check(
            "grid routing", "warn",
            f"row {usb_rows[0]['row']} takes USB 5, but no row outputs to Out 1/2",
            '{"op": "set_output", "row": <last row of the chain>, "port": "XLR_1_2"}')]
    return [Check("grid routing", "ok",
                  f"row {usb_rows[0]['row']} takes USB 5; row {reaching[0]['row']} "
                  f"outputs to {reaching[0]['out_port']}")]


def _library() -> list[Check]:
    try:
        import pyquadcortex
    except ImportError:
        return [Check("pyquadcortex", "fail", "not installed",
                      "pip install -e '.[device]'")]
    if pyquadcortex.__version__ != VERIFIED_LIBRARY:
        return [Check("pyquadcortex", "warn",
                      f"{pyquadcortex.__version__} installed; qctone was written against "
                      f"{VERIFIED_LIBRARY}", f"pip install pyquadcortex=={VERIFIED_LIBRARY}")]
    return [Check("pyquadcortex", "ok", pyquadcortex.__version__)]


def _unit() -> tuple[list[Check], dict | None]:
    from qctone import device
    try:
        with device.connect() as qc:
            info = qc.version()
            grid = device.snapshot(qc)
    except SystemExit as exc:
        fix = ("brew install hidapi, then export DYLD_LIBRARY_PATH=/opt/homebrew/lib"
               if "hid" in str(exc) and "install" in str(exc)
               else "quit Cortex Control, check the USB cable, wait for the unit to boot")
        return [Check("USB control", "fail", str(exc), fix)], None
    firmware = str(getattr(info, "app_fw_version", "?"))
    checks = [Check("USB control", "ok",
                    f"connected; firmware {firmware}, {len(grid['blocks'])} blocks on the grid")]
    if VERIFIED_COROS in firmware:
        checks.append(Check("firmware", "ok", firmware))
    else:
        checks.append(Check(
            "firmware", "warn",
            f"pyquadcortex is verified on CorOS {VERIFIED_COROS} only; this unit reports "
            f"{firmware}. If edits do not show on the unit, this is the first suspect."))
    return checks + routing_checks(grid), grid


def _audio(reamp_test: bool, routed: bool) -> list[Check]:
    from qctone import audio_io
    try:
        index = audio_io.find_device()
    except SystemExit as exc:
        fix = ("pip install -e '.[device]'" if "sounddevice" in str(exc)
               else "connect the unit by USB; set QCTONE_AUDIO_DEVICE if its name differs")
        return [Check("USB audio", "fail", str(exc), fix)]
    sd = audio_io._sd()
    dev = sd.query_devices(index)
    checks = [Check("USB audio", "ok",
                    f"{dev['name']}: {dev['max_input_channels']} in / "
                    f"{dev['max_output_channels']} out, default {dev['default_samplerate']:.0f} Hz")]
    if not reamp_test:
        return checks
    if not routed:
        return checks + [Check("reamp test", "warn", "skipped: fix the grid routing first")]
    sr = audio_io.SAMPLE_RATE
    burst = test_burst(sr)
    rec = audio_io.playrec(burst, sr)
    wet = rec.mean(axis=1)
    start = audio_io.onset(wet)
    if start is None or np.max(np.abs(wet)) < 1e-4:
        return checks + [Check(
            "reamp test", "fail",
            f"silence on USB record {audio_io.WET_IN_CHANNELS} after playing on "
            f"USB {audio_io.REAMP_OUT_CHANNEL}",
            "check the unit's USB settings and set QCTONE_REAMP_OUT / QCTONE_WET_IN")]
    latency_ms = (start - int(audio_io.PREROLL_S * sr)) / sr * 1000
    peak = 20 * np.log10(float(np.max(np.abs(wet))))
    level = "ok" if peak < -0.1 else "warn"
    return checks + [Check(
        "reamp test", level,
        f"signal back at {peak:.1f} dBFS peak, ~{latency_ms:.0f} ms round trip",
        "" if level == "ok" else "the render clips: lower the amp or lane output level")]


def run(reamp_test: bool = False) -> list[Check]:
    checks = _library()
    if checks[0].status == "fail":
        return checks
    unit, grid = _unit()
    checks += unit
    routed = grid is not None and all(
        c.status == "ok" for c in routing_checks(grid))
    return checks + _audio(reamp_test, routed)
