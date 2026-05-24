"""Monitor CRUD for the P3 RSS-poller pipeline.

Monitors are persisted rows in the ``monitors`` table (P1 baseline schema).
The scheduler process (:mod:`app.monitor_scheduler`) periodically loads the
unpaused set and polls each channel's RSS feed for new uploads.

Module surface is intentionally narrow — create / list / pause-or-resume /
delete / fetch one. Per-row enrichment options (include / callback_url) are
stored verbatim and forwarded to ``transcript_service`` on each poll.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.exceptions import NotFoundError
from app.models import Monitor


def _new_monitor_id() -> str:
    return f"mon_{ULID()}"


async def create_monitor(
    session: AsyncSession,
    *,
    channel_id: str,
    channel_url: str,
    poll_interval_minutes: int,
    include: list[str],
    callback_url: str,
    notes: str | None,
    created_by: str,
) -> Monitor:
    """Insert a new monitor row. Caller commits."""
    monitor = Monitor(
        id=_new_monitor_id(),
        channel_id=channel_id,
        channel_url=channel_url,
        poll_interval_minutes=int(poll_interval_minutes),
        include_jsonb=list(include),
        callback_url=callback_url,
        notes=notes,
        created_by=created_by,
        paused=False,
    )
    session.add(monitor)
    await session.commit()
    await session.refresh(monitor)
    return monitor


async def list_monitors(session: AsyncSession, *, include_paused: bool = True) -> list[Monitor]:
    stmt = select(Monitor).order_by(Monitor.created_at.desc())
    if not include_paused:
        stmt = stmt.where(Monitor.paused.is_(False))
    return list((await session.execute(stmt)).scalars().all())


async def get_monitor(session: AsyncSession, monitor_id: str) -> Monitor:
    monitor = (
        await session.execute(select(Monitor).where(Monitor.id == monitor_id))
    ).scalar_one_or_none()
    if monitor is None:
        raise NotFoundError(f"monitor {monitor_id!r} not found")
    return monitor


async def delete_monitor(session: AsyncSession, monitor_id: str) -> None:
    monitor = await get_monitor(session, monitor_id)
    await session.delete(monitor)
    await session.commit()


async def set_paused(session: AsyncSession, monitor_id: str, paused: bool) -> Monitor:
    monitor = await get_monitor(session, monitor_id)
    monitor.paused = paused
    await session.commit()
    await session.refresh(monitor)
    return monitor


async def mark_polled(
    session: AsyncSession,
    monitor_id: str,
    last_video_id: str | None,
) -> None:
    """Record a successful poll. Caller commits."""
    from datetime import datetime, timezone

    monitor = await get_monitor(session, monitor_id)
    monitor.last_polled_at = datetime.now(timezone.utc)
    if last_video_id is not None:
        monitor.last_video_id = last_video_id


__all__ = [
    "create_monitor",
    "list_monitors",
    "get_monitor",
    "delete_monitor",
    "set_paused",
    "mark_polled",
]
