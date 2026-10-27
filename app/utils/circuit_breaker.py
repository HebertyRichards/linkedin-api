import logging
import time
from enum import StrEnum

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


class CircuitBreaker:
    """
    States:
      - closed: calls go through; consecutive failures are counted.
      - open: after `failure_threshold` failures, calls are short-circuited for the cooldown
        instead of hammering a dependency that is down.
      - half_open: once the cooldown passes a probe is let through. Success closes
        the circuit; failure reopens it.

    With `max_recovery_timeout`, every reopen from half_open doubles the cooldown up to that cap,
    and a success brings it back to `recovery_timeout`.

    The worker runs on a single event loop, so transitions need no lock: there is no
    await between reading and writing the state.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        max_recovery_timeout: float | None = None,
        name: str = "circuit",
    ) -> None:
        self._failure_threshold = failure_threshold
        self._base_timeout = recovery_timeout
        self._max_timeout = max(max_recovery_timeout or recovery_timeout, recovery_timeout)
        self._timeout = recovery_timeout
        self._name = name
        self._state = CircuitState.closed
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow(self) -> bool:
        """Decides whether a call may proceed. Call once per attempt to use the dependency."""
        if self._state is CircuitState.open:
            if (time.monotonic() - self._opened_at) >= self._timeout:
                self._state = CircuitState.half_open
                logger.info("Circuit '%s' -> half_open (probing)", self._name)
                return True
            return False
        return True

    def record_success(self) -> None:
        if self._state is not CircuitState.closed:
            logger.info("Circuit '%s' -> closed (recovered)", self._name)
        self._state = CircuitState.closed
        self._failures = 0
        self._timeout = self._base_timeout

    def record_failure(self) -> None:
        if self._state is CircuitState.half_open:
            self.trip()
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self.trip()

    def trip(self, cooldown: float | None = None) -> None:
        """Opens now. `cooldown` (e.g. from Retry-After) wins when longer, up to the cap."""
        if self._state is CircuitState.half_open:
            self._timeout = min(self._timeout * 2, self._max_timeout)
        if cooldown is not None:
            self._timeout = max(self._timeout, min(cooldown, self._max_timeout))
        self._state = CircuitState.open
        self._opened_at = time.monotonic()
        self._failures = 0
        logger.warning("Circuit '%s' -> open for %.0fs (dependency unavailable)", self._name, self._timeout)
