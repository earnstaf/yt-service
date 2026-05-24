"""Job ledger and Redis-lock helpers for the Whisper pipeline.

This module is the persistence-layer companion to :mod:`app.worker`. The
orchestrator (:mod:`app.transcript_service`) calls :func:`enqueue_whisper`
to spawn background transcription work; the RQ worker calls the mark_*
helpers to flip job rows through their lifecycle.

Locks live in Redis under the key ``lock:whisper:{video_id}``. Per spec
§7.14, a duplicate enqueue for an in-flight ``video_id`` returns the existing
``job_id`` via :class:`JobInProgressError` rather than a 409 — the
orchestrator (JC-016) converts that into a 202 response.

All public functions are async and accept a SQLAlchemy ``AsyncSession``
managed by the caller (the FastAPI route handler or the worker entrypoint).
None of these functions commit, EXCEPT :func:`enqueue_whisper` which has to
commit the new job row before the RQ task is enqueued so the worker can see
it once it picks up the job. The mark_* helpers also commit, since they are
called from a worker context where each call corresponds to a single
state-transition write.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from rq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app.domain import JobPayload
from app.exceptions import JobInProgressError
from app.logging import get_logger
from app.models import Job

_logger = get_logger("jobs")

# Per-job-type registry (P2 A0-2). Each entry pins the RQ queue, the
# fully-qualified task path the worker exposes, the Redis lock op segment,
# and a default ``estimated_seconds`` for the 202 response shape. Adding
# new job types (e.g., topics in P4) only requires extending this dict.
_JOB_REGISTRY: dict[str, dict[str, Any]] = {
    "whisper": {
        "queue": "whisper",
        "task": "app.worker.run_whisper_job",
        "lock_op": "whisper",
        "estimated_seconds": 90,
    },
    "enrichment": {
        "queue": "enrichment",
        "task": "app.worker.run_diarization_job",
        "lock_op": "diarize",
        "estimated_seconds": 60,
    },
}

# Backwards-compatible aliases used by older P1 call sites / tests.
_WHISPER_QUEUE = _JOB_REGISTRY["whisper"]["queue"]
_WHISPER_TASK_PATH = _JOB_REGISTRY["whisper"]["task"]

# Lock TTL for Whisper operations. The spec says 1h is the upper bound for
# reasonable Whisper runs; if a worker dies without releasing, the lock
# expires and a re-submit can proceed.
_DEFAULT_LOCK_TTL_SECONDS = 3600

# Lua release script: only DEL the key if the stored value matches the
# claimed owner. Prevents one job from clobbering another's lock when their
# TTLs and ULIDs interleave. See H9 in the code-review notes.
_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def poll_url_for(job_id: str) -> str:
    """Return the canonical job-poll URL for ``job_id`` (spec §5.5 202 shape)."""
    return f"/v1/jobs/{job_id}"


def _lock_key(video_id: str, op: str) -> str:
    """Compose the Redis lock key for a given video/operation pair."""
    return f"lock:{op}:{video_id}"


def _lock_value(value: str) -> bytes:
    """Encode the lock owner value as bytes so Redis SET stores it verbatim."""
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


async def acquire_lock(
    redis_client: Any,
    video_id: str,
    op: str,
    value: str,
    ttl_seconds: int = _DEFAULT_LOCK_TTL_SECONDS,
) -> bool:
    """Acquire the per-``(video_id, op)`` Redis lock owned by ``value``.

    Uses ``SET NX EX`` so the lock both takes effect atomically and expires
    after ``ttl_seconds`` even if the holder crashes. The stored value is
    the caller-supplied ``value`` (typically the job_id), so the release
    path can verify ownership before deleting (see :func:`release_lock`).
    Returns ``True`` when we got the lock, ``False`` when someone else holds it.
    """
    key = _lock_key(video_id, op)
    result = await redis_client.set(key, _lock_value(value), nx=True, ex=ttl_seconds)
    return bool(result)


async def release_lock(
    redis_client: Any,
    video_id: str,
    op: str = "whisper",
    value: str | None = None,
) -> int:
    """Release the per-``(video_id, op)`` Redis lock owned by ``value``.

    Uses a compare-and-delete Lua script so the holder identified by
    ``value`` is the only caller allowed to delete the key. Returns 1 when
    the lock was released, 0 when the key was missing or owned by someone
    else. Callers should pass the same ``value`` they passed to
    :func:`acquire_lock`; if ``value`` is None we fall back to an
    unconditional DELETE (legacy behavior, used by cleanup paths that don't
    know the holder identity).
    """
    key = _lock_key(video_id, op)
    if value is None:
        await redis_client.delete(key)
        return 1
    result = await redis_client.eval(_LOCK_RELEASE_LUA, 1, key, _lock_value(value))
    try:
        return int(result)
    except (TypeError, ValueError):
        return 0


async def _steal_stale_lock(redis_client: Any, video_id: str, op: str = "whisper") -> None:
    """Force-delete a lock that has no backing job row (orphaned from a crash).

    Last-resort cleanup used by :func:`enqueue_whisper` when the lock-held
    fast path finds no in-progress job to attribute the lock to. Logs a
    warning because this should be rare and signals a worker that died
    between SETNX and the row INSERT.
    """
    key = _lock_key(video_id, op)
    await redis_client.delete(key)
    _logger.warning("whisper_stale_lock_stolen", video_id=video_id, op=op)


async def _find_in_progress_job(
    session: AsyncSession,
    video_id: str,
    job_type: str = "whisper",
) -> Job | None:
    """Return the most recent ``queued``/``running`` job for ``(video_id, job_type)``.

    Used to recover the existing ``job_id`` when lock acquisition fails so the
    orchestrator can return 202 with an actionable poll URL instead of a
    bare 409. P2 adds ``job_type`` as an explicit parameter so diarization
    enqueues don't accidentally claim a whisper job's identity.
    """
    stmt = (
        select(Job)
        .where(Job.video_id == video_id)
        .where(Job.job_type == job_type)
        .where(Job.status.in_(("queued", "running")))
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _new_job_id() -> str:
    """Return a fresh ULID string for use as a ``jobs.job_id`` value."""
    return str(ULID())


def _get_rq_queue(name: str = _WHISPER_QUEUE) -> Queue:
    """Build an RQ ``Queue`` bound to the synchronous Redis client.

    RQ's ``Queue`` performs synchronous I/O internally (``Queue.enqueue`` blocks
    on a Redis RPUSH); it cannot consume a ``redis.asyncio.Redis`` instance.
    Lazy import of the sync client keeps :mod:`app.jobs` importable in
    contexts that don't need to enqueue (e.g. test fixtures that only call
    the mark_* helpers).
    """
    from app.redis_client import get_sync_redis_client  # noqa: PLC0415 — lazy

    return Queue(name, connection=get_sync_redis_client())


async def enqueue_job(
    session: AsyncSession,
    redis_client: Any,
    *,
    video_id: str,
    job_type: str,
    payload: dict[str, Any],
    token_id: str,
) -> Job:
    """Generic enqueue for any registered ``job_type`` (whisper | enrichment | ...).

    Lifecycle, parameterized via ``_JOB_REGISTRY[job_type]``:

    1. Mint a fresh ``job_id`` (used as the lock owner value).
    2. Acquire ``lock:{lock_op}:{video_id}`` via SETNX/EX with the new
       ``job_id`` as the stored value.
    3. On lock conflict, look up an in-progress job of the same ``job_type``
       and raise :class:`JobInProgressError`. Stale-lock recovery same as
       P1 (50ms recheck, then steal).
    4. Insert + commit the ``jobs`` row.
    5. Enqueue the registered RQ task on the registered queue.

    Returns the newly created ``Job`` ORM instance.
    """
    if job_type not in _JOB_REGISTRY:
        raise ValueError(f"unknown job_type: {job_type!r}")
    entry = _JOB_REGISTRY[job_type]
    lock_op = entry["lock_op"]
    queue_name = entry["queue"]
    task_path = entry["task"]

    callback_url = payload.get("callback_url")

    job_id = _new_job_id()
    acquired = await acquire_lock(redis_client, video_id, lock_op, value=job_id)
    if not acquired:
        existing = await _find_in_progress_job(session, video_id, job_type=job_type)
        if existing is not None:
            raise JobInProgressError(
                existing_job_id=existing.job_id,
                poll_url=poll_url_for(existing.job_id),
                message=f"{job_type} job already running for video",
                details={"video_id": video_id, "job_type": job_type},
            )
        # Lock held but no job row visible — likely the lock holder hasn't
        # committed yet (race), OR the lock is stale from a crashed worker.
        await asyncio.sleep(0.05)
        existing = await _find_in_progress_job(session, video_id, job_type=job_type)
        if existing is not None:
            raise JobInProgressError(
                existing_job_id=existing.job_id,
                poll_url=poll_url_for(existing.job_id),
                message=f"{job_type} job already running for video",
                details={"video_id": video_id, "job_type": job_type},
            )
        # Truly orphaned lock. Steal it and retry SETNX once.
        await _steal_stale_lock(redis_client, video_id, lock_op)
        acquired = await acquire_lock(redis_client, video_id, lock_op, value=job_id)
        if not acquired:
            raise JobInProgressError(
                existing_job_id="",
                poll_url="",
                message=f"{job_type} lock held but no job row found",
                details={"video_id": video_id, "job_type": job_type},
            )

    job = Job(
        job_id=job_id,
        video_id=video_id,
        job_type=job_type,
        status="queued",
        token_id=token_id,
        callback_url=callback_url,
        payload_jsonb=dict(payload),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # If RQ enqueue fails (Redis down, queue full, serialization error), the
    # job row is already committed in ``queued`` state and the lock is held.
    # Flip the row to ``failed`` and release the lock so subsequent retries
    # don't see a phantom in-progress job and a stuck lock.
    try:
        queue = _get_rq_queue(queue_name)
        queue.enqueue(task_path, job_id)
    except Exception as exc:  # noqa: BLE001
        await mark_failed(session, job_id, f"enqueue failed: {type(exc).__name__}: {exc}")
        try:
            await release_lock(redis_client, video_id, op=lock_op, value=job_id)
        except Exception:  # noqa: BLE001
            pass
        _logger.error(
            "job_enqueue_failed",
            job_id=job_id,
            video_id=video_id,
            job_type=job_type,
            error=str(exc),
        )
        raise

    _logger.info(
        "job_enqueued",
        job_id=job_id,
        video_id=video_id,
        job_type=job_type,
        token_id=token_id,
    )
    return job


async def enqueue_whisper(
    session: AsyncSession,
    redis_client: Any,
    payload: JobPayload,
    token_id: str,
) -> Job:
    """Thin wrapper around :func:`enqueue_job` for the whisper job type.

    Kept for backward compatibility with P1 call sites and tests that
    constructed the whisper ``JobPayload`` directly. New call sites should
    use :func:`enqueue_job` or the type-specific helpers below.
    """
    return await enqueue_job(
        session,
        redis_client,
        video_id=payload["video_id"],
        job_type="whisper",
        payload=dict(payload),
        token_id=token_id,
    )


async def enqueue_diarization(
    session: AsyncSession,
    redis_client: Any,
    video_id: str,
    token_id: str,
    language: str = "en",
) -> Job:
    """Enqueue a diarization enrichment job for ``video_id``.

    The diarization worker (:func:`app.worker.run_diarization_job`) loads
    the cached transcript, downloads audio fresh, runs pyannote, and
    partial-updates the transcript row via :func:`app.cache.put_diarization`.
    """
    payload = {
        "video_id": video_id,
        "language": language,
        "force_whisper": False,
        "include": ["speakers"],
        "callback_url": None,
    }
    return await enqueue_job(
        session,
        redis_client,
        video_id=video_id,
        job_type="enrichment",
        payload=payload,
        token_id=token_id,
    )


async def get_job(session: AsyncSession, job_id: str) -> Job | None:
    """Return the ``Job`` row for ``job_id`` or ``None`` if missing."""
    stmt = select(Job).where(Job.job_id == job_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def mark_running(session: AsyncSession, job_id: str) -> None:
    """Transition ``job_id`` to ``running`` and stamp ``started_at`` to now."""
    job = await get_job(session, job_id)
    if job is None:
        return
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await session.commit()


async def mark_complete(session: AsyncSession, job_id: str) -> None:
    """Transition ``job_id`` to ``complete`` and stamp ``finished_at``."""
    job = await get_job(session, job_id)
    if job is None:
        return
    job.status = "complete"
    job.finished_at = datetime.now(timezone.utc)
    job.error = None
    await session.commit()


async def mark_failed(session: AsyncSession, job_id: str, error: str) -> None:
    """Transition ``job_id`` to ``failed``, record ``error``, stamp ``finished_at``."""
    job = await get_job(session, job_id)
    if job is None:
        return
    job.status = "failed"
    job.finished_at = datetime.now(timezone.utc)
    job.error = error
    await session.commit()


__all__ = [
    "enqueue_job",
    "enqueue_whisper",
    "enqueue_diarization",
    "get_job",
    "mark_running",
    "mark_complete",
    "mark_failed",
    "acquire_lock",
    "release_lock",
    "poll_url_for",
]
