"""Transcript orchestration — the brain behind ``GET /v1/transcript``.

Per spec §5.5, a transcript request walks a tiered fallback path:

1. If ``force=refresh``, purge the cache row first.
2. Try cached row (unless ``force=whisper``).
3. Try YouTube captions (unless ``force=whisper``).
4. Otherwise enqueue a Whisper job and either wait (up to 25s) or return 202.

The orchestrator returns a 2-tuple ``(kind, payload)`` where ``kind`` is one
of ``"transcript"`` or ``"job_accepted"``. The route handler in ``app.main``
inspects the tag to choose HTTP 200 vs 202. This keeps the orchestrator
free of any FastAPI / Starlette imports so it can be reused by the batch
endpoint and the monitor processor (P3) without circular imports.

JC-016: when ``enqueue_whisper`` raises ``JobInProgressError`` from a
duplicate submit, we *convert* that to a 202 ``JobAcceptedResponse`` here.
The 409 path is reserved for endpoints that explicitly opt into conflict
semantics (batch with ``force=whisper`` while a job is already running).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, jobs, youtube
from app.config import settings
from app.deep_links import with_deep_links
from app.domain import JobPayload, Snippet, TranscriptRecord, TranscriptRequest
from app.exceptions import (
    InvalidRequestError,
    JobInProgressError,
    RateLimitedError,
    VideoTooLongError,
    WhisperFailedError,
)
from app.logging import get_logger
from app.metrics import TRANSCRIPT_SOURCE_TOTAL
from app.schemas import (
    ChapterOut,
    JobAcceptedResponse,
    TranscriptResponse,
    TranscriptSnippetOut,
)

_logger = get_logger("transcript_service")

# Spec §5.5 caps the inline wait at 25 seconds. Anything longer should poll
# the /v1/jobs endpoint instead.
_MAX_WAIT_SECONDS = 25

# Default estimated completion seconds in 202 responses. A future task may
# compute this from video duration; for P1 we use the example value from
# spec §5.5 (90s).
_DEFAULT_ESTIMATED_SECONDS = 90

# Interval between job-status polls when ``wait_seconds`` > 0.
_POLL_INTERVAL_SECONDS = 0.5

OrchestratorTag = Literal["transcript", "job_accepted"]


def _snippet_to_out(snippet: Snippet) -> TranscriptSnippetOut:
    """Convert an internal ``Snippet`` dataclass to the API model."""
    return TranscriptSnippetOut(
        start=snippet.start,
        duration=snippet.duration,
        text=snippet.text,
        speaker=snippet.speaker,
        deep_link=snippet.deep_link,
    )


def _record_to_response(record: TranscriptRecord, *, cache_hit: bool) -> TranscriptResponse:
    """Render a cached ``TranscriptRecord`` as a ``TranscriptResponse``."""
    chapters_out: list[ChapterOut] | None = None
    if record.chapters:
        chapters_out = [
            ChapterOut(start=c.start, end=c.end, title=c.title) for c in record.chapters
        ]
    return TranscriptResponse(
        video_id=record.video_id,
        source=record.source,
        language=record.language,
        is_generated=record.is_generated,
        duration_seconds=record.duration_seconds,
        snippet_count=record.snippet_count,
        cached_at=record.cached_at,
        cache_hit=cache_hit,
        chapters=chapters_out,
        snippets=[_snippet_to_out(s) for s in record.snippets],
        full_text=record.full_text,
    )


def _captions_to_record(
    captions_result: Any,
    video_id: str,
) -> TranscriptRecord:
    """Build a ``TranscriptRecord`` from a ``CaptionsResult``, populating deep links."""
    snippets_with_links = with_deep_links(captions_result.snippets, video_id)
    return TranscriptRecord(
        video_id=video_id,
        language=captions_result.language,
        source="youtube_captions",
        is_generated=captions_result.is_generated,
        duration_seconds=captions_result.duration_seconds,
        snippet_count=len(snippets_with_links),
        cached_at=datetime.now(tz=timezone.utc),
        snippets=snippets_with_links,
        full_text=captions_result.full_text,
        chapters=None,
        has_diarization=False,
    )


def _job_accepted(job_id: str, video_id: str, status: str = "queued") -> JobAcceptedResponse:
    """Build the 202-shape response for a queued/running job."""
    return JobAcceptedResponse(
        job_id=job_id,
        status=status,  # type: ignore[arg-type]
        video_id=video_id,
        poll_url=jobs.poll_url_for(job_id),
        estimated_seconds=_DEFAULT_ESTIMATED_SECONDS,
    )


def _clamp_wait_seconds(value: int) -> int:
    """Clamp ``value`` into [0, _MAX_WAIT_SECONDS] or raise on negative.

    Negative ``wait`` is a client bug — we reject it. Anything above the cap
    is silently clamped to the cap; the spec writes "Max 25" rather than
    "reject > 25", and clamping is the friendlier UX.
    """
    if value < 0:
        raise InvalidRequestError("wait_seconds must be non-negative")
    return min(value, _MAX_WAIT_SECONDS)


async def get_or_fetch(
    session: AsyncSession,
    redis_client: Any,
    request: TranscriptRequest,
    token_id: str,
    whisper_rate_limit_hook: Callable[[], Awaitable[None]] | None = None,
) -> tuple[OrchestratorTag, TranscriptResponse | JobAcceptedResponse]:
    """Resolve a transcript request per spec §5.5.

    Returns a 2-tuple discriminating cache/captions success (HTTP 200) from a
    queued Whisper job (HTTP 202). The route handler maps the tag to the
    HTTP status code.

    ``whisper_rate_limit_hook`` is an optional coroutine the orchestrator
    awaits immediately before enqueueing a Whisper job. It exists so the API
    layer can apply ``enforce_rate_limit("whisper", ...)`` to the actual
    Whisper-enqueue path (and not to cache/captions hits). H8 in the review
    notes. The hook is expected to raise :class:`RateLimitedError` when the
    bucket is full.

    Raises:
        InvalidRequestError: ``wait_seconds`` was negative.
        WhisperFailedError: a polled Whisper job entered ``failed`` state.
        VideoTooLongError: yt-dlp reports duration over the configured cap.
        RateLimitedError: ``whisper_rate_limit_hook`` rejected the request.
        VideoUnavailableError, YouTubeBlockedError: bubbled from
            :mod:`app.youtube` for terminal upstream failures.
    """
    wait_seconds = _clamp_wait_seconds(request.wait_seconds)
    video_id = request.video_id
    language = request.language
    force = request.force

    if force == "refresh":
        await cache.purge_transcript(session, video_id)
        await session.commit()

    if force != "whisper":
        cached = await cache.get_transcript(session, video_id, language)
        if cached is not None:
            _logger.info(
                "transcript_cache_hit",
                video_id=video_id,
                language=language,
                source=cached.source,
            )
            return "transcript", _record_to_response(cached, cache_hit=True)

        captions_result = await youtube.fetch_captions(video_id, lang=language)
        if captions_result is not None:
            record = _captions_to_record(captions_result, video_id)
            await cache.put_transcript(session, record, settings.cache_ttl_days)
            await session.commit()
            TRANSCRIPT_SOURCE_TOTAL.labels(source="youtube_captions").inc()
            _logger.info(
                "transcript_captions_fetched",
                video_id=video_id,
                language=record.language,
                snippet_count=record.snippet_count,
            )
            return "transcript", _record_to_response(record, cache_hit=False)

    # No cache, no captions (or force=whisper bypassing both). Enqueue Whisper.

    # H12: refuse videos that exceed the duration cap BEFORE downloading audio.
    # yt-dlp metadata is best-effort; if the probe fails we proceed and let the
    # worker catch the over-cap case after audio download.
    try:
        metadata = await youtube.fetch_video_metadata(video_id)
    except Exception:  # noqa: BLE001 — probe must never block the request path
        metadata = None
    if metadata is not None:
        dur = metadata.get("duration")
        if isinstance(dur, (int, float)) and dur > settings.max_video_duration_seconds:
            raise VideoTooLongError(
                f"video duration {int(dur)}s exceeds limit "
                f"{settings.max_video_duration_seconds}s",
                details={
                    "video_id": video_id,
                    "duration_seconds": float(dur),
                    "limit_seconds": settings.max_video_duration_seconds,
                },
            )

    # H8: apply the per-token Whisper rate limit at the actual enqueue point.
    # The hook is supplied by the API layer; coroutine workers / tests can
    # omit it.
    if whisper_rate_limit_hook is not None:
        await whisper_rate_limit_hook()

    payload: JobPayload = JobPayload(
        video_id=video_id,
        language=language,
        force_whisper=(force == "whisper"),
        include=list(request.include),
        callback_url=request.callback_url,
    )

    try:
        job = await jobs.enqueue_whisper(session, redis_client, payload, token_id)
        job_id = job.job_id
    except JobInProgressError as exc:
        # JC-016: convert duplicate-submit into a 202 with the existing id.
        if not exc.existing_job_id:
            # Lock held but no row found — surface as 409 by re-raising.
            raise
        _logger.info(
            "transcript_job_in_progress_returning_202",
            video_id=video_id,
            existing_job_id=exc.existing_job_id,
        )
        return "job_accepted", _job_accepted(exc.existing_job_id, video_id, status="running")

    if wait_seconds > 0:
        accepted = await _wait_for_job_completion(
            session=session,
            video_id=video_id,
            language=language,
            job_id=job_id,
            wait_seconds=wait_seconds,
        )
        if accepted is not None:
            return accepted

    return "job_accepted", _job_accepted(job_id, video_id)


async def _wait_for_job_completion(
    *,
    session: AsyncSession,
    video_id: str,
    language: str,
    job_id: str,
    wait_seconds: int,
) -> tuple[OrchestratorTag, TranscriptResponse] | None:
    """Poll the job table for up to ``wait_seconds`` seconds.

    Returns:
        ``("transcript", TranscriptResponse)`` if the job completed and the
        cache row is now visible.
        ``None`` if the polling window elapsed without completion (caller
        should fall through to the 202 path).

    Raises:
        WhisperFailedError: the polled job entered ``failed`` state.
    """
    elapsed = 0.0
    while elapsed < wait_seconds:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS

        # SQLAlchemy's identity map may keep stale state; expire the session
        # so the next read hits the DB.
        session.expire_all()
        job = await jobs.get_job(session, job_id)
        if job is None:
            continue
        if job.status == "complete":
            cached = await cache.get_transcript(session, video_id, language)
            if cached is not None:
                return "transcript", _record_to_response(cached, cache_hit=False)
            # Job claims complete but cache row missing — unusual; fall through.
            continue
        if job.status == "failed":
            raise WhisperFailedError(
                job.error or "whisper job failed",
                details={"job_id": job_id, "video_id": video_id},
            )

    return None


__all__ = ["get_or_fetch"]
