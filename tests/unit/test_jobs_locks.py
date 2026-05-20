"""Unit tests for the per-job Redis lock semantics in :mod:`app.jobs`.

Covers code-review finding H9: locks must be value-aware (compare-and-delete
on release) so a stale-lock takeover by job B cannot be cleared by a delayed
release from job A. Uses the FakeRedis stub from ``_endpoint_helpers`` which
models ``set NX``, ``delete``, and the H9 release Lua script.
"""

from __future__ import annotations

import pytest

from app import jobs

from ._endpoint_helpers import FakeRedis

VIDEO_ID = "OMhKgQmeMhI"


@pytest.mark.asyncio
async def test_acquire_lock_stores_owner_value() -> None:
    redis = FakeRedis()
    ok = await jobs.acquire_lock(redis, VIDEO_ID, "whisper", value="job_a", ttl_seconds=60)
    assert ok is True
    assert redis.locks[f"lock:whisper:{VIDEO_ID}"] == b"job_a"


@pytest.mark.asyncio
async def test_acquire_lock_returns_false_when_held_and_does_not_overwrite() -> None:
    redis = FakeRedis()
    first = await jobs.acquire_lock(redis, VIDEO_ID, "whisper", value="job_a")
    second = await jobs.acquire_lock(redis, VIDEO_ID, "whisper", value="job_b")
    assert first is True
    assert second is False
    # First owner's value is intact — second SETNX did not clobber it.
    assert redis.locks[f"lock:whisper:{VIDEO_ID}"] == b"job_a"


@pytest.mark.asyncio
async def test_release_lock_only_owner_can_release() -> None:
    """Non-owner DELETE attempt returns 0 and leaves the key in place."""
    redis = FakeRedis()
    await jobs.acquire_lock(redis, VIDEO_ID, "whisper", value="job_a")

    # Stranger tries to release — must fail.
    result = await jobs.release_lock(redis, VIDEO_ID, "whisper", value="stranger")
    assert result == 0
    assert redis.locks.get(f"lock:whisper:{VIDEO_ID}") == b"job_a"

    # Owner can release.
    result = await jobs.release_lock(redis, VIDEO_ID, "whisper", value="job_a")
    assert result == 1
    assert f"lock:whisper:{VIDEO_ID}" not in redis.locks


@pytest.mark.asyncio
async def test_release_lock_without_value_does_unconditional_delete() -> None:
    """Legacy callers that don't know the owner can still clean up via DELETE."""
    redis = FakeRedis()
    await jobs.acquire_lock(redis, VIDEO_ID, "whisper", value="job_a")
    result = await jobs.release_lock(redis, VIDEO_ID, "whisper", value=None)
    assert result == 1
    assert f"lock:whisper:{VIDEO_ID}" not in redis.locks
