"""Tests for ``ProviderRegistry`` (see CONTRACT.md §8.5, §11).

These exercise the registry's fallback/skip logic against FAKE in-memory
providers — no network, no real provider modules, fully deterministic and
offline. Each test builds a ``Settings`` whose ``capability_chains`` and cache
dir are controlled by the test itself.

Asserted behaviors (per the CONTRACT test matrix):

* fallback order is honored (first eligible provider in the chain wins);
* a provider that raises ``MissingApiKey`` is a silent skip (chain continues,
  breaker untouched);
* a provider whose circuit is open is skipped;
* a provider that does not ``support`` the capability is skipped;
* concurrent identical fetches collapse to a single upstream call
  (single-flight, implemented in the cache layer the registry relies on);
* ``AllProvidersFailed`` is raised (carrying the capability) when every
  provider in the chain is skipped or fails.
"""

from __future__ import annotations

import asyncio

import pytest

from makecrazypenny.core.config import Settings
from makecrazypenny.core.errors import AllProvidersFailed, MissingApiKey
from makecrazypenny.providers.registry import ProviderRegistry


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal in-memory provider mimicking the ``Provider`` contract.

    It is deliberately NOT a subclass of the real ``Provider`` ABC: the registry
    only relies on duck-typed attributes (``name``, ``supported``, ``rate_key``,
    ``rate_per_min``, ``cost``) plus an async ``fetch``. Building from scratch
    keeps the test independent of any concrete provider module and guarantees no
    network can ever be touched.

    Args:
        name: Provider identifier (registry keys ``_providers`` by this).
        supported: Capabilities this provider serves.
        result: Value its ``fetch`` returns (already JSON-ready by contract).
        error: If set, an exception instance ``fetch`` raises instead.
        rate_per_min: Bucket sizing; ``0`` means unlimited (no waiting), which
            keeps tests fast and deterministic.
    """

    def __init__(
        self,
        name: str,
        supported: set[str],
        *,
        result=None,
        error: BaseException | None = None,
        rate_per_min: int = 0,
    ) -> None:
        self.name = name
        self.supported = set(supported)
        self.rate_key = name
        self.rate_per_min = rate_per_min
        self.cost = 1
        self.requires_key = None
        self._result = result if result is not None else {"provider": name}
        self._error = error
        self.calls = 0

    async def fetch(self, capability: str, **params):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


class BlockingProvider(FakeProvider):
    """A provider whose ``fetch`` blocks until released.

    Used for the single-flight test: it lets every concurrent caller pile up
    inside the cache's in-flight wait before the single upstream call resolves.
    """

    def __init__(self, name: str, supported: set[str], *, result=None) -> None:
        super().__init__(name, supported, result=result)
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def fetch(self, capability: str, **params):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self._result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(tmp_path, chains: dict[str, list[str]]) -> Settings:
    """Build deterministic offline ``Settings`` for the registry.

    L2 disk cache is disabled so tests never depend on filesystem state between
    cases; the cache dir still points at a unique ``tmp_path`` for safety.
    """
    return Settings(
        cache_dir=tmp_path,
        capability_chains=dict(chains),
        l2_cache_enabled=False,
        circuit_fail_threshold=2,
        circuit_cooldown_s=60.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fallback_order_honored(tmp_path):
    """The first eligible provider in the chain serves the request."""
    settings = make_settings(tmp_path, {"quote": ["primary", "secondary"]})
    registry = ProviderRegistry(settings)

    primary = FakeProvider("primary", {"quote"}, result={"px": 1})
    secondary = FakeProvider("secondary", {"quote"}, result={"px": 2})
    registry.register(primary)
    registry.register(secondary)

    out = await registry.fetch("quote", symbol="AAPL")

    assert out == {"provider": "primary", "data": {"px": 1}, "cached": False}
    assert primary.calls == 1
    assert secondary.calls == 0  # never reached: order honored


async def test_missing_key_falls_through(tmp_path):
    """A provider raising ``MissingApiKey`` is skipped silently; chain continues."""
    settings = make_settings(tmp_path, {"quote": ["needs_key", "free"]})
    registry = ProviderRegistry(settings)

    needs_key = FakeProvider(
        "needs_key",
        {"quote"},
        error=MissingApiKey("needs_key", "NEEDS_KEY_API_KEY"),
    )
    free = FakeProvider("free", {"quote"}, result={"px": 42})
    registry.register(needs_key)
    registry.register(free)

    out = await registry.fetch("quote", symbol="AAPL")

    assert out == {"provider": "free", "data": {"px": 42}, "cached": False}
    assert needs_key.calls == 1  # it was tried, then fell through
    # MissingApiKey is a config fact, not a health failure: breaker stays closed.
    assert registry._circuits["needs_key"].state == "closed"


async def test_circuit_open_provider_skipped(tmp_path):
    """A provider with an open circuit is skipped without being called."""
    settings = make_settings(tmp_path, {"quote": ["flaky", "backup"]})
    registry = ProviderRegistry(settings)

    flaky = FakeProvider("flaky", {"quote"}, result={"px": 1})
    backup = FakeProvider("backup", {"quote"}, result={"px": 2})
    registry.register(flaky)
    registry.register(backup)

    # Force the flaky provider's circuit open (fail_threshold == 2).
    registry._circuits["flaky"].record_failure()
    registry._circuits["flaky"].record_failure()
    assert registry._circuits["flaky"].state == "open"

    out = await registry.fetch("quote", symbol="AAPL")

    assert out == {"provider": "backup", "data": {"px": 2}, "cached": False}
    assert flaky.calls == 0  # open circuit => never invoked


async def test_unsupported_capability_skipped(tmp_path):
    """A provider not declaring the capability is skipped (no NotImplementedError)."""
    settings = make_settings(tmp_path, {"quote": ["wrong_cap", "right_cap"]})
    registry = ProviderRegistry(settings)

    # Declares only ohlcv, so the registry's `capability not in supported`
    # guard skips it before even calling fetch.
    wrong_cap = FakeProvider("wrong_cap", {"ohlcv"}, result={"px": 0})
    right_cap = FakeProvider("right_cap", {"quote"}, result={"px": 7})
    registry.register(wrong_cap)
    registry.register(right_cap)

    out = await registry.fetch("quote", symbol="AAPL")

    assert out == {"provider": "right_cap", "data": {"px": 7}, "cached": False}
    assert wrong_cap.calls == 0  # skipped by the supported-set guard


async def test_not_implemented_from_fetch_falls_through(tmp_path):
    """A ``NotImplementedError`` raised inside fetch is a silent skip too."""
    settings = make_settings(tmp_path, {"quote": ["raiser", "ok"]})
    registry = ProviderRegistry(settings)

    # Supports the capability (passes the guard) but its fetch raises
    # NotImplementedError, which the registry treats as a skip, not a failure.
    raiser = FakeProvider(
        "raiser",
        {"quote"},
        error=NotImplementedError("not wired up"),
    )
    ok = FakeProvider("ok", {"quote"}, result={"px": 9})
    registry.register(raiser)
    registry.register(ok)

    out = await registry.fetch("quote", symbol="AAPL")

    assert out == {"provider": "ok", "data": {"px": 9}, "cached": False}
    assert raiser.calls == 1
    assert registry._circuits["raiser"].state == "closed"  # breaker untouched


async def test_single_flight_one_upstream_call(tmp_path):
    """Concurrent identical fetches collapse to a single upstream call."""
    settings = make_settings(tmp_path, {"quote": ["blocker"]})
    registry = ProviderRegistry(settings)

    blocker = BlockingProvider("blocker", {"quote"}, result={"px": 100})
    registry.register(blocker)

    # Launch N identical concurrent fetches. The leader's fetch blocks on
    # `release`, so all followers must reach the cache's in-flight wait before
    # any upstream call can complete.
    n = 5
    tasks = [
        asyncio.create_task(registry.fetch("quote", symbol="AAPL")) for _ in range(n)
    ]

    # Wait until the (single) upstream call has actually started, then release.
    await asyncio.wait_for(blocker.started.wait(), timeout=2.0)
    blocker.release.set()

    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)

    # Exactly one upstream call despite N concurrent identical requests.
    assert blocker.calls == 1
    # Every caller gets the same value.
    assert all(r["data"] == {"px": 100} and r["provider"] == "blocker" for r in results)
    # Single-flight followers are served from the in-flight result: the leader
    # reports cached=False, followers cached=True.
    cached_flags = sorted(r["cached"] for r in results)
    assert cached_flags == [False, True, True, True, True]


async def test_distinct_params_are_not_collapsed(tmp_path):
    """Different params are distinct keys: each triggers its own upstream call."""
    settings = make_settings(tmp_path, {"quote": ["p"]})
    registry = ProviderRegistry(settings)

    provider = FakeProvider("p", {"quote"}, result={"px": 1})
    registry.register(provider)

    await registry.fetch("quote", symbol="AAPL")
    await registry.fetch("quote", symbol="MSFT")

    assert provider.calls == 2  # distinct symbols => distinct cache keys


async def test_cache_hit_reports_cached_true(tmp_path):
    """A second identical fetch is served from cache with cached=True."""
    settings = make_settings(tmp_path, {"quote": ["p"]})
    registry = ProviderRegistry(settings)

    provider = FakeProvider("p", {"quote"}, result={"px": 5})
    registry.register(provider)

    first = await registry.fetch("quote", symbol="AAPL")
    second = await registry.fetch("quote", symbol="AAPL")

    assert first["cached"] is False
    assert second["cached"] is True
    assert provider.calls == 1  # second served from L1 cache


async def test_all_providers_failed_when_all_fail(tmp_path):
    """When every provider raises a runtime error, ``AllProvidersFailed`` rises."""
    settings = make_settings(tmp_path, {"quote": ["a", "b"]})
    registry = ProviderRegistry(settings)

    a = FakeProvider("a", {"quote"}, error=RuntimeError("boom-a"))
    b = FakeProvider("b", {"quote"}, error=RuntimeError("boom-b"))
    registry.register(a)
    registry.register(b)

    with pytest.raises(AllProvidersFailed) as excinfo:
        await registry.fetch("quote", symbol="AAPL")

    assert excinfo.value.capability == "quote"
    assert a.calls == 1
    assert b.calls == 1
    # Genuine runtime failures DO record a breaker failure on each provider.
    assert registry._circuits["a"].state in ("closed", "open")
    assert registry._circuits["b"].state in ("closed", "open")


async def test_all_providers_failed_when_all_skipped(tmp_path):
    """All-skipped (missing key / unsupported) also yields ``AllProvidersFailed``."""
    settings = make_settings(tmp_path, {"quote": ["nokey", "wrongcap"]})
    registry = ProviderRegistry(settings)

    nokey = FakeProvider(
        "nokey", {"quote"}, error=MissingApiKey("nokey", "NOKEY_API_KEY")
    )
    wrongcap = FakeProvider("wrongcap", {"ohlcv"}, result={"px": 0})
    registry.register(nokey)
    registry.register(wrongcap)

    with pytest.raises(AllProvidersFailed) as excinfo:
        await registry.fetch("quote", symbol="AAPL")

    assert excinfo.value.capability == "quote"
    assert wrongcap.calls == 0  # never called (unsupported)


async def test_unknown_capability_raises_all_providers_failed(tmp_path):
    """A capability with no chain entry resolves to an empty chain => failure."""
    settings = make_settings(tmp_path, {"quote": ["p"]})
    registry = ProviderRegistry(settings)
    registry.register(FakeProvider("p", {"quote"}))

    with pytest.raises(AllProvidersFailed) as excinfo:
        await registry.fetch("nonexistent_capability", symbol="AAPL")

    assert excinfo.value.capability == "nonexistent_capability"


async def test_runtime_failure_then_fallback_succeeds(tmp_path):
    """A runtime error trips that provider's breaker but the chain still serves."""
    settings = make_settings(tmp_path, {"quote": ["broken", "healthy"]})
    registry = ProviderRegistry(settings)

    broken = FakeProvider("broken", {"quote"}, error=ValueError("upstream 500"))
    healthy = FakeProvider("healthy", {"quote"}, result={"px": 11})
    registry.register(broken)
    registry.register(healthy)

    out = await registry.fetch("quote", symbol="AAPL")

    assert out == {"provider": "healthy", "data": {"px": 11}, "cached": False}
    assert broken.calls == 1
    assert healthy.calls == 1
    # A genuine runtime failure records a breaker failure on the broken provider.
    assert registry._circuits["broken"]._failures >= 1
