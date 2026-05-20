"""Alembic environment for yt-transcript-service.

Reads ``DATABASE_URL`` from :mod:`app.config` instead of ``alembic.ini`` so a
single ``.env`` file drives the application and migrations alike. The script
runs migrations through an async engine, which matches the runtime DB layer.

Online mode (default): ``alembic upgrade head`` etc., uses a live connection.
Offline mode: ``alembic upgrade head --sql`` emits SQL without connecting.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.models import Base

config = context.config

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Configure the context with a URL only and emit SQL to stdout."""
    url = get_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations against a live sync-style connection bridge."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an async engine, run migrations through ``run_sync``, dispose."""
    settings = get_settings()
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.database_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Sync wrapper that runs the async migration flow."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
