"""Unit tests for ``app.worker``.

Mocks the entire dependency tree: session factory, audio download, Whisper
transcription, cache writes, jobs state, and webhook enqueue. We assert the
control-flow shape, not real persistence — :mod:`tests.integration.test_jobs`
covers the DB side.

These tests drive the SYNCHRONOUS ``run_whisper_job`` entrypoint because
that's what RQ workers actually call. The async pipeline is wrapped with
``asyncio.run`` inside.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import worker as worker_mod
from app.domain import Snippet, WhisperResult
from app.exceptions import NoAudioStreamError, WhisperFailedError


VIDEO_ID = "OMhKgQmeMhI"
JOB_ID = "01HXYZJOB1234567890"
TOKEN_ID = "tok_unit"


def _job_row(callback_url: str | None = None) -> MagicMock:
    """Mock ``Job`` row exposing the attributes the worker reads."""
    job = MagicMock()
    job.job_id = JOB_ID
    job.video_id = VIDEO_ID
    job.payload_jsonb = {
        "video_id": VIDEO_ID,
        "language": "en",
        "force_whisper": True,
        "include": [],
        "callback_url": callback_url,
    }
    job.callback_url = callback_url
    job.token_id = TOKEN_ID
    return job


def _whisper_result() -> WhisperResult:
    """Build a representative ``WhisperResult`` for happy-path tests."""
    return WhisperResult(
        video_id=VIDEO_ID,
        source="whisper_openai",
        language="en",
        snippets=[
            Snippet(start=0.0, duration=2.0, text="hello"),
            Snippet(start=2.0, duration=2.0, text="world"),
        ],
        duration_seconds=4.0,
        full_text="hello world",
    )


@asynccontextmanager
async def _fake_session_ctx():
    """Async context manager yielding an async-compatible mock session.

    AsyncMock makes ``await session.commit()`` / ``rollback`` / etc. work without
    each test having to register every method individually.
    """
    yield AsyncMock()


def _patch_factory(audio_path: Path) -> dict:
    """Return a dict of ``patch.object`` targets sharing a session mock.

    Each test enters these patches and asserts on the resulting mocks.
    """
    fake_factory = MagicMock()
    fake_factory.return_value = _fake_session_ctx()
    return {
        "session_factory": patch.object(worker_mod, "get_session_factory", return_value=fake_factory),
        "redis": patch.object(worker_mod, "get_redis_client", return_value=MagicMock()),
        "download": patch.object(
            worker_mod,
            "download_audio",
            new=AsyncMock(return_value=audio_path),
        ),
        "transcribe": patch.object(
            worker_mod,
            "whisper_transcribe",
            new=AsyncMock(return_value=_whisper_result()),
        ),
        "cleanup": patch.object(worker_mod, "cleanup_audio"),
    }


def test_run_whisper_job_happy_path(tmp_path) -> None:
    """Happy path: mark_running -> transcribe -> cache.put -> mark_complete -> webhook."""
    audio_path = tmp_path / "audio.m4a"
    audio_path.touch()
    job = _job_row(callback_url="https://hooks.example.com/cb")

    with (
        patch.object(worker_mod.jobs, "get_job", new=AsyncMock(return_value=job)),
        patch.object(worker_mod.jobs, "mark_running", new=AsyncMock()) as mock_running,
        patch.object(worker_mod.jobs, "mark_complete", new=AsyncMock()) as mock_complete,
        patch.object(worker_mod.jobs, "mark_failed", new=AsyncMock()) as mock_failed,
        patch.object(worker_mod.jobs, "release_lock", new=AsyncMock()) as mock_release,
        patch.object(worker_mod.cache, "put_transcript", new=AsyncMock()) as mock_put,
        patch.object(worker_mod, "_enqueue_completion_webhook", new=AsyncMock()) as mock_webhook,
        patch.object(worker_mod, "download_audio", new=AsyncMock(return_value=audio_path)),
        patch.object(
            worker_mod,
            "whisper_transcribe",
            new=AsyncMock(return_value=_whisper_result()),
        ),
        patch.object(worker_mod, "cleanup_audio") as mock_cleanup,
        patch.object(worker_mod, "get_redis_client", return_value=MagicMock()),
        patch.object(worker_mod, "get_session_factory") as mock_factory,
    ):
        mock_factory.return_value.return_value = _fake_session_ctx()

        worker_mod.run_whisper_job(JOB_ID)

    mock_running.assert_awaited_once()
    mock_put.assert_awaited_once()
    mock_complete.assert_awaited_once()
    mock_failed.assert_not_awaited()
    mock_release.assert_awaited_once()
    mock_cleanup.assert_called_once_with(audio_path)
    mock_webhook.assert_awaited_once()
    # Webhook payload carries job_id + video_id + source.
    kwargs = mock_webhook.call_args.kwargs
    assert kwargs["job_id"] == JOB_ID
    assert kwargs["video_id"] == VIDEO_ID
    assert kwargs["source"] == "whisper_openai"
    assert kwargs["callback_url"] == "https://hooks.example.com/cb"


def test_run_whisper_job_no_callback_does_not_fire_webhook(tmp_path) -> None:
    """When callback_url is None, the webhook enqueue is skipped."""
    audio_path = tmp_path / "audio.m4a"
    audio_path.touch()
    job = _job_row(callback_url=None)

    with (
        patch.object(worker_mod.jobs, "get_job", new=AsyncMock(return_value=job)),
        patch.object(worker_mod.jobs, "mark_running", new=AsyncMock()),
        patch.object(worker_mod.jobs, "mark_complete", new=AsyncMock()),
        patch.object(worker_mod.jobs, "release_lock", new=AsyncMock()),
        patch.object(worker_mod.cache, "put_transcript", new=AsyncMock()),
        patch.object(worker_mod, "_enqueue_completion_webhook", new=AsyncMock()) as mock_webhook,
        patch.object(worker_mod, "download_audio", new=AsyncMock(return_value=audio_path)),
        patch.object(
            worker_mod,
            "whisper_transcribe",
            new=AsyncMock(return_value=_whisper_result()),
        ),
        patch.object(worker_mod, "cleanup_audio"),
        patch.object(worker_mod, "get_redis_client", return_value=MagicMock()),
        patch.object(worker_mod, "get_session_factory") as mock_factory,
    ):
        mock_factory.return_value.return_value = _fake_session_ctx()
        worker_mod.run_whisper_job(JOB_ID)

    mock_webhook.assert_not_awaited()


def test_run_whisper_job_whisper_failure_marks_failed_no_cache_no_webhook(tmp_path) -> None:
    """WhisperFailedError: mark_failed runs, cache.put_transcript does NOT, lock released, audio cleaned."""
    audio_path = tmp_path / "audio.m4a"
    audio_path.touch()
    job = _job_row(callback_url="https://hooks.example.com/cb")

    with (
        patch.object(worker_mod.jobs, "get_job", new=AsyncMock(return_value=job)),
        patch.object(worker_mod.jobs, "mark_running", new=AsyncMock()),
        patch.object(worker_mod.jobs, "mark_complete", new=AsyncMock()) as mock_complete,
        patch.object(worker_mod.jobs, "mark_failed", new=AsyncMock()) as mock_failed,
        patch.object(worker_mod.jobs, "release_lock", new=AsyncMock()) as mock_release,
        patch.object(worker_mod.cache, "put_transcript", new=AsyncMock()) as mock_put,
        patch.object(worker_mod, "_enqueue_completion_webhook", new=AsyncMock()) as mock_webhook,
        patch.object(worker_mod, "download_audio", new=AsyncMock(return_value=audio_path)),
        patch.object(
            worker_mod,
            "whisper_transcribe",
            new=AsyncMock(side_effect=WhisperFailedError("both backends failed")),
        ),
        patch.object(worker_mod, "cleanup_audio") as mock_cleanup,
        patch.object(worker_mod, "get_redis_client", return_value=MagicMock()),
        patch.object(worker_mod, "get_session_factory") as mock_factory,
    ):
        mock_factory.return_value.return_value = _fake_session_ctx()
        worker_mod.run_whisper_job(JOB_ID)

    mock_failed.assert_awaited_once()
    args, _ = mock_failed.call_args
    # second positional arg is the error message
    assert "both backends failed" in args[2]
    mock_complete.assert_not_awaited()
    mock_put.assert_not_awaited()
    mock_webhook.assert_not_awaited()
    mock_release.assert_awaited_once()
    mock_cleanup.assert_called_once_with(audio_path)


def test_run_whisper_job_no_audio_marks_failed(tmp_path) -> None:
    """NoAudioStreamError triggers mark_failed with the exception message."""
    job = _job_row(callback_url=None)

    with (
        patch.object(worker_mod.jobs, "get_job", new=AsyncMock(return_value=job)),
        patch.object(worker_mod.jobs, "mark_running", new=AsyncMock()),
        patch.object(worker_mod.jobs, "mark_failed", new=AsyncMock()) as mock_failed,
        patch.object(worker_mod.jobs, "release_lock", new=AsyncMock()) as mock_release,
        patch.object(
            worker_mod,
            "download_audio",
            new=AsyncMock(side_effect=NoAudioStreamError("yt-dlp returned no info")),
        ),
        patch.object(worker_mod, "cleanup_audio") as mock_cleanup,
        patch.object(worker_mod, "get_redis_client", return_value=MagicMock()),
        patch.object(worker_mod, "get_session_factory") as mock_factory,
    ):
        mock_factory.return_value.return_value = _fake_session_ctx()
        worker_mod.run_whisper_job(JOB_ID)

    mock_failed.assert_awaited_once()
    mock_release.assert_awaited_once()
    # Audio cleanup was skipped because download itself raised before path was set.
    mock_cleanup.assert_not_called()


def test_run_whisper_job_missing_row_no_crash() -> None:
    """If the job row vanishes between enqueue and pickup, the task exits cleanly."""
    with (
        patch.object(worker_mod.jobs, "get_job", new=AsyncMock(return_value=None)),
        patch.object(worker_mod.jobs, "mark_running", new=AsyncMock()) as mock_running,
        patch.object(worker_mod.jobs, "release_lock", new=AsyncMock()) as mock_release,
        patch.object(worker_mod, "get_redis_client", return_value=MagicMock()),
        patch.object(worker_mod, "get_session_factory") as mock_factory,
    ):
        mock_factory.return_value.return_value = _fake_session_ctx()
        worker_mod.run_whisper_job("non-existent")

    mock_running.assert_not_awaited()
    # Lock release is keyed on video_id which is unknown when row is missing,
    # so it should not be called.
    mock_release.assert_not_awaited()
