"""Unit tests for ``makecrazypenny.providers.cache.TTLCache``.

Covers the behaviours mandated by CONTRACT.md §8.3 / §11:

* L1 hit/miss and the ``cached`` flag.
* TTL expiry (deterministic via a monkeypatched clock — no sleeping).
* L2 on-disk JSON persistence under a tmp dir, including round-trip,
  corrupt-entry tolerance, expired-entry tolerance, and ``l2_enabled=False``.
* Single-flight: N concurrent identical ``get_or_fetch`` calls invoke the
  factory exactly once.

All tests are deterministic and fully offline.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from makecrazypenny.providers import cache as cache_module
from makecrazypenny.providers.cache import CacheResult, TTLCache, make_key


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic-ish clock patched over ``cache.time.time``.

    The cache reads wall time exclusively through ``time.time`` inside the
    ``cache`` module, so patching that single symbol makes TTL behaviour fully
    deterministic without any real sleeping.
    """

    class Clock:
        def __init__(self) -> None:
            self.now = 1_000.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    c = Clock()
    monkeypatch.setattr(cache_module.time, "time", c)
    return c


def make_factory(value, calls):
    """Build a zero-arg async factory that records its invocations.

    Args:
        value: The value the factory returns.
        calls: A mutable list appended to on every invocation.
    """

    async def factory():
        calls.append(value)
        return value

    return factory


# --------------------------------------------------------------------------- #
# CacheResult / make_key
# --------------------------------------------------------------------------- #


def test_cache_result_shape():
    res = CacheResult(value={"a": 1}, cached=True)
    assert res.value == {"a": 1}
    assert res.cached is True


def test_make_key_is_stable_and_order_independent():
    k1 = make_key("finnhub", "quote", {"symbol": "AAPL", "extra": 1})
    k2 = make_key("finnhub", "quote", {"extra": 1, "symbol": "AAPL"})
    assert k1 == k2  # sorted keys => order independent
    # Different params produce different keys.
    assert make_key("finnhub", "quote", {"symbol": "MSFT"}) != k1


# --------------------------------------------------------------------------- #
# Hit / miss + cached flag
# --------------------------------------------------------------------------- #


async def test_miss_then_hit(tmp_path, clock):
    cache = TTLCache(tmp_path)
    calls: list[str] = []
    factory = make_factory("VALUE", calls)

    # First call: miss -> factory runs, cached=False.
    first = await cache.get_or_fetch("k", ttl=100.0, factory=factory)
    assert isinstance(first, CacheResult)
    assert first.value == "VALUE"
    assert first.cached is False
    assert calls == ["VALUE"]

    # Second call within TTL: L1 hit -> factory NOT re-run, cached=True.
    second = await cache.get_or_fetch("k", ttl=100.0, factory=factory)
    assert second.value == "VALUE"
    assert second.cached is True
    assert calls == ["VALUE"]  # still only one factory invocation


async def test_distinct_keys_are_independent(tmp_path, clock):
    cache = TTLCache(tmp_path)
    calls_a: list[str] = []
    calls_b: list[str] = []

    ra = await cache.get_or_fetch("a", ttl=100.0, factory=make_factory("A", calls_a))
    rb = await cache.get_or_fetch("b", ttl=100.0, factory=make_factory("B", calls_b))

    assert ra.value == "A" and ra.cached is False
    assert rb.value == "B" and rb.cached is False
    assert calls_a == ["A"]
    assert calls_b == ["B"]


async def test_tuple_key_is_coerced(tmp_path, clock):
    """A ``(name, capability, params)`` tuple keys the same entry as its make_key string."""
    cache = TTLCache(tmp_path)
    calls: list[str] = []

    tuple_key = ("finnhub", "quote", {"symbol": "AAPL"})
    first = await cache.get_or_fetch(tuple_key, ttl=100.0, factory=make_factory("Q", calls))
    assert first.cached is False

    # Same logical request, expressed as the equivalent serialized string key.
    str_key = make_key("finnhub", "quote", {"symbol": "AAPL"})
    second = await cache.get_or_fetch(str_key, ttl=100.0, factory=make_factory("Q", calls))
    assert second.cached is True  # served from the tuple-keyed entry
    assert calls == ["Q"]  # factory ran only once


# --------------------------------------------------------------------------- #
# TTL expiry
# --------------------------------------------------------------------------- #


async def test_ttl_expiry_refetches(tmp_path, clock):
    cache = TTLCache(tmp_path)
    calls: list[str] = []
    factory = make_factory("V", calls)

    await cache.get_or_fetch("k", ttl=30.0, factory=factory)
    assert calls == ["V"]

    # Just before expiry: still a hit.
    clock.advance(29.9)
    res = await cache.get_or_fetch("k", ttl=30.0, factory=factory)
    assert res.cached is True
    assert calls == ["V"]

    # Past expiry: L1 (and L2, written with the same expiry) are stale -> refetch.
    clock.advance(1.0)  # total 30.9 > 30
    res = await cache.get_or_fetch("k", ttl=30.0, factory=factory)
    assert res.cached is False
    assert calls == ["V", "V"]  # factory ran a second time


async def test_expiry_boundary_is_strict(tmp_path, clock):
    """Entry is fresh while ``expires_at > now`` and stale once ``expires_at <= now``."""
    cache = TTLCache(tmp_path, l2_enabled=False)
    calls: list[str] = []
    factory = make_factory("V", calls)

    await cache.get_or_fetch("k", ttl=10.0, factory=factory)  # expires_at = now + 10

    # Exactly at expiry: _get_fresh uses ``expires_at > now`` so this is stale.
    clock.advance(10.0)
    res = await cache.get_or_fetch("k", ttl=10.0, factory=factory)
    assert res.cached is False
    assert calls == ["V", "V"]


# --------------------------------------------------------------------------- #
# L2 disk persistence
# --------------------------------------------------------------------------- #


async def test_l2_file_written_with_expected_shape(tmp_path, clock):
    cache = TTLCache(tmp_path)
    await cache.get_or_fetch("k", ttl=50.0, factory=make_factory({"x": 1}, []))

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["value"] == {"x": 1}
    # expires_at = now (1000.0) + ttl (50) under the patched clock.
    assert record["expires_at"] == pytest.approx(1_050.0)


async def test_l2_survives_new_cache_instance(tmp_path, clock):
    """A fresh TTLCache over the same dir serves a value its L1 never held."""
    first = TTLCache(tmp_path)
    calls: list[str] = []
    await first.get_or_fetch("k", ttl=100.0, factory=make_factory("PERSISTED", calls))
    assert calls == ["PERSISTED"]

    # New instance: empty L1, but L2 file on disk is still fresh.
    second = TTLCache(tmp_path)
    res = await second.get_or_fetch("k", ttl=100.0, factory=make_factory("PERSISTED", calls))
    assert res.value == "PERSISTED"
    assert res.cached is True
    assert calls == ["PERSISTED"]  # factory not re-run; served from L2


async def test_l2_expired_entry_ignored(tmp_path, clock):
    first = TTLCache(tmp_path)
    calls: list[str] = []
    await first.get_or_fetch("k", ttl=20.0, factory=make_factory("OLD", calls))

    # Advance past the on-disk entry's expiry, then use a fresh instance so the
    # only possible source is the (now stale) L2 file.
    clock.advance(21.0)
    second = TTLCache(tmp_path)
    res = await second.get_or_fetch("k", ttl=20.0, factory=make_factory("NEW", calls))
    assert res.value == "NEW"
    assert res.cached is False
    assert calls == ["OLD", "NEW"]


async def test_l2_corrupt_entry_tolerated(tmp_path, clock):
    cache = TTLCache(tmp_path)

    # Write a corrupt L2 file at the exact path the cache will look up.
    skey = cache._coerce_key("k")
    path = cache._l2_path(skey)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json", encoding="utf-8")

    calls: list[str] = []
    # Fresh instance to force an L2 read on miss (no L1 entry to shortcut it).
    fresh = TTLCache(tmp_path)
    res = await fresh.get_or_fetch("k", ttl=100.0, factory=make_factory("RECOVERED", calls))
    assert res.value == "RECOVERED"
    assert res.cached is False  # corrupt L2 == miss
    assert calls == ["RECOVERED"]


async def test_l2_missing_required_field_tolerated(tmp_path, clock):
    """An L2 record lacking ``expires_at`` is treated as a miss, not an error."""
    cache = TTLCache(tmp_path)
    skey = cache._coerce_key("k")
    path = cache._l2_path(skey)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"value": "NO_EXPIRY"}), encoding="utf-8")

    calls: list[str] = []
    res = await cache.get_or_fetch("k", ttl=100.0, factory=make_factory("FETCHED", calls))
    assert res.value == "FETCHED"
    assert res.cached is False
    assert calls == ["FETCHED"]


async def test_l2_disabled_writes_nothing(tmp_path, clock):
    cache = TTLCache(tmp_path, l2_enabled=False)
    await cache.get_or_fetch("k", ttl=100.0, factory=make_factory("V", []))
    assert list(tmp_path.glob("*.json")) == []


async def test_l2_disabled_no_cross_instance_persistence(tmp_path, clock):
    first = TTLCache(tmp_path, l2_enabled=False)
    calls: list[str] = []
    await first.get_or_fetch("k", ttl=100.0, factory=make_factory("V", calls))

    # New instance, L2 disabled => empty L1, nothing on disk to recover.
    second = TTLCache(tmp_path, l2_enabled=False)
    res = await second.get_or_fetch("k", ttl=100.0, factory=make_factory("V", calls))
    assert res.cached is False
    assert calls == ["V", "V"]


# --------------------------------------------------------------------------- #
# Single-flight
# --------------------------------------------------------------------------- #


async def test_single_flight_one_factory_call_under_concurrency(tmp_path, clock):
    """N concurrent identical get_or_fetch calls => factory invoked exactly once."""
    cache = TTLCache(tmp_path)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        # Hold the leader open so all followers register before it completes.
        await release.wait()
        return "SHARED"

    async def waiter():
        return await cache.get_or_fetch("k", ttl=100.0, factory=factory)

    tasks = [asyncio.create_task(waiter()) for _ in range(25)]

    # Wait until the leader's factory is actually running, then let it finish.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    release.set()

    results = await asyncio.gather(*tasks)

    assert calls == 1  # the contract's core single-flight guarantee
    assert all(r.value == "SHARED" for r in results)

    # Exactly one task was the leader (cached=False); the rest are followers.
    cached_flags = sorted(r.cached for r in results)
    assert cached_flags.count(False) == 1
    assert cached_flags.count(True) == len(results) - 1


async def test_single_flight_failure_propagates_and_allows_retry(tmp_path, clock):
    """If the leader's factory raises, the error surfaces and a later call can retry."""
    cache = TTLCache(tmp_path)
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await cache.get_or_fetch("k", ttl=100.0, factory=flaky)
    assert attempts == 1

    # The failed in-flight entry was cleaned up, so a retry actually re-runs.
    async def ok():
        nonlocal attempts
        attempts += 1
        return "OK"

    res = await cache.get_or_fetch("k", ttl=100.0, factory=ok)
    assert res.value == "OK"
    assert res.cached is False
    assert attempts == 2


async def test_sequential_calls_after_inflight_clears(tmp_path, clock):
    """After a single-flight batch completes, the value is served from cache."""
    cache = TTLCache(tmp_path)
    calls: list[str] = []
    factory = make_factory("V", calls)

    await asyncio.gather(
        cache.get_or_fetch("k", ttl=100.0, factory=factory),
        cache.get_or_fetch("k", ttl=100.0, factory=factory),
    )
    # A subsequent lone call hits cache, not the factory.
    res = await cache.get_or_fetch("k", ttl=100.0, factory=factory)
    assert res.cached is True
    assert calls == ["V"]
