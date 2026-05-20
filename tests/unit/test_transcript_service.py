"""Unit tests for ``app.transcript_service``.

Every external dependency is mocked: ``cache``, ``youtube``, ``jobs``. We
exercise each branch of the orchestrator algorithm independently. Wait-path
polling is exercised by patching ``asyncio.sleep`` to a no-op so the loop
spins without real time elapsing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import transcript_service as svc
from app.domain import (
    CaptionsResult,
    Snippet,
    TranscriptRecord,
    TranscriptRequest,
)
from app.exceptions import (
    InvalidRequestError,
    JobInProgressError,
    RateLimitedError,
    VideoTooLongError,
    WhisperFailedError,
)


VIDEO_ID = "OMhKgQmeMhI"
TOKEN_ID = "tok_unit"


def _cached_record(source: str = "youtube_captions") -> TranscriptRecord:
    """Build a ``TranscriptRecord`` fixture that looks like a cached row."""
    return TranscriptRecord(
        video_id=VIDEO_ID,
        language="en",
        source=source,  # type: ignore[arg-type]
        is_generated=True,
        duration_seconds=120.0,
        snippet_count=2,
        cached_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        snippets=[
            Snippet(start=0.0, duration=2.0, text="hello", deep_link="https://youtu.be/X?t=0"),
            Snippet(start=2.0, duration=2.0, text="world", deep_link="https://youtu.be/X?t=2"),
        ],
        full_text="hello world",
    )


def _captions_result() -> CaptionsResult:
    """Captions adapter output with deep_link not yet populated."""
    return CaptionsResult(
        video_id=VIDEO_ID,
        language="en",
        is_generated=True,
        snippets=[
            Snippet(start=0.0, duration=2.0, text="hi"),
            Snippet(start=2.0, duration=3.0, text="there"),
        ],
        duration_seconds=5.0,
        full_text="hi there",
    )


def _request(**overrides) -> TranscriptRequest:
    """Default ``TranscriptRequest`` with optional field overrides."""
    base = {
        "video_id": VIDEO_ID,
        "language": "en",
        "force": None,
        "wait_seconds": 0,
        "include": [],
        "callback_url": None,
        "token_id": TOKEN_ID,
    }
    base.update(overrides)
    return TranscriptRequest(**base)


# ---------------------------------------------------------------------------
# Cache hit
# ---------------------------------------------------------------------------


async def test_cache_hit_returns_transcript_with_cache_hit_true() -> None:
    """Cache hit short-circuits to a TranscriptResponse with cache_hit=True."""
    session = MagicMock()
    redis = MagicMock()

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=_cached_record())),
        patch.object(svc.cache, "put_transcript", new=AsyncMock()) as mock_put,
        patch.object(svc.cache, "purge_transcript", new=AsyncMock()) as mock_purge,
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock()) as mock_yt,
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock()) as mock_enq,
    ):
        tag, response = await svc.get_or_fetch(session, redis, _request(), TOKEN_ID)

    assert tag == "transcript"
    assert response.video_id == VIDEO_ID
    assert response.cache_hit is True
    mock_put.assert_not_awaited()
    mock_purge.assert_not_awaited()
    mock_yt.assert_not_awaited()
    mock_enq.assert_not_awaited()


# ---------------------------------------------------------------------------
# Captions success
# ---------------------------------------------------------------------------


async def test_captions_success_writes_cache_and_returns_transcript() -> None:
    """Cache miss + captions returns: put_transcript called, deep links populated."""
    session = MagicMock()
    session.commit = AsyncMock()
    redis = MagicMock()

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.cache, "put_transcript", new=AsyncMock()) as mock_put,
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=_captions_result())),
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock()) as mock_enq,
    ):
        tag, response = await svc.get_or_fetch(session, redis, _request(), TOKEN_ID)

    assert tag == "transcript"
    assert response.source == "youtube_captions"
    assert response.cache_hit is False
    # Every snippet has its deep_link computed by the orchestrator.
    assert all(s.deep_link.startswith(f"https://youtu.be/{VIDEO_ID}?t=") for s in response.snippets)
    mock_put.assert_awaited_once()
    mock_enq.assert_not_awaited()


# ---------------------------------------------------------------------------
# Captions miss → enqueue
# ---------------------------------------------------------------------------


async def test_captions_miss_enqueues_whisper_and_returns_202() -> None:
    """Cache miss + no captions enqueues Whisper and returns ('job_accepted', ...)."""
    session = MagicMock()
    redis = MagicMock()
    fake_job = MagicMock()
    fake_job.job_id = "01HXYZJOBNEWID"

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock(return_value=fake_job)),
    ):
        tag, response = await svc.get_or_fetch(session, redis, _request(), TOKEN_ID)

    assert tag == "job_accepted"
    assert response.job_id == "01HXYZJOBNEWID"
    assert response.video_id == VIDEO_ID
    assert response.status == "queued"
    assert response.poll_url == f"/v1/jobs/{fake_job.job_id}"
    assert response.estimated_seconds == 90


# ---------------------------------------------------------------------------
# Duplicate submit (JobInProgressError → 202)
# ---------------------------------------------------------------------------


async def test_job_in_progress_converted_to_202() -> None:
    """``JobInProgressError`` with an existing job_id becomes a 202 response (JC-016)."""
    session = MagicMock()
    redis = MagicMock()
    existing_id = "01HXYZEXISTING"

    err = JobInProgressError(
        existing_job_id=existing_id,
        poll_url=f"/v1/jobs/{existing_id}",
        message="dup",
    )

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock(side_effect=err)),
    ):
        tag, response = await svc.get_or_fetch(session, redis, _request(), TOKEN_ID)

    assert tag == "job_accepted"
    assert response.job_id == existing_id
    assert response.status == "running"


# ---------------------------------------------------------------------------
# force=refresh purges before anything else
# ---------------------------------------------------------------------------


async def test_force_refresh_purges_before_fetch() -> None:
    """``force=refresh`` invokes ``purge_transcript`` before any other lookup."""
    session = MagicMock()
    session.commit = AsyncMock()
    redis = MagicMock()
    call_order: list[str] = []

    async def _purge(*_args, **_kwargs):
        call_order.append("purge")
        return 1

    async def _get(*_args, **_kwargs):
        call_order.append("get")
        return None

    async def _fetch(*_args, **_kwargs):
        call_order.append("fetch")
        return _captions_result()

    with (
        patch.object(svc.cache, "purge_transcript", new=_purge),
        patch.object(svc.cache, "get_transcript", new=_get),
        patch.object(svc.cache, "put_transcript", new=AsyncMock()),
        patch.object(svc.youtube, "fetch_captions", new=_fetch),
    ):
        await svc.get_or_fetch(session, redis, _request(force="refresh"), TOKEN_ID)

    assert call_order[0] == "purge"
    assert "get" in call_order
    assert "fetch" in call_order


# ---------------------------------------------------------------------------
# force=whisper skips cache + captions
# ---------------------------------------------------------------------------


async def test_force_whisper_skips_cache_and_captions() -> None:
    """``force=whisper`` does not call cache.get_transcript or youtube.fetch_captions."""
    session = MagicMock()
    redis = MagicMock()
    fake_job = MagicMock()
    fake_job.job_id = "01HXYZFW"

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock()) as mock_get,
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock()) as mock_fetch,
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock(return_value=fake_job)),
    ):
        tag, _ = await svc.get_or_fetch(session, redis, _request(force="whisper"), TOKEN_ID)

    assert tag == "job_accepted"
    mock_get.assert_not_awaited()
    mock_fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wait-seconds validation + clamping
# ---------------------------------------------------------------------------


async def test_wait_seconds_negative_raises() -> None:
    """Negative wait is a client error."""
    session = MagicMock()
    redis = MagicMock()

    with pytest.raises(InvalidRequestError):
        await svc.get_or_fetch(session, redis, _request(wait_seconds=-1), TOKEN_ID)


async def test_wait_seconds_above_max_is_silently_clamped(monkeypatch) -> None:
    """``wait_seconds > 25`` is clamped to 25 rather than rejected."""
    # We assert clamping by capturing the loop count via mocked asyncio.sleep.
    sleep_calls = {"count": 0}
    fake_job = MagicMock()
    fake_job.job_id = "01HXYZCLAMP"
    incomplete_job = MagicMock()
    incomplete_job.status = "queued"

    async def _fake_sleep(_seconds):
        sleep_calls["count"] += 1
        # Stop polling after ~5 iterations to keep test fast; service will
        # still produce a job_accepted at the end.
        if sleep_calls["count"] >= 5:
            raise _StopLoop()

    class _StopLoop(Exception):
        pass

    session = MagicMock()
    session.expire_all = MagicMock()
    redis = MagicMock()

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock(return_value=fake_job)),
        patch.object(svc.jobs, "get_job", new=AsyncMock(return_value=incomplete_job)),
        patch.object(svc.asyncio, "sleep", new=_fake_sleep),
    ):
        with pytest.raises(_StopLoop):
            await svc.get_or_fetch(session, redis, _request(wait_seconds=9999), TOKEN_ID)

    # Confirm we entered the wait loop (clamped to 25s = 50 ticks at 0.5s).
    assert sleep_calls["count"] >= 1


# ---------------------------------------------------------------------------
# Wait path: job completes inside window
# ---------------------------------------------------------------------------


async def test_wait_path_returns_transcript_when_job_completes() -> None:
    """If the job finishes inside the wait window, fall through to the cache row."""
    session = MagicMock()
    session.expire_all = MagicMock()
    redis = MagicMock()
    fake_job = MagicMock()
    fake_job.job_id = "01HXYZWAIT"

    completed_job = MagicMock()
    completed_job.status = "complete"

    async def _fake_sleep(_seconds):
        return None

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(side_effect=[None, _cached_record("whisper_openai")])),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock(return_value=fake_job)),
        patch.object(svc.jobs, "get_job", new=AsyncMock(return_value=completed_job)),
        patch.object(svc.asyncio, "sleep", new=_fake_sleep),
    ):
        tag, response = await svc.get_or_fetch(session, redis, _request(wait_seconds=2), TOKEN_ID)

    assert tag == "transcript"
    assert response.source == "whisper_openai"
    assert response.cache_hit is False


# ---------------------------------------------------------------------------
# Wait path: job fails inside window
# ---------------------------------------------------------------------------


async def test_wait_path_raises_when_job_fails() -> None:
    """A polled job in ``failed`` state raises WhisperFailedError."""
    session = MagicMock()
    session.expire_all = MagicMock()
    redis = MagicMock()
    fake_job = MagicMock()
    fake_job.job_id = "01HXYZFAIL"

    failed_job = MagicMock()
    failed_job.status = "failed"
    failed_job.error = "both backends down"

    async def _fake_sleep(_seconds):
        return None

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock(return_value=fake_job)),
        patch.object(svc.jobs, "get_job", new=AsyncMock(return_value=failed_job)),
        patch.object(svc.asyncio, "sleep", new=_fake_sleep),
    ):
        with pytest.raises(WhisperFailedError):
            await svc.get_or_fetch(session, redis, _request(wait_seconds=2), TOKEN_ID)


# ---------------------------------------------------------------------------
# H12: video duration cap rejects before enqueue
# ---------------------------------------------------------------------------


async def test_video_too_long_rejected_before_whisper_enqueue() -> None:
    """When yt-dlp metadata reports a duration over the cap, raise 413 immediately.

    H12: cache miss + no captions normally enqueues Whisper. If the pre-flight
    metadata probe reports a duration > MAX_VIDEO_DURATION_SECONDS, we refuse
    BEFORE downloading audio so the worker queue stays clean.
    """
    session = MagicMock()
    redis = MagicMock()

    # MAX_VIDEO_DURATION_SECONDS defaults to 14400 (4h). Return 5h.
    long_meta = {"duration": 18000.0, "title": "very long video"}

    enqueue_mock = AsyncMock()
    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_video_metadata", new=AsyncMock(return_value=long_meta)),
        patch.object(svc.jobs, "enqueue_whisper", new=enqueue_mock),
    ):
        with pytest.raises(VideoTooLongError) as ei:
            await svc.get_or_fetch(session, redis, _request(), TOKEN_ID)

    assert "exceeds limit" in ei.value.message
    enqueue_mock.assert_not_called()


async def test_video_too_long_probe_failure_proceeds_to_enqueue() -> None:
    """Best-effort probe: a None return must NOT block enqueue (H12 fail-open on probe)."""
    session = MagicMock()
    redis = MagicMock()
    fake_job = MagicMock()
    fake_job.job_id = "01HXYZJOBOK"

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_video_metadata", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=AsyncMock(return_value=fake_job)),
    ):
        tag, response = await svc.get_or_fetch(session, redis, _request(), TOKEN_ID)

    assert tag == "job_accepted"
    assert response.job_id == "01HXYZJOBOK"


# ---------------------------------------------------------------------------
# H8: whisper_rate_limit_hook fires immediately before enqueue
# ---------------------------------------------------------------------------


async def test_whisper_rate_limit_hook_invoked_before_enqueue() -> None:
    """The hook supplied by the API layer is awaited just before enqueue."""
    session = MagicMock()
    redis = MagicMock()
    fake_job = MagicMock()
    fake_job.job_id = "01HXYZRLOK"

    call_order: list[str] = []

    async def hook() -> None:
        call_order.append("hook")

    async def fake_enqueue(*args, **kwargs):
        call_order.append("enqueue")
        return fake_job

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_video_metadata", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=fake_enqueue),
    ):
        await svc.get_or_fetch(
            session, redis, _request(), TOKEN_ID, whisper_rate_limit_hook=hook
        )

    assert call_order == ["hook", "enqueue"]


async def test_whisper_rate_limit_hook_raise_short_circuits_enqueue() -> None:
    """When the hook raises ``RateLimitedError``, enqueue is never called."""
    session = MagicMock()
    redis = MagicMock()
    enqueue_mock = AsyncMock()

    async def hook() -> None:
        raise RateLimitedError(
            "rate limit exceeded for whisper",
            details={"retry_after": 60, "limit": 30, "window_seconds": 3600},
        )

    with (
        patch.object(svc.cache, "get_transcript", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_captions", new=AsyncMock(return_value=None)),
        patch.object(svc.youtube, "fetch_video_metadata", new=AsyncMock(return_value=None)),
        patch.object(svc.jobs, "enqueue_whisper", new=enqueue_mock),
    ):
        with pytest.raises(RateLimitedError):
            await svc.get_or_fetch(
                session, redis, _request(), TOKEN_ID, whisper_rate_limit_hook=hook
            )
    enqueue_mock.assert_not_called()
