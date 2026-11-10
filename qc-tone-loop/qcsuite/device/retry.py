"""Retry the reads the unit is known to drop.

Measured on the player's unit and documented in pyquadcortex: the catalog fetch
(ModelRepoMessage) sometimes times out at 25 s and succeeds when asked again;
listings and the first read after connecting are sometimes not answered. A
``TimeoutError`` from one of those is "ask again", not an answer.

Writes are never retried here: most host writes get no reply at all, so a
retry would duplicate the write. Writes are confirmed by reading back instead.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("qcsuite.device")


def retry(fn, *, what: str, attempts: int = 3, delay: float = 1.0, backoff: float = 2.0,
          exceptions=(TimeoutError,), on_retry=None):
    """Call ``fn()`` until it succeeds or ``attempts`` run out (then re-raise)."""
    wait = delay
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:
            if attempt == attempts:
                raise
            log.warning("%s: %s (attempt %d/%d, retrying)", what, exc, attempt, attempts)
            if on_retry is not None:
                on_retry(what, exc, attempt)
            if wait > 0:
                time.sleep(wait)
            wait *= backoff
    raise AssertionError("unreachable")
