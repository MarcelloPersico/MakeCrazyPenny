"""Async token-bucket rate limiter (see CONTRACT.md §8.2).

A :class:`TokenBucket` refills continuously at ``rate_per_min / 60`` tokens per
second up to ``capacity`` and lets callers ``acquire`` tokens, sleeping until
enough are available. ``rate_per_min <= 0`` means unlimited.

By design ``acquire`` *waits* rather than raising; the optional ``max_wait``
parameter lets a caller cap the wait and surface :class:`RateLimited` instead.
"""

from __future__ import annotations

import asyncio
import time

from ..core.errors import RateLimited


class TokenBucket:
    """A continuously-refilling async token bucket.

    Attributes:
        rate_per_min: Refill rate in tokens per minute; ``<= 0`` is unlimited.
        capacity: Maximum tokens that can accumulate (burst size).
    """

    def __init__(self, rate_per_min: int, capacity: int | None = None) -> None:
        """Initialize the bucket.

        Args:
            rate_per_min: Tokens added per minute. ``<= 0`` => unlimited.
            capacity: Maximum token accumulation. Defaults to ``rate_per_min``
                (one minute of burst), with a floor of 1 when limited.
        """
        self.rate_per_min = rate_per_min
        self._unlimited = rate_per_min <= 0
        self._rate_per_s = rate_per_min / 60.0 if not self._unlimited else 0.0
        if capacity is not None:
            self.capacity = float(capacity)
        else:
            self.capacity = float(max(rate_per_min, 1)) if not self._unlimited else float("inf")
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Add tokens accrued since the last update (caller holds the lock)."""
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self._rate_per_s)

    async def acquire(self, cost: int = 1, *, max_wait: float | None = None) -> None:
        """Consume ``cost`` tokens, waiting (async) until they are available.

        Args:
            cost: Number of tokens to consume.
            max_wait: Optional ceiling on total seconds to wait. If the wait
                would exceed this, :class:`RateLimited` is raised instead.

        Raises:
            RateLimited: If ``max_wait`` is set and would be exceeded.
        """
        if self._unlimited or cost <= 0:
            return

        # A single request can never need more than capacity tokens.
        needed = min(float(cost), self.capacity)
        waited = 0.0

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= needed:
                    self._tokens -= needed
                    return
                deficit = needed - self._tokens
                sleep_for = deficit / self._rate_per_s if self._rate_per_s > 0 else float("inf")

            if max_wait is not None and (waited + sleep_for) > max_wait:
                raise RateLimited(
                    f"Token bucket wait ({waited + sleep_for:.2f}s) exceeds "
                    f"max_wait ({max_wait:.2f}s)."
                )

            await asyncio.sleep(sleep_for)
            waited += sleep_for


__all__ = ["TokenBucket"]
