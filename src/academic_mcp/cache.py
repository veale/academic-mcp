"""Short-TTL response cache with in-flight request coalescing.

Agent workflows re-issue near-identical searches constantly — a refinement
loop hits ``s2_search`` with the same keywords three or four times in a
minute, and every one of those was a fresh round-trip (and a fresh chance of
a 429 from Semantic Scholar, which rate-limits hard without an API key).

``ttl_cached`` memoises an async function's return value for
``SEARCH_CACHE_TTL`` seconds, and coalesces concurrent callers with the same
arguments onto a single in-flight request.

Cached values are shared, not copied. Every consumer of these API responses
treats them as read-only; do not mutate a returned payload in place.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import OrderedDict
from typing import Any, Callable

from .config import config

logger = logging.getLogger(__name__)


class _TTLCache:
    """Bounded, time-expiring store. Not thread-safe; asyncio-only."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = max(1, maxsize)
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any, ttl: float) -> tuple[bool, Any]:
        entry = self._data.get(key)
        if entry is None:
            return False, None
        stored_at, value = entry
        if time.monotonic() - stored_at > ttl:
            self._data.pop(key, None)
            return False, None
        self._data.move_to_end(key)
        return True, value

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


def _make_key(namespace: str, args: tuple, kwargs: dict) -> Any:
    return (namespace, args, tuple(sorted(kwargs.items())))


def _swallow(fut: "asyncio.Future") -> None:
    """Mark a failed future's exception as retrieved so asyncio stays quiet."""

    def _cb(f: "asyncio.Future") -> None:
        if not f.cancelled():
            f.exception()

    fut.add_done_callback(_cb)


def ttl_cached(namespace: str) -> Callable:
    """Memoise an async function for ``config.search_cache_ttl`` seconds.

    A TTL of 0 (or less) disables caching entirely, which keeps the decorator
    inert for callers who set ``SEARCH_CACHE_TTL=0``.
    """

    def decorator(fn: Callable) -> Callable:
        cache = _TTLCache(config.search_cache_maxsize)
        # key -> (owning loop, future). Coalescing only applies within one loop.
        inflight: dict[Any, tuple[asyncio.AbstractEventLoop, "asyncio.Future"]] = {}

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ttl = config.search_cache_ttl
            if ttl <= 0:
                return await fn(*args, **kwargs)

            try:
                key = _make_key(namespace, args, kwargs)
                hash(key)
            except TypeError:  # unhashable argument — pass straight through
                return await fn(*args, **kwargs)

            hit, value = cache.get(key, ttl)
            if hit:
                logger.debug("cache hit: %s", namespace)
                return value

            loop = asyncio.get_running_loop()
            pending = inflight.get(key)
            if pending is not None and pending[0] is loop:
                # Someone else is already fetching this exact request. Shield so
                # our own cancellation doesn't kill the shared fetch.
                return await asyncio.shield(pending[1])

            fut: "asyncio.Future" = loop.create_future()
            inflight[key] = (loop, fut)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                inflight.pop(key, None)
                if not fut.done():
                    fut.set_exception(exc)
                    _swallow(fut)
                raise
            else:
                cache.set(key, result)
                inflight.pop(key, None)
                if not fut.done():
                    fut.set_result(result)
                return result

        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorator
