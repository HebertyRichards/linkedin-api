"""Liveness for a worker with no port. Railway's healthcheck is HTTP and runs only at deploy time, and an
HTTP endpoint would open a door to the internet; Railway does restart a process that exits with an error.
So a stalled loop ends the process with code 1, and the restart policy (On Failure) brings it back clean."""

import logging
import os
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class Watchdog:
    """Exits the process when `beat()` has not been called for `stall_after` seconds.

    A thread, not an asyncio task: it still fires when the event loop itself is blocked.
    """

    def __init__(
        self,
        stall_after: float,
        *,
        check_every: float = 60.0,
        exit_process: Callable[[int], None] = os._exit,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stall_after = stall_after
        self._check_every = check_every
        # os._exit, not sys.exit: sys.exit in a thread ends only that thread. The seen-set is already saved
        # after every change, so nothing is lost by skipping the shutdown.
        self._exit = exit_process
        self._clock = clock
        self._last_beat = clock()
        self._stopped = threading.Event()

    def beat(self) -> None:
        self._last_beat = self._clock()

    def stalled(self) -> bool:
        return self._clock() - self._last_beat > self._stall_after

    def start(self) -> None:
        threading.Thread(target=self._watch, name="watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stopped.set()

    def _watch(self) -> None:
        while not self._stopped.wait(self._check_every):
            if self.stalled():
                logger.critical(
                    "HEALTH_STALLED no progress for over %ds: exiting so the host restarts the worker",
                    int(self._stall_after),
                )
                self._exit(1)
                return
