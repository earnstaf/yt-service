"""Unit tests for :mod:`app.jobs` generic enqueue + diarization helper (P2 A0-2).

Mocks RQ and uses ``AsyncMock`` sessions so these tests are hermetic — they do
NOT need a real Postgres or Redis. Integration tests covering the
"actually-talks-to-postgres" behavior live in ``tests/integration/test_jobs.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import jobs as jobs_mod
from app.exceptions import JobInProgressError


def _fake_redis() -> AsyncMock:
    """Async Redis mock supporting set / eval / delete."""
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)  # NX SET succeeds
    redis.delete = AsyncMock(return_value=1)
    redis.eval = AsyncMock(return_value=1)
    return redis


def _fake_session() -> AsyncMock:
    """Async SQLAlchemy session mock with the minimum surface jobs.py uses."""
    session = AsyncMock()
    session.add = MagicMock()  # add is sync on AsyncSession
    return session


@pytest.mark.asyncio
async def test_enqueue_diarization_uses_enrichment_queue_and_task() -> None:
    """``enqueue_diarization`` must route through the enrichment registry entry."""
    session = _fake_session()
    redis = _fake_redis()

    # Patch _find_in_progress_job (no row exists) so lock acquisition succeeds.
    with (
        patch.object(jobs_mod, "_find_in_progress_job", new=AsyncMock(return_value=None)),
        patch.object(jobs_mod, "_get_rq_queue") as mock_get_queue,
    ):
        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue

        job = await jobs_mod.enqueue_diarization(
            session,
            redis,
            video_id="OMhKgQmeMhI",
            token_id="tok_test",
            language="en",
        )

    # Queue resolution targets "enrichment"
    mock_get_queue.assert_called_once_with("enrichment")
    # Task path is run_diarization_job
    args, _ = mock_queue.enqueue.call_args
    assert args[0] == "app.worker.run_diarization_job"
    assert args[1] == job.job_id

    # Job ORM instance carries enrichment metadata
    assert job.job_type == "enrichment"
    assert job.video_id == "OMhKgQmeMhI"
    assert job.status == "queued"
    assert job.token_id == "tok_test"


@pytest.mark.asyncio
async def test_enqueue_diarization_uses_diarize_lock_key() -> None:
    """Lock key for enrichment must be ``lock:diarize:<vid>`` not ``lock:whisper:``."""
    session = _fake_session()
    redis = _fake_redis()

    with (
        patch.object(jobs_mod, "_find_in_progress_job", new=AsyncMock(return_value=None)),
        patch.object(jobs_mod, "_get_rq_queue", return_value=MagicMock()),
    ):
        await jobs_mod.enqueue_diarization(
            session,
            redis,
            video_id="OMhKgQmeMhI",
            token_id="tok_test",
        )

    # Inspect the SET call to verify the lock key
    set_call = redis.set.call_args
    args, kwargs = set_call
    key = args[0] if args else kwargs.get("name")
    assert key == "lock:diarize:OMhKgQmeMhI"
    # SETNX semantics
    assert kwargs.get("nx") is True


@pytest.mark.asyncio
async def test_enqueue_whisper_still_uses_whisper_lock() -> None:
    """``enqueue_whisper`` (the back-compat wrapper) must still target whisper."""
    session = _fake_session()
    redis = _fake_redis()

    payload: dict[str, Any] = {
        "video_id": "OMhKgQmeMhI",
        "language": "en",
        "force_whisper": False,
        "include": [],
        "callback_url": None,
    }

    with (
        patch.object(jobs_mod, "_find_in_progress_job", new=AsyncMock(return_value=None)),
        patch.object(jobs_mod, "_get_rq_queue") as mock_get_queue,
    ):
        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue
        await jobs_mod.enqueue_whisper(session, redis, payload, token_id="tok_test")  # type: ignore[arg-type]

    mock_get_queue.assert_called_once_with("whisper")
    set_call = redis.set.call_args
    key = set_call.args[0] if set_call.args else set_call.kwargs.get("name")
    assert key == "lock:whisper:OMhKgQmeMhI"


@pytest.mark.asyncio
async def test_enqueue_job_unknown_type_raises() -> None:
    """Unregistered job types fail fast with a clear ValueError."""
    session = _fake_session()
    redis = _fake_redis()

    with pytest.raises(ValueError, match="unknown job_type"):
        await jobs_mod.enqueue_job(
            session,
            redis,
            video_id="abc",
            job_type="not_a_real_type",
            payload={},
            token_id="tok",
        )


@pytest.mark.asyncio
async def test_enqueue_diarization_lock_conflict_returns_existing_job() -> None:
    """If a diarization job is already in flight, raise with its job_id."""
    session = _fake_session()
    redis = _fake_redis()
    redis.set = AsyncMock(return_value=None)  # SETNX fails

    existing_job = MagicMock()
    existing_job.job_id = "01HXEXISTING_DIAR_JOB"

    with patch.object(
        jobs_mod, "_find_in_progress_job", new=AsyncMock(return_value=existing_job)
    ) as mock_find:
        with pytest.raises(JobInProgressError) as exc_info:
            await jobs_mod.enqueue_diarization(
                session, redis, video_id="OMhKgQmeMhI", token_id="tok"
            )

    assert exc_info.value.existing_job_id == "01HXEXISTING_DIAR_JOB"
    # CRITICAL: lookup must filter by job_type="enrichment", not the default
    # "whisper", or a stale whisper job would shadow a diarization request.
    call_args = mock_find.await_args_list[0]
    assert call_args.kwargs.get("job_type") == "enrichment"


@pytest.mark.asyncio
async def test_find_in_progress_job_type_parameter_filters_correctly() -> None:
    """``_find_in_progress_job`` must respect the ``job_type`` parameter."""
    # We can't easily mock the SQLAlchemy select chain, so we verify the
    # statement is constructed with the right WHERE clause via session.execute
    # call inspection.
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=result)

    await jobs_mod._find_in_progress_job(session, "vid", job_type="enrichment")

    # Just confirm execute was called once with a Select-shaped argument.
    assert session.execute.call_count == 1
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "enrichment" in compiled
    assert "whisper" not in compiled
