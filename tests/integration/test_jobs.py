"""Integration tests for ``app.jobs``.

Exercises the job-ledger helpers against a real Postgres database and a
fakeredis client. Marked ``integration`` (P-14) so the default ``pytest``
run skips them.

The RQ enqueue itself is patched out — we only care that the ``Job`` row is
written, the lock is held, and the conflict path raises
``JobInProgressError`` with the expected id and poll URL.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import fakeredis.aioredis
import pytest

pytestmark = pytest.mark.integration

from sqlalchemy import delete  # noqa: E402  -- after pytestmark by design
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app import jobs  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.domain import JobPayload  # noqa: E402
from app.exceptions import JobInProgressError  # noqa: E402
from app.models import Job, Token  # noqa: E402


VIDEO_ID = "OMhKgQmeMhI"
TOKEN_ID = "tok_test_jobs"


async def _clear(session: AsyncSession) -> None:
    """Wipe ``jobs`` and ``tokens`` so each test starts empty."""
    await session.execute(delete(Job))
    await session.execute(delete(Token))
    await session.commit()


@pytest.fixture
async def session() -> AsyncSession:
    """Yield an async session with the relevant tables wiped on entry and exit."""
    factory = get_session_factory()
    async with factory() as s:
        await _clear(s)
        # Seed a token row so foreign-key-style references stay realistic
        # even though jobs.token_id is not a true FK in P1.
        s.add(
            Token(
                id=TOKEN_ID,
                name="test-token",
                token_hash="argon2-hash-placeholder",
                scopes=["read"],
                webhook_secret="test-webhook-secret",
            )
        )
        await s.commit()
        try:
            yield s
        finally:
            await _clear(s)


@pytest.fixture
async def redis_client():
    """Yield a fakeredis async client suitable for SETNX/DEL operations."""
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def _payload(callback: str | None = None) -> JobPayload:
    return JobPayload(
        video_id=VIDEO_ID,
        language="en",
        force_whisper=False,
        include=[],
        callback_url=callback,
    )


async def test_enqueue_whisper_happy_path(
    session: AsyncSession,
    redis_client,
) -> None:
    """Successful enqueue: row written, lock held, RQ queue.enqueue called."""
    fake_queue = MagicMock()
    with patch.object(jobs, "Queue", return_value=fake_queue):
        job = await jobs.enqueue_whisper(session, redis_client, _payload(), TOKEN_ID)

    assert job.job_id
    assert job.video_id == VIDEO_ID
    assert job.status == "queued"
    assert job.token_id == TOKEN_ID
    assert job.job_type == "whisper"
    # RQ was asked to enqueue our task.
    fake_queue.enqueue.assert_called_once()
    args = fake_queue.enqueue.call_args[0]
    assert args[0] == "app.worker.run_whisper_job"
    assert args[1] == job.job_id
    # Lock is now held.
    lock_value = await redis_client.get(f"lock:whisper:{VIDEO_ID}")
    assert lock_value is not None


async def test_enqueue_whisper_lock_conflict_raises_with_existing_id(
    session: AsyncSession,
    redis_client,
) -> None:
    """Second enqueue for same video while first is queued raises JobInProgressError."""
    fake_queue = MagicMock()
    with patch.object(jobs, "Queue", return_value=fake_queue):
        first = await jobs.enqueue_whisper(session, redis_client, _payload(), TOKEN_ID)

    with patch.object(jobs, "Queue", return_value=fake_queue):
        with pytest.raises(JobInProgressError) as excinfo:
            await jobs.enqueue_whisper(session, redis_client, _payload(), TOKEN_ID)

    assert excinfo.value.existing_job_id == first.job_id
    assert excinfo.value.poll_url == f"/v1/jobs/{first.job_id}"


async def test_get_job_returns_none_for_missing(session: AsyncSession) -> None:
    """``get_job`` returns ``None`` for an unknown id."""
    assert await jobs.get_job(session, "no-such-id") is None


async def test_mark_running_complete_failed_roundtrip(
    session: AsyncSession,
    redis_client,
) -> None:
    """State transitions through the mark_* helpers persist correctly."""
    fake_queue = MagicMock()
    with patch.object(jobs, "Queue", return_value=fake_queue):
        job = await jobs.enqueue_whisper(session, redis_client, _payload(), TOKEN_ID)

    await jobs.mark_running(session, job.job_id)
    fresh = await jobs.get_job(session, job.job_id)
    assert fresh is not None
    assert fresh.status == "running"
    assert fresh.started_at is not None

    await jobs.mark_complete(session, job.job_id)
    fresh = await jobs.get_job(session, job.job_id)
    assert fresh is not None
    assert fresh.status == "complete"
    assert fresh.finished_at is not None
    assert fresh.error is None

    await jobs.mark_failed(session, job.job_id, "downstream blew up")
    fresh = await jobs.get_job(session, job.job_id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.error == "downstream blew up"


async def test_release_lock_clears_lock(redis_client) -> None:
    """``release_lock`` with a matching owner deletes the key; idempotent on missing."""
    owner = "job_owner_1"
    await redis_client.set(f"lock:whisper:{VIDEO_ID}", owner.encode("utf-8"))
    await jobs.release_lock(redis_client, VIDEO_ID, "whisper", value=owner)
    assert await redis_client.get(f"lock:whisper:{VIDEO_ID}") is None
    # Calling again on missing key must not raise; result is 0 (key gone).
    await jobs.release_lock(redis_client, VIDEO_ID, "whisper", value=owner)


async def test_acquire_lock_returns_false_when_already_held(redis_client) -> None:
    """SETNX semantics: first acquire wins, second returns False."""
    first = await jobs.acquire_lock(
        redis_client, VIDEO_ID, "whisper", value="owner_a", ttl_seconds=60
    )
    second = await jobs.acquire_lock(
        redis_client, VIDEO_ID, "whisper", value="owner_b", ttl_seconds=60
    )
    assert first is True
    assert second is False


async def test_release_lock_refuses_non_owner(redis_client) -> None:
    """H9: a release attempt from a non-owner must NOT delete the lock."""
    holder = "real_owner"
    intruder = "stranger"
    await jobs.acquire_lock(
        redis_client, VIDEO_ID, "whisper", value=holder, ttl_seconds=60
    )
    result = await jobs.release_lock(redis_client, VIDEO_ID, "whisper", value=intruder)
    assert result == 0
    # Lock still held by the real owner.
    again = await jobs.acquire_lock(
        redis_client, VIDEO_ID, "whisper", value="someone_else", ttl_seconds=60
    )
    assert again is False
    # Owner can release.
    released = await jobs.release_lock(
        redis_client, VIDEO_ID, "whisper", value=holder
    )
    assert released == 1
