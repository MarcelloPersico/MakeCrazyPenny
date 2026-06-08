"""Unit tests for :class:`makecrazypenny.providers.ratelimit.TokenBucket`.

These tests are deterministic and fully offline. They never sleep for real:
a fake monotonic clock is injected, and the bucket's ``asyncio.sleep`` is
monkeypatched to *advance that clock* instead of waiting on the wall clock.
This lets us assert refill/consume math and blocking-then-recovery behavior
without any wall-clock dependence.

API under test (see CONTRACT.md §8.2 and the implementation):

    TokenBucket(rate_per_min: int, capacity: int | None = None)
        - capacity defaults to max(rate_per_min, 1) when limited
        - rate_per_min <= 0 => unlimited (capacity == inf)
    async acquire(cost: int = 1, *, max_wait: float | None = None) -> None
        - waits (async) until `cost` tokens are available, then consumes them
        - cost <= 0 returns immediately
        - raises RateLimited if a finite max_wait would be exceeded
"""

from __future__ import annotations

import math

import pytest

from makecrazypenny.core.errors import RateLimited
from makecrazypenny.providers import ratelimit as ratelimit_mod
from makecrazypenny.providers.ratelimit import TokenBucket


class FakeClock:
    """A controllable monotonic clock.

    ``now()`` is installed in place of ``time.monotonic``; ``sleep()`` is
    installed in place of ``asyncio.sleep`` so that "sleeping" deterministically
    advances time instead of blocking the event loop.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        # Record the requested sleep and advance the fake clock by that amount.
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Install a fake clock + non-blocking sleep into the ratelimit module."""
    fake = FakeClock()
    monkeypatch.setattr(ratelimit_mod.time, "monotonic", fake.now)
    monkeypatch.setattr(ratelimit_mod.asyncio, "sleep", fake.sleep)
    return fake


# --------------------------------------------------------------------------- #
# Construction / capacity / defaults
# --------------------------------------------------------------------------- #


def test_capacity_defaults_to_rate_per_min(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60)
    assert bucket.capacity == 60.0
    # Starts full.
    assert bucket._tokens == 60.0
    assert bucket.rate_per_min == 60


def test_explicit_capacity_overrides_default(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60, capacity=10)
    assert bucket.capacity == 10.0
    assert bucket._tokens == 10.0


def test_limited_capacity_has_floor_of_one(clock: FakeClock) -> None:
    # rate_per_min == 1 -> capacity floored at 1 (not below).
    bucket = TokenBucket(rate_per_min=1)
    assert bucket.capacity == 1.0


def test_unlimited_when_rate_zero(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=0)
    assert bucket._unlimited is True
    assert math.isinf(bucket.capacity)


def test_unlimited_when_rate_negative(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=-5)
    assert bucket._unlimited is True
    assert math.isinf(bucket.capacity)


# --------------------------------------------------------------------------- #
# acquire: immediate / no-wait paths
# --------------------------------------------------------------------------- #


async def test_acquire_within_capacity_does_not_sleep(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60)  # capacity 60, starts full
    await bucket.acquire(1)
    assert bucket._tokens == 59.0
    assert clock.sleeps == []  # never had to wait


async def test_acquire_consumes_requested_cost(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60)
    await bucket.acquire(cost=10)
    assert bucket._tokens == 50.0
    assert clock.sleeps == []


async def test_acquire_zero_cost_is_noop(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60)
    await bucket.acquire(0)
    assert bucket._tokens == 60.0
    assert clock.sleeps == []


async def test_acquire_negative_cost_is_noop(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60)
    await bucket.acquire(-3)
    assert bucket._tokens == 60.0
    assert clock.sleeps == []


async def test_unlimited_bucket_never_sleeps(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=0)
    # Even a huge cost returns immediately and does not sleep.
    await bucket.acquire(1_000_000)
    assert clock.sleeps == []


async def test_draining_full_bucket_without_refill(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60, capacity=3)
    for _ in range(3):
        await bucket.acquire(1)
    # Three immediate acquisitions, no sleeping; bucket now empty.
    assert clock.sleeps == []
    assert bucket._tokens == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# acquire: blocking-then-recovery (the core behavior)
# --------------------------------------------------------------------------- #


async def test_acquire_blocks_when_exhausted_then_recovers(clock: FakeClock) -> None:
    # rate 60/min => 1 token/sec; capacity 1 so it is trivially exhaustible.
    bucket = TokenBucket(rate_per_min=60, capacity=1)

    # First acquire empties the bucket immediately.
    await bucket.acquire(1)
    assert clock.sleeps == []
    assert bucket._tokens == pytest.approx(0.0)

    # Second acquire must wait for one token to refill: deficit 1 / 1 tok/s = 1s.
    await bucket.acquire(1)
    assert clock.sleeps == [pytest.approx(1.0)]
    # After waiting exactly enough, the token is consumed back to ~0.
    assert bucket._tokens == pytest.approx(0.0)


async def test_wait_duration_scales_with_deficit(clock: FakeClock) -> None:
    # rate 60/min => 1 token/sec, capacity 5.
    bucket = TokenBucket(rate_per_min=60, capacity=5)
    await bucket.acquire(5)  # drains to 0, no sleep
    assert clock.sleeps == []

    # Need 3 tokens, have 0, at 1 tok/s => 3s wait.
    await bucket.acquire(3)
    assert clock.sleeps == [pytest.approx(3.0)]
    assert bucket._tokens == pytest.approx(0.0)


async def test_wait_duration_scales_with_rate(clock: FakeClock) -> None:
    # rate 30/min => 0.5 token/sec, capacity 1.
    bucket = TokenBucket(rate_per_min=30, capacity=1)
    await bucket.acquire(1)  # empty
    assert clock.sleeps == []

    # Need 1 token at 0.5 tok/s => 2s.
    await bucket.acquire(1)
    assert clock.sleeps == [pytest.approx(2.0)]


async def test_partial_refill_then_short_wait(clock: FakeClock) -> None:
    # rate 60/min => 1 token/sec, capacity 5.
    bucket = TokenBucket(rate_per_min=60, capacity=5)
    await bucket.acquire(5)  # drain to 0
    assert clock.sleeps == []

    # Let 2 seconds pass externally -> 2 tokens refilled.
    clock.advance(2.0)

    # Need 3 tokens; have ~2 after refill -> deficit 1 -> 1s wait.
    await bucket.acquire(3)
    assert clock.sleeps == [pytest.approx(1.0)]
    assert bucket._tokens == pytest.approx(0.0)


async def test_refill_capped_at_capacity(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=60, capacity=5)
    await bucket.acquire(5)  # drain to 0
    # Let a very long time pass; refill must cap at capacity, not overflow.
    clock.advance(10_000.0)
    await bucket.acquire(5)  # exactly capacity, available immediately
    assert clock.sleeps == []
    assert bucket._tokens == pytest.approx(0.0)


async def test_repeated_acquire_sleeps_each_cycle_at_steady_state(clock: FakeClock) -> None:
    # rate 60/min => 1 token/sec, capacity 1. After draining, each subsequent
    # acquire should wait ~1s (one token's worth) per call.
    bucket = TokenBucket(rate_per_min=60, capacity=1)
    await bucket.acquire(1)  # drain, no sleep
    assert clock.sleeps == []

    for _ in range(3):
        await bucket.acquire(1)

    assert len(clock.sleeps) == 3
    for s in clock.sleeps:
        assert s == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# acquire: cost larger than capacity is clamped to capacity
# --------------------------------------------------------------------------- #


async def test_cost_exceeding_capacity_is_clamped(clock: FakeClock) -> None:
    # capacity 5 starts full; asking for 100 needs only `capacity` tokens
    # (a single request can never need more than capacity), so no wait.
    bucket = TokenBucket(rate_per_min=60, capacity=5)
    await bucket.acquire(100)
    assert clock.sleeps == []
    assert bucket._tokens == pytest.approx(0.0)


async def test_cost_exceeding_capacity_when_empty_waits_capacity_worth(clock: FakeClock) -> None:
    # rate 60/min => 1 tok/s, capacity 5. Drain, then ask for more than
    # capacity: needed clamps to 5, deficit 5 => 5s wait.
    bucket = TokenBucket(rate_per_min=60, capacity=5)
    await bucket.acquire(5)  # drain
    assert clock.sleeps == []
    await bucket.acquire(50)
    assert clock.sleeps == [pytest.approx(5.0)]


# --------------------------------------------------------------------------- #
# acquire: max_wait raises RateLimited (the "raise when exhausted" path)
# --------------------------------------------------------------------------- #


async def test_max_wait_exceeded_raises_rate_limited(clock: FakeClock) -> None:
    # rate 60/min => 1 tok/s, capacity 1. Drain, then required wait is 1s but
    # max_wait is 0.5s => RateLimited, and no sleep should occur.
    bucket = TokenBucket(rate_per_min=60, capacity=1)
    await bucket.acquire(1)  # drain
    with pytest.raises(RateLimited):
        await bucket.acquire(1, max_wait=0.5)
    assert clock.sleeps == []  # raised before sleeping
    # Tokens were not consumed by the failed acquire.
    assert bucket._tokens == pytest.approx(0.0)


async def test_max_wait_not_exceeded_proceeds(clock: FakeClock) -> None:
    # Required wait 1s, max_wait 2s -> should sleep and succeed.
    bucket = TokenBucket(rate_per_min=60, capacity=1)
    await bucket.acquire(1)  # drain
    await bucket.acquire(1, max_wait=2.0)
    assert clock.sleeps == [pytest.approx(1.0)]
    assert bucket._tokens == pytest.approx(0.0)


async def test_max_wait_does_not_raise_when_tokens_available(clock: FakeClock) -> None:
    # Plenty of tokens => no waiting => max_wait is irrelevant, no raise.
    bucket = TokenBucket(rate_per_min=60, capacity=5)
    await bucket.acquire(1, max_wait=0.0)
    assert clock.sleeps == []
    assert bucket._tokens == pytest.approx(4.0)


async def test_unlimited_ignores_max_wait(clock: FakeClock) -> None:
    bucket = TokenBucket(rate_per_min=0)
    # Unlimited returns immediately regardless of max_wait.
    await bucket.acquire(1_000, max_wait=0.0)
    assert clock.sleeps == []
