"""Cooperative hand-off between a question and a running indexing job.

While a question is being answered, the indexing worker waits at its next
between-files checkpoint (the per-file progress callback) and resumes when the
question is done. Nothing here preempts work already in flight: a file that is
being parsed or embedded finishes first. A stop request always wins, so a
paused job can still be stopped.
"""

from __future__ import annotations

import threading
from typing import Callable


class YieldGate:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._holders = 0

    @property
    def held(self) -> bool:
        with self._cond:
            return self._holders > 0

    def hold(self) -> None:
        """A question started: indexing yields at its next checkpoint."""
        with self._cond:
            self._holders += 1

    def release(self) -> None:
        """A question ended (answered, failed or abandoned)."""
        with self._cond:
            self._holders = max(0, self._holders - 1)
            self._cond.notify_all()

    def release_all(self) -> None:
        with self._cond:
            self._holders = 0
            self._cond.notify_all()

    def poke(self) -> None:
        """Wake waiters so they re-check their stop condition."""
        with self._cond:
            self._cond.notify_all()

    def wait_clear(
        self,
        give_up: Callable[[], bool],
        on_wait: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        *,
        poll: float = 0.25,
    ) -> bool:
        """Block while a question holds the gate, unless ``give_up()`` turns true.

        Returns True when it actually waited. ``poll`` only bounds how stale a
        missed notification can be; the wake-ups are event driven.
        """
        with self._cond:
            if self._holders == 0 or give_up():
                return False
        if on_wait is not None:
            on_wait()
        try:
            with self._cond:
                while self._holders > 0 and not give_up():
                    self._cond.wait(poll)
        finally:
            if on_resume is not None:
                on_resume()
        return True
