"""Circuit breaker for provider health (see CONTRACT.md §8.4).

States:
  * ``closed``    — normal operation; calls allowed.
  * ``open``      — recent failures exceeded the threshold; calls skipped until
    ``cooldown_s`` elapses.
  * ``half-open`` — after cooldown, a single trial call is allowed; success
    closes the circuit, failure re-opens it.
"""

from __future__ import annotations

import time
from typing import Literal

State = Literal["closed", "open", "half-open"]


class CircuitBreaker:
    """A simple three-state circuit breaker.

    Attributes:
        fail_threshold: Consecutive failures that open the circuit.
        cooldown_s: Seconds the circuit stays open before a half-open trial.
    """

    def __init__(self, fail_threshold: int = 5, cooldown_s: float = 60.0) -> None:
        """Initialize a closed circuit.

        Args:
            fail_threshold: Failures required to open the circuit.
            cooldown_s: Cooldown before transitioning open -> half-open.
        """
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self._failures = 0
        self._state: State = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> State:
        """Current circuit state (``closed`` / ``open`` / ``half-open``)."""
        return self._state

    def allow(self) -> bool:
        """Return whether a call may proceed right now.

        Transitions ``open`` -> ``half-open`` when ``cooldown_s`` has elapsed,
        permitting exactly one trial call.

        Returns:
            ``True`` if the call is permitted (closed or half-open trial),
            ``False`` if the circuit is open and still cooling down.
        """
        if self._state == "closed":
            return True
        if self._state == "half-open":
            # A trial is already in flight; hold further calls until it resolves.
            return False
        # open: check whether cooldown has elapsed.
        if (time.monotonic() - self._opened_at) >= self.cooldown_s:
            self._state = "half-open"
            return True
        return False

    def record_success(self) -> None:
        """Record a successful call: reset failures and close the circuit."""
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0

    def record_failure(self) -> None:
        """Record a failed call.

        A failure during a half-open trial immediately re-opens the circuit.
        Otherwise failures accumulate and open the circuit once they reach
        :attr:`fail_threshold`.
        """
        if self._state == "half-open":
            self._open()
            return
        self._failures += 1
        if self._failures >= self.fail_threshold:
            self._open()

    def _open(self) -> None:
        """Transition to the open state and start the cooldown clock."""
        self._state = "open"
        self._opened_at = time.monotonic()
        self._failures = self.fail_threshold


__all__ = ["CircuitBreaker"]
