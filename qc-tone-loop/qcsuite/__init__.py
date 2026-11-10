"""qcsuite: everyday Quad Cortex tools over USB, built on qctone.

The rule every module follows: **people judge the sound, the machine does the
handling** - rendering under identical conditions, matching loudness, hiding
names, recording, sorting, and making every change undoable. Nothing here
decides what sounds good.

Every feature runs against :class:`qcsuite.device.fake.FakeQuadCortex` without
hardware. Behaviour on the real unit is unverified until the hardware test plan
(``docs/hardware-test-plan.md``) has been run.
"""

__version__ = "0.1.0"
