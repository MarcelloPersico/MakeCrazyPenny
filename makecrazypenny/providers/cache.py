"""Two-tier TTL cache with async single-flight (see CONTRACT.md §8.3).

Layers:
  * **L1** — in-memory ``dict`` of ``key -> (expires_at, value)``.
  * **L2** — on-disk JSON files under the configured cache directory.

``get_or_fetch`` returns a :class:`CacheResult` carrying the value and a
``cached`` flag, and guarantees the ``factory`` runs *exactly once* even under
concurrent identical keys (single-flight via per-key futures).

L2 is best-effort: corrupt or expired entries are ignored and any disk error
degrades the cache to L1-only — it never raises to the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class CacheResult:
    """Outcome of :meth:`TTLCache.get_or_fetch`.

    Attributes:
        value: The cached or freshly fetched value.
        cached: ``True`` if served from L1/L2 cache, ``False`` if just fetched.
    """

    value: Any
    cached: bool


def make_key(provider_name: str, capability: str, params: dict[str, Any]) -> str:
    """Build a stable string cache key from the request tuple.

    The params dict is serialized with sorted keys so identical requests with
    differently-ordered kwargs collapse to the same key.

    Args:
        provider_name: Provider that would serve the request.
        capability: Requested capability.
        params: Capability parameters.

    Returns:
        A deterministic JSON string usable as an L1 key and L2 filename seed.
    """
    payload = {
        "provider": provider_name,
        "capability": capability,
        "params": params,
    }
    return json.dumps(payload, sort_keys=True, default=str)


class TTLCache:
    """In-memory L1 + on-disk L2 JSON cache with async single-flight."""

    def __init__(self, cache_dir: str | os.PathLike[str], *, l2_enabled: bool = True) -> None:
        """Initialize the cache.

        Args:
            cache_dir: Directory for L2 JSON files (created lazily on write).
            l2_enabled: Whether the on-disk L2 tier is active.
        """
        self._cache_dir = Path(cache_dir)
        self._l2_enabled = l2_enabled
        self._l1: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    # -- L2 helpers ---------------------------------------------------------

    def _l2_path(self, key: str) -> Path:
        """Return the deterministic L2 file path for ``key``."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _l2_read(self, key: str) -> tuple[bool, Any, float]:
        """Read a fresh value from L2.

        Returns:
            ``(hit, value, expires_at)`` where ``hit`` is ``True`` only for a
            fresh entry. Any error or staleness yields ``(False, None, 0.0)``.
        """
        if not self._l2_enabled:
            return False, None, 0.0
        path = self._l2_path(key)
        try:
            with path.open("r", encoding="utf-8") as fh:
                record = json.load(fh)
            expires_at = float(record["expires_at"])
            if expires_at <= time.time():
                return False, None, 0.0
            return True, record["value"], expires_at
        except (OSError, ValueError, KeyError, TypeError):
            # Missing, corrupt, or unreadable: silently miss.
            return False, None, 0.0

    def _l2_write(self, key: str, value: Any, expires_at: float) -> None:
        """Mirror a value to L2, swallowing any disk error."""
        if not self._l2_enabled:
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._l2_path(key)
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"expires_at": expires_at, "value": value}, fh, default=str)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            # Disk/serialization issue: degrade to L1-only.
            pass

    # -- public API ---------------------------------------------------------

    def _get_fresh(self, key: str) -> tuple[bool, Any]:
        """Look up ``key`` in L1 then L2, returning ``(hit, value)``.

        On an L2 hit the value is promoted back into L1.
        """
        now = time.time()
        entry = self._l1.get(key)
        if entry is not None:
            expires_at, value = entry
            if expires_at > now:
                return True, value
            # Expired L1 entry; drop it.
            self._l1.pop(key, None)

        hit, value, expires_at = self._l2_read(key)
        if hit:
            # Promote into L1 with L2's own (validated) expiry, so subsequent
            # lookups stop paying the disk read.
            self._l1[key] = (expires_at, value)
            return True, value
        return False, None

    async def get_or_fetch(
        self,
        key: str | tuple[Any, ...],
        ttl: float,
        factory: Callable[[], Awaitable[Any]],
    ) -> CacheResult:
        """Return a fresh cached value or fetch it via ``factory`` (single-flight).

        If a fresh value exists in L1 or L2 it is returned with ``cached=True``.
        Otherwise ``factory`` is invoked exactly once even under concurrent
        identical keys; the result is stored in L1 and mirrored to L2 and
        returned with ``cached=False``.

        Args:
            key: Cache key. Accepts the registry's ``(name, capability, params)``
                tuple or a pre-serialized string.
            ttl: Time-to-live in seconds for the stored value.
            factory: Zero-arg coroutine factory producing the value on a miss.

        Returns:
            A :class:`CacheResult` with ``value`` and ``cached`` flag.
        """
        skey = self._coerce_key(key)

        # Fast path: fresh value already cached.
        hit, value = self._get_fresh(skey)
        if hit:
            return CacheResult(value=value, cached=True)

        # Single-flight: ensure only one factory call per key.
        async with self._lock:
            # Re-check under the lock in case another task just populated it.
            hit, value = self._get_fresh(skey)
            if hit:
                return CacheResult(value=value, cached=True)

            inflight = self._inflight.get(skey)
            if inflight is None:
                loop = asyncio.get_event_loop()
                inflight = loop.create_future()
                self._inflight[skey] = inflight
                leader = True
            else:
                leader = False

        if not leader:
            # Followers await the in-flight result; it is a cache-backed value.
            value = await inflight
            return CacheResult(value=value, cached=True)

        # Leader performs the single upstream call.
        try:
            value = await factory()
        except BaseException as exc:  # noqa: BLE001 - propagate after cleanup
            async with self._lock:
                self._inflight.pop(skey, None)
            if not inflight.done():
                inflight.set_exception(exc)
                # Mark the future's exception retrieved so asyncio does not log
                # "Future exception was never retrieved" when no follower awaits
                # it (the common single-caller case). Followers that *do* await
                # still receive the exception via ``await inflight`` below.
                inflight.exception()
            raise

        expires_at = time.time() + ttl
        self._l1[skey] = (expires_at, value)
        self._l2_write(skey, value, expires_at)

        async with self._lock:
            self._inflight.pop(skey, None)
        if not inflight.done():
            inflight.set_result(value)

        return CacheResult(value=value, cached=False)

    @staticmethod
    def _coerce_key(key: str | tuple[Any, ...]) -> str:
        """Normalize a tuple/string key into a stable string.

        Tuples of the shape ``(name, capability, params)`` are routed through
        :func:`make_key`; other tuples are JSON-serialized; strings pass through.
        """
        if isinstance(key, str):
            return key
        if isinstance(key, tuple) and len(key) == 3 and isinstance(key[2], dict):
            return make_key(key[0], key[1], key[2])
        return json.dumps(key, sort_keys=True, default=str)

    def clear(self) -> None:
        """Drop all in-memory (L1) entries. Does not touch L2 files."""
        self._l1.clear()


__all__ = ["TTLCache", "CacheResult", "make_key"]
