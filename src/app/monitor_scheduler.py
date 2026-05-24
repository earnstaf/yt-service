"""Standalone monitor scheduler process (P3 C2).

Runs out-of-band of the API and worker pool. APScheduler polls each unpaused
monitor's RSS feed at its configured cadence. New videos are fed through the
existing ``transcript_service.get_or_fetch`` orchestrator (cache, captions,
Whisper enqueue, all reused) and a per-monitor webhook fires on each new
result.

Launched via ``deploy/yt-transcript-monitor.service`` → ``python -m
app.monitor_scheduler``. Crash-recovery: on startup it re-reads the monitor
set from Postgres. Newly-created monitors picked up on the periodic reload
(every 5 minutes by default).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import feedparser

from app import jobs, monitors, transcript_service, webhooks
from app.config import settings
from app.db import close_db, get_session_factory
from app.domain import TranscriptRequest
from app.logging import configure_logging, get_logger
from app.metrics import MONITOR_POLLS
from app.models import Monitor
from app.redis_client import close_redis, get_redis_client

_logger = get_logger("monitor_scheduler")

_RELOAD_SECONDS = 300  # reload monitor set every 5 minutes
_RSS_TIMEOUT_SECONDS = 30


def _rss_url_for(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


async def _resolve_token_secret(session, token_id: str) -> str | None:
    """Look up the per-token webhook secret used to sign monitor callbacks."""
    from app.models import Token

    if not token_id:
        return None
    row = (
        await session.execute(__import__("sqlalchemy").select(Token).where(Token.id == token_id))
    ).scalar_one_or_none()
    return getattr(row, "webhook_secret", None) if row else None


async def _poll_one(monitor: Monitor) -> None:
    """Poll a single monitor: fetch RSS, dispatch new videos, fire callbacks."""
    rss_url = _rss_url_for(monitor.channel_id)
    started = time.monotonic()
    try:
        parsed = await asyncio.wait_for(
            asyncio.to_thread(feedparser.parse, rss_url),
            timeout=_RSS_TIMEOUT_SECONDS,
        )
    except (TimeoutError, Exception) as exc:  # noqa: BLE001
        MONITOR_POLLS.labels(monitor_id=monitor.id, result="fetch_error").inc()
        _logger.warning("monitor_rss_fetch_failed", monitor_id=monitor.id, error=str(exc))
        return

    entries = list(getattr(parsed, "entries", []) or [])
    if not entries:
        MONITOR_POLLS.labels(monitor_id=monitor.id, result="empty").inc()
        return

    # RSS entries are newest-first. Walk until we hit the last-seen.
    new_video_ids: list[str] = []
    for entry in entries:
        vid = getattr(entry, "yt_videoid", None) or _extract_vid_from_link(entry.get("link", ""))
        if not vid:
            continue
        if monitor.last_video_id and vid == monitor.last_video_id:
            break
        new_video_ids.append(vid)

    if not new_video_ids:
        MONITOR_POLLS.labels(monitor_id=monitor.id, result="no_new").inc()
        async with get_session_factory()() as session:
            await monitors.mark_polled(session, monitor.id, last_video_id=None)
            await session.commit()
        return

    # Dispatch newest-first so the most recent video lands in last_video_id.
    # The transcript request carries the MONITOR's callback_url so the worker
    # fires it on completion (queued case). Cache hits fire the callback here
    # synchronously since no async job will be enqueued.
    redis_async = get_redis_client()
    async with get_session_factory()() as session:
        secret = await _resolve_token_secret(session, monitor.created_by) or ""
        newest_succeeded: str | None = None
        for vid in reversed(new_video_ids):  # oldest-first so failures don't skip newer
            try:
                request = TranscriptRequest(
                    video_id=vid,
                    language="en",
                    force=None,
                    wait_seconds=0,
                    include=list(monitor.include_jsonb or []),
                    # Pass the monitor's callback so the worker fires it when the
                    # whisper job completes (H2). For cache hits we fire below.
                    callback_url=monitor.callback_url,
                    token_id=monitor.created_by,
                )
                kind, payload = await transcript_service.get_or_fetch(
                    session, redis_async, request, token_id=monitor.created_by
                )
                if kind == "transcript":
                    # Cache hit — worker won't run; fire callback synchronously.
                    webhooks.enqueue_webhook(
                        None,
                        monitor.callback_url,
                        event="monitor.new_video",
                        payload={
                            "monitor_id": monitor.id,
                            "video_id": vid,
                            "outcome": "transcript",
                            "payload": _serialize_for_callback(payload),
                        },
                        secret=secret,
                    )
                # else: queued — worker will fire the webhook on completion.
                newest_succeeded = vid
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "monitor_dispatch_failed",
                    monitor_id=monitor.id,
                    video_id=vid,
                    error=str(exc),
                )
                # H4: stop advancing — leaving the failed video as the next-poll
                # candidate so it'll be retried on the next cycle.
                break

        # Only mark progress through the videos that actually dispatched.
        await monitors.mark_polled(session, monitor.id, last_video_id=newest_succeeded)
        await session.commit()

    elapsed = time.monotonic() - started
    MONITOR_POLLS.labels(monitor_id=monitor.id, result="dispatched").inc()
    _logger.info(
        "monitor_polled",
        monitor_id=monitor.id,
        new_videos=len(new_video_ids),
        elapsed_seconds=elapsed,
    )


def _extract_vid_from_link(link: str) -> str | None:
    """Fallback when feedparser doesn't expose yt_videoid: parse ?v= from the link."""
    from urllib.parse import parse_qs, urlparse

    try:
        qs = parse_qs(urlparse(link).query)
        v = (qs.get("v") or [""])[0]
        return v if v and len(v) == 11 else None
    except Exception:  # noqa: BLE001
        return None


def _serialize_for_callback(payload: Any) -> dict:
    """Convert a pydantic response (TranscriptResponse or JobAcceptedResponse) to a plain dict."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json", exclude_unset=True)
    return dict(payload) if isinstance(payload, dict) else {"raw": str(payload)}


async def _poll_loop() -> None:
    """Main loop: load monitor set, poll each at its cadence, periodically reload."""
    next_runs: dict[str, float] = {}
    last_reload = 0.0
    monitors_cache: list[Monitor] = []

    while True:
        now = time.monotonic()
        if now - last_reload > _RELOAD_SECONDS:
            async with get_session_factory()() as session:
                monitors_cache = await monitors.list_monitors(session, include_paused=False)
            last_reload = now
            _logger.info("monitor_set_reloaded", count=len(monitors_cache))
            # Reset run schedule so newly-created monitors get a near-immediate first poll.
            for monitor in monitors_cache:
                next_runs.setdefault(monitor.id, now)

        for monitor in monitors_cache:
            if next_runs.get(monitor.id, 0) <= now:
                try:
                    await _poll_one(monitor)
                except Exception as exc:  # noqa: BLE001
                    _logger.error("monitor_poll_crashed", monitor_id=monitor.id, error=str(exc))
                next_runs[monitor.id] = now + (monitor.poll_interval_minutes * 60)

        await asyncio.sleep(10.0)


async def _main() -> None:
    configure_logging(settings.yt_log_level)
    _logger.info("monitor_scheduler_starting", env=settings.yt_env)
    try:
        await _poll_loop()
    finally:
        await close_db()
        await close_redis()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())


__all__ = ["_poll_one", "_poll_loop"]
