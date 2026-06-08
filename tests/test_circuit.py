"""Unit tests for ``providers.circuit.CircuitBreaker`` (CONTRACT.md §8.4).

Covers the full state machine: closed -> open after N failures, open skips
calls, the open -> half-open trial after cooldown, success closing the circuit,
and a half-open failure re-opening it.

Deterministic and fully offline: the breaker's only time dependency is
``time.monotonic()``, which is monkeypatched to a controllable fake clock so no
real sleeping or wall-clock reliance is needed.
"""

from __future__ import annotations

import pytest

from makecrazypenny.providers import circuit
from makecrazypenny.providers.circuit import CircuitBreaker


class FakeClock:
    """A controllable monotonic clock for deterministic cooldown tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Patch ``time.monotonic`` (as used inside the circuit module) to a fake."""
    fake = FakeClock()
    monkeypatch.setattr(circuit.time, "monotonic", fake)
    return fake


# ---------------------------------------------------------------------------
# Construction / defaults
# ---------------------------------------------------------------------------


def test_defaults_match_contract() -> None:
    """Default threshold/cooldown match the contract signature (5, 60.0)."""
    cb = CircuitBreaker()
    assert cb.fail_threshold == 5
    assert cb.cooldown_s == 60.0
    assert cb.state == "closed"


def test_starts_closed_and_allows() -> None:
    """A fresh breaker is closed and permits calls."""
    cb = CircuitBreaker(fail_threshold=3, cooldown_s=10.0)
    assert cb.state == "closed"
    assert cb.allow() is True


# ---------------------------------------------------------------------------
# Closed -> open after N failures
# ---------------------------------------------------------------------------


def test_opens_exactly_at_threshold(clock: FakeClock) -> None:
    """The circuit stays closed below the threshold and opens at it."""
    cb = CircuitBreaker(fail_threshold=3, cooldown_s=10.0)

    cb.record_failure()
    assert cb.state == "closed"
    assert cb.allow() is True

    cb.record_failure()
    assert cb.state == "closed"
    assert cb.allow() is True

    cb.record_failure()  # third failure hits the threshold
    assert cb.state == "open"


def test_single_failure_threshold_one_opens_immediately() -> None:
    """A threshold of 1 opens on the first failure."""
    cb = CircuitBreaker(fail_threshold=1, cooldown_s=5.0)
    cb.record_failure()
    assert cb.state == "open"


def test_intermittent_success_resets_failure_count(clock: FakeClock) -> None:
    """A success between failures resets the count, so the circuit stays closed."""
    cb = CircuitBreaker(fail_threshold=3, cooldown_s=10.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()  # resets accumulated failures
    assert cb.state == "closed"

    cb.record_failure()
    cb.record_failure()
    # Only two failures since the reset -> still below threshold.
    assert cb.state == "closed"
    assert cb.allow() is True


# ---------------------------------------------------------------------------
# Open blocks while cooling down
# ---------------------------------------------------------------------------


def test_open_blocks_during_cooldown(clock: FakeClock) -> None:
    """While open and still cooling down, ``allow()`` returns False."""
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"

    # No time has passed; calls are blocked.
    assert cb.allow() is False
    # Partway through the cooldown -> still blocked, still open.
    clock.advance(29.0)
    assert cb.allow() is False
    assert cb.state == "open"


# ---------------------------------------------------------------------------
# Open -> half-open trial after cooldown
# ---------------------------------------------------------------------------


def test_transitions_to_half_open_after_cooldown(clock: FakeClock) -> None:
    """Once cooldown elapses, ``allow()`` permits one trial and goes half-open."""
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"

    clock.advance(30.0)  # cooldown fully elapsed (>= boundary)
    assert cb.allow() is True
    assert cb.state == "half-open"


def test_half_open_allows_only_one_trial(clock: FakeClock) -> None:
    """After the half-open trial is granted, further calls are held off."""
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0)
    cb.record_failure()
    cb.record_failure()
    clock.advance(30.0)

    assert cb.allow() is True  # trial granted -> half-open
    assert cb.state == "half-open"
    # The in-flight trial has not resolved; subsequent calls are blocked.
    assert cb.allow() is False
    assert cb.allow() is False
    assert cb.state == "half-open"


# ---------------------------------------------------------------------------
# Half-open -> closed on success
# ---------------------------------------------------------------------------


def test_half_open_success_closes_circuit(clock: FakeClock) -> None:
    """A successful half-open trial closes the circuit and allows calls again."""
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0)
    cb.record_failure()
    cb.record_failure()
    clock.advance(30.0)
    assert cb.allow() is True  # half-open trial

    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow() is True


def test_recovery_allows_full_failure_budget_again(clock: FakeClock) -> None:
    """After recovery, the breaker tolerates a fresh full run of failures."""
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0)
    cb.record_failure()
    cb.record_failure()
    clock.advance(30.0)
    cb.allow()
    cb.record_success()  # recovered -> closed, counter reset

    # A single failure post-recovery is below threshold again.
    cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"


# ---------------------------------------------------------------------------
# Half-open -> open on failure
# ---------------------------------------------------------------------------


def test_half_open_failure_reopens(clock: FakeClock) -> None:
    """A failed half-open trial immediately re-opens the circuit."""
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0)
    cb.record_failure()
    cb.record_failure()
    clock.advance(30.0)
    assert cb.allow() is True  # half-open trial
    assert cb.state == "half-open"

    cb.record_failure()  # trial failed
    assert cb.state == "open"
    # And it blocks again until a fresh cooldown elapses.
    assert cb.allow() is False


def test_reopen_starts_new_cooldown(clock: FakeClock) -> None:
    """Re-opening from a failed trial resets the cooldown clock."""
    cb = CircuitBreaker(fail_threshold=1, cooldown_s=30.0)
    cb.record_failure()  # open
    clock.advance(30.0)
    assert cb.allow() is True  # half-open
    cb.record_failure()  # re-open at the current (advanced) time
    assert cb.state == "open"

    # Less than a full cooldown since the re-open -> still blocked.
    clock.advance(29.0)
    assert cb.allow() is False
    # A further second crosses the new cooldown boundary -> half-open trial.
    clock.advance(1.0)
    assert cb.allow() is True
    assert cb.state == "half-open"


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


def test_full_lifecycle(clock: FakeClock) -> None:
    """closed -> open -> half-open -> closed -> open across a single breaker."""
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=10.0)

    # closed
    assert cb.state == "closed"
    assert cb.allow() is True

    # -> open
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    assert cb.allow() is False

    # -> half-open after cooldown
    clock.advance(10.0)
    assert cb.allow() is True
    assert cb.state == "half-open"

    # -> closed on success
    cb.record_success()
    assert cb.state == "closed"

    # -> open again on a fresh threshold breach
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
