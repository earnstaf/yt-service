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


def run_diarization_job(job_id: str) -> None:
    """RQ entry point for diarization. See :func:`_run_diarization_job_async`."""
    asyncio.run(_run_diarization_job_async(job_id))


async def _run_diarization_job_async(job_id: str) -> None:
    """Pipeline: load job → load transcript → download audio → diarize → persist.

    Refuses to diarize captions-source transcripts (JC-032). Cleans audio
    in ``finally``. Releases the diarize lock with the job_id as owner.
    """
    from app import cache as cache_mod  # noqa: PLC0415 — avoid cycles
    from app import diarization, jobs as jobs_mod  # noqa: PLC0415
    from app.redis_client import get_redis_client  # noqa: PLC0415
    from app.whisper.audio import cleanup as cleanup_audio, download_audio  # noqa: PLC0415

    factory = get_session_factory()
    redis = get_redis_client()

    audio_path: Path | None = None
    video_id: str | None = None

    try:
        async with factory() as session:
            job = await jobs_mod.get_job(session, job_id)
            if job is None:
                _logger.warning("diarization_job_missing", job_id=job_id)
                return
            video_id = job.video_id
            payload = job.payload_jsonb or {}
            language = payload.get("language", "en")

            await jobs_mod.mark_running(session, job_id)

            record = await cache_mod.get_transcript(session, video_id, language)
            if record is None:
                await jobs_mod.mark_failed(
                    session, job_id, "transcript not cached; fetch /v1/transcript first"
                )
                return

            if record.source == "youtube_captions":
                await jobs_mod.mark_failed(
                    session,
                    job_id,
                    "diarization not supported on captions-sourced transcripts; "
                    "use force=whisper to re-transcribe via Whisper first",
                )
                return

            if not diarization.is_available():
                await jobs_mod.mark_failed(
                    session,
                    job_id,
                    "diarization unavailable: HUGGINGFACE_TOKEN missing or "
                    "pyannote model not accessible",
                )
                return

        ACTIVE_JOBS.labels(type="enrichment").inc()
        try:
            with JOB_DURATION.labels(type="enrichment").time():
                audio_path = await download_audio(video_id, Path(settings.ytdlp_tmp_dir))
                tagged = await diarization.diarize(audio_path, record.snippets)

            async with factory() as session:
                rowcount = await cache_mod.put_diarization(
                    session, video_id, language, tagged, has_diarization=True
                )
                if rowcount == 0:
                    await session.rollback()
                    await jobs_mod.mark_failed(
                        session,
                        job_id,
                        "transcript row missing or purged during diarization",
                    )
                else:
                    await session.commit()
                    await jobs_mod.mark_complete(session, job_id)
        finally:
            ACTIVE_JOBS.labels(type="enrichment").dec()
    except Exception as exc:  # noqa: BLE001
        _logger.error(
            "diarization_job_unexpected_error",
            job_id=job_id,
            error=str(exc),
            exc_info=True,
        )
        try:
            async with factory() as session:
                await jobs_mod.mark_failed(session, job_id, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        if video_id is not None:
            try:
                await jobs.release_lock(redis, video_id, op="diarize", value=job_id)
            except Exception:  # noqa: BLE001
                pass
        if audio_path is not None:
            cleanup_audio(audio_path)


def make_worker(queue_name: str = "whisper") -> Worker:
    """Construct an RQ ``Worker`` bound to ``queue_name``.

    P2: ``queue_name`` is parameterized so the same factory drives both the
    whisper systemd unit and the enrichment unit. The worker uses a
    *synchronous* Redis client because RQ requires sync I/O for its
    pubsub keepalive — separate from the async client the orchestrator uses.
    """
    from app.redis_client import get_sync_redis_client  # noqa: PLC0415 — lazy

    sync_redis = get_sync_redis_client()
    queue = Queue(queue_name, connection=sync_redis)
    return Worker([queue], connection=sync_redis)


def make_enrichment_worker() -> Worker:
    """Convenience factory for the enrichment queue (diarization in P2)."""
    return make_worker(queue_name="enrichment")


if __name__ == "__main__":  # pragma: no cover — manual invocation only
    # ``python -m app.worker [queue_name]``. Defaults to whisper for back-compat.
    import sys

    from app.logging import configure_logging

    queue_name = sys.argv[1] if len(sys.argv) > 1 else "whisper"
    configure_logging(settings.yt_log_level)
    _logger.info("starting_worker", queue=queue_name, pid=os.getpid())
    worker = make_worker(queue_name=queue_name)
    worker.work(with_scheduler=True)


__all__ = [
    "run_whisper_job",
    "run_diarization_job",
    "make_worker",
    "make_enrichment_worker",
]
