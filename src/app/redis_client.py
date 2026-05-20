"""Async Redis client factory, health check, and shutdown.

Module name: this file is intentionally named ``redis_client.py`` rather than
``redis.py``. A module named ``app/redis.py`` would shadow the upstream
``redis`` distribution when resolving ``from redis.asyncio import Redis`` from
inside the ``app`` package (the local ``app.redis`` wins), breaking imports in
a subtle, hard-to-diagnose way. ``app/logging.py`` gets away with the same
trick only because it never imports the stdlib ``logging`` package through a
relative-looking dotted path. Redis is not that lucky.

Public surface:

- :func:`get_redis_client` — process-wide ``redis.asyncio.Redis`` (cached).
- :func:`check_redis_health` — runs ``PING`` and returns ``True``/``False``.
- :func:`close_redis` — closes the cached client on shutdown.

``decode_responses=False`` is deliberate. Several upstream uses (HMAC payloads
and raw-byte distributed locks) need ``bytes`` return values, and decoding at
the boundary is cheaper than re-encoding everywhere else.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from app.config import get_settings

if TYPE_CHECKING:
    # Imported only for type checkers; sync client is constructed lazily in
    # :func:`get_sync_redis_client`.
    from redis import Redis as SyncRedis


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    """Return the cached async Redis client, building it on first call.

    Pulls ``redis_url`` from settings lazily so import of this module does
    not require a populated environment.
    """
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=False)


@lru_cache(maxsize=1)
def get_sync_redis_client() -> "SyncRedis":
    """Return the cached SYNCHRONOUS Redis client (built on first call).

    RQ's ``Queue`` and ``Worker`` APIs are synchronous: they block on
    ``BLPOP`` / pubsub keepalive and cannot consume a ``redis.asyncio.Redis``
    instance. The async client in :func:`get_redis_client` covers the
    coroutine-driven paths (SETNX locks, INCR rate limits); this companion
    covers the RQ paths in :mod:`app.jobs`, :mod:`app.webhooks`, and
    :mod:`app.worker`.

    ``decode_responses=False`` matches the async client so byte-level lock
    values compare cleanly across both surfaces.
    """
    from redis import Redis as SyncRedis  # noqa: PLC0415 — lazy by design

    settings = get_settings()
    return SyncRedis.from_url(settings.redis_url, decode_responses=False)


async def check_redis_health() -> bool:
    """Return ``True`` if Redis responds to ``PING``, else ``False``.

    Catches every exception so a transient outage surfaces as ``False`` to
    the health probe instead of bubbling.
    """
    try:
        client = get_redis_client()
        pong = await client.ping()
        # Some Redis clients return ``True``; others return ``b'PONG'``.
        return bool(pong)
    except Exception:
        return False


async def close_redis() -> None:
    """Close the cached Redis client, if one was constructed.

    Safe to call multiple times. Clears the ``lru_cache`` so a subsequent
    ``get_redis_client()`` builds a fresh client.
    """
    if get_redis_client.cache_info().currsize == 0:
        return
    client = get_redis_client()
    await client.aclose()
    get_redis_client.cache_clear()


def close_sync_redis() -> None:
    """Close the cached synchronous Redis client, if one was constructed.

    Safe to call multiple times. Clears the ``lru_cache`` so a subsequent
    ``get_sync_redis_client()`` builds a fresh client.
    """
    if get_sync_redis_client.cache_info().currsize == 0:
        return
    client = get_sync_redis_client()
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass
    get_sync_redis_client.cache_clear()


__all__ = [
    "get_redis_client",
    "get_sync_redis_client",
    "check_redis_health",
    "close_redis",
    "close_sync_redis",
]
