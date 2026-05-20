"""RQ worker entrypoints for yt-transcript-service.

Owns the synchronous task functions that RQ calls in worker processes. The
Whisper pipeline orchestrates audio download, transcription, cache write,
and (optional) webhook delivery. State transitions on the job row are
performed by :mod:`app.jobs`; this module composes them with the audio /
Whisper / cache layers.

Per spec §7.14 every job is guarded by a Redis ``lock:whisper:{video_id}``
acquired at enqueue time. The worker is responsible for releasing the lock
in the ``finally`` block regardless of outcome so a crashed worker doesn't
strand a video forever (the TTL also acts as a backstop).

Webhook delivery is enqueued onto the ``default`` RQ queue by
:func:`app.webhooks.enqueue_webhook`, NOT delivered inline. This matches
plan P-6 and keeps a stuck callback from tying up a Whisper worker.

Failure handling: per the task brief, no webhook fires on a failed job in
P1. P3 may introduce explicit ``transcript.failed`` callbacks as part of
the monitor surface.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rq import Queue, Worker
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, jobs, webhooks
from app.config import settings
from app.db import get_session_factory
from app.deep_links import with_deep_links
from app.domain import TranscriptRecord
from app.exceptions import (
    NoAudioStreamError,
    VideoUnavailableError,
    WhisperFailedError,
    YTServiceError,
)
from app.logging import get_logger
from app.metrics import ACTIVE_JOBS, JOB_DURATION, TRANSCRIPT_SOURCE_TOTAL
from app.redis_client import get_redis_client
from app.whisper import transcribe as whisper_transcribe
from app.whisper.audio import cleanup as cleanup_audio
from app.whisper.audio import download_audio

_logger = get_logger("worker")

_WHISPER_QUEUE = "whisper"
_JOB_TYPE_LABEL = "whisper"
_WEBHOOK_EVENT_COMPLETE = "transcript.complete"


def run_whisper_job(job_id: str) -> None:
    """RQ task entrypoint for Whisper transcription.

    Synchronous wrapper around the async pipeline so RQ workers (which call
    tasks synchronously) can drive an async DB session and async Redis
    client. Each invocation gets its own event loop via ``asyncio.run``.
    """
    asyncio.run(_run_whisper_job_async(job_id))


async def _run_whisper_job_async(job_id: str) -> None:
    """Inner async pipeline for :func:`run_whisper_job`.

    Loads the job row, transitions through ``running`` / ``complete`` /
    ``failed`` states, writes the transcript to cache, and fires the
    webhook (if a callback URL is set). Audio files and Redis locks are
    released in ``finally`` regardless of outcome.
    """
    ACTIVE_JOBS.labels(type=_JOB_TYPE_LABEL).inc()
    audio_path: Path | None = None
    video_id_for_cleanup: str | None = None

    factory = get_session_factory()
    redis_client = get_redis_client()

    try:
        with JOB_DURATION.labels(type=_JOB_TYPE_LABEL).time():
            async with factory() as session:
                job = await jobs.get_job(session, job_id)
                if job is None:
                    _logger.error("whisper_job_missing", job_id=job_id)
                    return
                video_id = job.video_id
                video_id_for_cleanup = video_id
                payload: dict[str, Any] = dict(job.payload_jsonb or {})
                language = payload.get("language", "en")
                callback_url = job.callback_url
                token_id = job.token_id

                await jobs.mark_running(session, job_id)

                try:
                    tmp_dir = Path(settings.ytdlp_tmp_dir)
                    audio_path = await download_audio(video_id, tmp_dir)
                    result = await whisper_transcribe(audio_path, video_id=video_id)
                    snippets_with_links = with_deep_links(result.snippets, video_id)

                    record = TranscriptRecord(
                        video_id=video_id,
                        language=result.language or language,
                        source=result.source,
                        is_generated=True,
                        duration_seconds=result.duration_seconds,
                        snippet_count=len(snippets_with_links),
                        cached_at=datetime.now(timezone.utc),
                        snippets=snippets_with_links,
                        full_text=result.full_text,
                        chapters=None,
                        has_diarization=False,
                    )

                    await cache.put_transcript(session, record, settings.cache_ttl_days)
                    await session.commit()

                    await jobs.mark_complete(session, job_id)
                    TRANSCRIPT_SOURCE_TOTAL.labels(source=result.source).inc()

                    if callback_url:
                        await _enqueue_completion_webhook(
                            session=session,
                            redis_client=redis_client,
                            callback_url=callback_url,
                            job_id=job_id,
                            video_id=video_id,
                            source=result.source,
                            token_id=token_id,
                        )

                    _logger.info(
                        "whisper_job_complete",
                        job_id=job_id,
                        video_id=video_id,
                        source=result.source,
                        snippet_count=len(snippets_with_links),
                    )

                except (WhisperFailedError, NoAudioStreamError, VideoUnavailableError) as exc:
                    await jobs.mark_failed(session, job_id, exc.message)
                    _logger.warning(
                        "whisper_job_failed",
                        job_id=job_id,
                        video_id=video_id,
                        error_code=exc.error_code,
                        error=exc.message,
                    )
                except YTServiceError as exc:
                    await jobs.mark_failed(session, job_id, exc.message)
                    _logger.warning(
                        "whisper_job_failed",
                        job_id=job_id,
                        video_id=video_id,
                        error_code=exc.error_code,
                        error=exc.message,
                    )
                except Exception as exc:  # noqa: BLE001
                    await jobs.mark_failed(session, job_id, f"unexpected: {exc}")
                    _logger.exception(
                        "whisper_job_unexpected_error",
                        job_id=job_id,
                        video_id=video_id,
                    )
    finally:
        # Release the lock and clean up audio regardless of outcome. The lock
        # value is the same job_id we're running so the compare-and-delete in
        # ``release_lock`` will refuse to clobber a lock owned by anyone else
        # (covers stolen-and-replaced races; see H9).
        if video_id_for_cleanup is not None:
            try:
                await jobs.release_lock(
                    redis_client, video_id_for_cleanup, "whisper", value=job_id
                )
            except Exception:  # noqa: BLE001 — never let cleanup mask the real error
                _logger.warning("whisper_lock_release_failed", video_id=video_id_for_cleanup)
        if audio_path is not None:
            cleanup_audio(audio_path)
        ACTIVE_JOBS.labels(type=_JOB_TYPE_LABEL).dec()


async def _enqueue_completion_webhook(
    *,
    session: AsyncSession,
    redis_client: Any,
    callback_url: str,
    job_id: str,
    video_id: str,
    source: str,
    token_id: str,
) -> None:
    """Schedule a ``transcript.complete`` webhook for a finished Whisper job.

    The HMAC secret is read from the token row's ``webhook_secret`` column
    via the same session that wrote the cache row. If no secret is configured
    for the token, we fire the webhook with an empty secret — real
    deployments require a non-empty secret; this is a P1 simplification.
    """
    from sqlalchemy import select  # noqa: PLC0415 — lazy

    from app.models import Token  # noqa: PLC0415

    result = await session.execute(select(Token).where(Token.id == token_id))
    token = result.scalar_one_or_none()
    secret = (token.webhook_secret if token and token.webhook_secret else "") or ""

    payload = {
        "job_id": job_id,
        "video_id": video_id,
        "status": "complete",
        "source": source,
        "poll_url": jobs.poll_url_for(job_id),
    }
    webhooks.enqueue_webhook(
        redis_client=redis_client,
        callback_url=callback_url,
        event=_WEBHOOK_EVENT_COMPLETE,
        payload=payload,
        secret=secret,
        attempt=1,
    )


def make_worker() -> Worker:
    """Construct an RQ ``Worker`` bound to the ``whisper`` queue.

    Used by ``deploy/yt-transcript-worker-whisper.service`` as the systemd
    entrypoint and by the dev runner ``python -m app.worker``. The worker
    uses a *synchronous* Redis client because RQ requires sync I/O for its
    pubsub keepalive — separate from the async client the orchestrator uses.
    Routed through :func:`app.redis_client.get_sync_redis_client` so the
    shared cached instance is reused across modules.
    """
    from app.redis_client import get_sync_redis_client  # noqa: PLC0415 — lazy

    sync_redis = get_sync_redis_client()
    queue = Queue(_WHISPER_QUEUE, connection=sync_redis)
    return Worker([queue], connection=sync_redis)


if __name__ == "__main__":  # pragma: no cover — manual invocation only
    # Allow ``python -m app.worker`` to start a Whisper worker in dev.
    from app.logging import configure_logging

    configure_logging(settings.yt_log_level)
    _logger.info("starting_whisper_worker", pid=os.getpid())
    worker = make_worker()
    worker.work(with_scheduler=True)


__all__ = ["run_whisper_job", "make_worker"]
