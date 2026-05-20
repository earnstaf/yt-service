"""Async SQLAlchemy engine, session factory, and FastAPI dependency.

Everything is wired lazily because ``app.config.get_settings()`` itself is
lazy and we don't want module import to trigger a ``Settings()`` instantiation
that would fail in environments missing required env vars (e.g. during a fresh
``python -m app`` shell before the .env has been written).

Public surface:

- :func:`get_engine` — process-wide ``AsyncEngine`` (cached).
- :func:`get_session_factory` — process-wide ``async_sessionmaker`` (cached).
- :func:`get_session` — FastAPI dependency yielding an ``AsyncSession`` with
  commit-on-success / rollback-on-exception semantics.
- :func:`check_db_health` — runs ``SELECT 1`` and returns ``True``/``False``.
- :func:`close_db` — disposes the cached engine on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the cached ``AsyncEngine``, constructing it on first call.

    Defaults are conservative: ``pool_pre_ping=True`` to recover from idle
    connection drops on managed Postgres, and ``pool_size`` left at the
    SQLAlchemy default so behavior is predictable across environments.
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the cached session factory bound to the engine.

    ``expire_on_commit=False`` keeps ORM instances usable after commit, which
    matches the request/response lifecycle in FastAPI handlers.
    """
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields one session per request.

    Commits if the handler completes without raising. Rolls back on any
    exception so half-applied work doesn't survive. Always closes.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_db_health() -> bool:
    """Return ``True`` if a ``SELECT 1`` round-trip succeeds, else ``False``.

    Used by the ``/healthz`` endpoint. Catches every exception so a transient
    DB blip surfaces as ``False`` instead of bubbling to the health probe.
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            return result.scalar_one() == 1
    except Exception:
        return False


async def close_db() -> None:
    """Dispose the cached engine, if one was constructed.

    Safe to call multiple times. Clears the ``lru_cache`` so a subsequent
    ``get_engine()`` rebuilds from scratch (useful in tests that swap DSNs).
    """
    if get_engine.cache_info().currsize == 0:
        return
    engine = get_engine()
    await engine.dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


__all__ = [
    "get_engine",
    "get_session_factory",
    "get_session",
    "check_db_health",
    "close_db",
]
