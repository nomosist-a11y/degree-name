"""Exceptions qcsuite raises on purpose.

Each one carries a message meant for the player, in Japanese, because it ends
up on screen in the CLI and the web UI.
"""


class QcsuiteError(RuntimeError):
    """Base class: an expected failure with a message for the player."""


class WriteRefused(QcsuiteError):
    """A write was aimed at a folder the safety rules protect."""


class DeviceBusy(QcsuiteError):
    """Another qcsuite/qctone process holds the device lock."""


class DeviceUnavailable(QcsuiteError):
    """The unit cannot be reached (USB, Cortex Control, power)."""


class ForbiddenCall(QcsuiteError):
    """Code tried to call a device method the safety rules never allow."""


class PlanError(QcsuiteError):
    """A plan could not be built or executed as asked."""


class VerifyFailed(QcsuiteError):
    """A write was sent but reading back did not show the expected result."""
