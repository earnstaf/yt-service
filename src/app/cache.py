"""Transcript cache layer.

Async read/write helpers for the ``transcripts`` table. The orchestrator
(``app.transcript_service``, P1 D3) calls these to populate and serve cached
transcripts. None of these helpers commit — the caller (FastAPI route handler
or worker) owns the transaction boundary so cache writes can be composed with
job-state updates atomically.

Semantics:

- :func:`get_transcript` only returns rows whose ``expires_at > now()``. An
  expired row is treated the same as a miss; reconciliation (purge/refresh)
  is the caller's responsibility.
- :func:`put_transcript` upserts on the primary key ``(video_id, language)``.
  ``expires_at`` is computed Python-side using ``datetime.now(UTC) +
  timedelta(days=ttl)`` so unit tests and integration tests can pin it
  deterministically.
- :func:`purge_transcript` is a hard delete across ALL languages — used by
  the admin ``DELETE /v1/cache/{video_id}`` route.
- :func:`stats` powers the admin ``GET /v1/cache/stats`` route.

All snippet JSON round-trips through the canonical ``Snippet`` dataclass so
downstream consumers always get the same shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain import Snippet, TranscriptRecord, TranscriptSource
from app.metrics import CACHE_HITS, CACHE_MISSES
from app.models import Transcript

_CACHE_TABLE_LABEL = "transcripts"


def _snippet_to_dict(snippet: Snippet) -> dict[str, Any]:
    """Serialize a ``Snippet`` to the JSONB-safe dict shape stored in Postgres."""
    return {
        "start": snippet.start,
        "duration": snippet.duration,
        "text": snippet.text,
        "speaker": snippet.speaker,
        "deep_link": snippet.deep_link,
    }


def _dict_to_snippet(raw: dict[str, Any]) -> Snippet:
    """Reconstruct a ``Snippet`` from a stored JSONB dict.

    Defensive against legacy rows that may have been written before some
    optional fields existed: ``speaker`` defaults to ``None`` and
    ``deep_link`` to empty string per the dataclass defaults.
    """
    return Snippet(
        start=float(raw["start"]),
        duration=float(raw["duration"]),
        text=raw["text"],
        speaker=raw.get("speaker"),
        deep_link=raw.get("deep_link", ""),
    )


def _row_to_record(row: Transcript) -> TranscriptRecord:
    """Map an ORM ``Transcript`` row into the canonical ``TranscriptRecord``."""
    snippets = [_dict_to_snippet(s) for s in (row.snippets_jsonb or [])]
    return TranscriptRecord(
        video_id=row.video_id,
        language=row.language,
        source=row.source,  # type: ignore[arg-type]
        is_generated=row.is_generated,
        duration_seconds=row.duration_seconds,
        snippet_count=len(snippets),
        cached_at=row.fetched_at,
        snippets=snippets,
        full_text=row.full_text,
        chapters=None,  # P2 will populate from row.chapters_jsonb
        has_diarization=row.has_diarization,
    )


async def get_transcript(
    session: AsyncSession,
    video_id: str,
    language: str,
) -> TranscriptRecord | None:
    """Return the cached transcript for ``(video_id, language)`` or ``None``.

    Returns ``None`` if no row exists OR if the row's ``expires_at`` has
    already passed. Increments the ``yt_cache_hits_total`` / ``yt_cache_misses_total``
    counters labeled ``transcripts``.
    """
    stmt = (
        select(Transcript)
        .where(Transcript.video_id == video_id)
        .where(Transcript.language == language)
        .where(Transcript.expires_at > func.now())
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        CACHE_MISSES.labels(_CACHE_TABLE_LABEL).inc()
        return None
    CACHE_HITS.labels(_CACHE_TABLE_LABEL).inc()
    return _row_to_record(row)


async def put_transcript(
    session: AsyncSession,
    record: TranscriptRecord,
    ttl_days: int | None = None,
) -> None:
    """Upsert a transcript record. Does not commit; caller owns the transaction.

    ``expires_at`` is computed Python-side as ``now(UTC) + ttl_days`` so tests
    can pin it deterministically. When ``ttl_days`` is ``None``, falls back to
    ``settings.cache_ttl_days``.

    The ``record.cached_at`` field is honored as ``fetched_at`` on insert so
    the same value persists across read/write round-trips. On conflict, every
    payload column is refreshed and ``fetched_at``/``expires_at`` are reset to
    the new write time (this is a re-fetch, not a no-op).
    """
    if ttl_days is None:
        ttl_days = get_settings().cache_ttl_days

    now = datetime.now(timezone.utc)
    fetched_at = record.cached_at or now
    expires_at = now + timedelta(days=ttl_days)

    snippets_payload = [_snippet_to_dict(s) for s in record.snippets]

    values: dict[str, Any] = {
        "video_id": record.video_id,
        "language": record.language,
        "source": record.source,
        "is_generated": record.is_generated,
        "duration_seconds": record.duration_seconds,
        "snippets_jsonb": snippets_payload,
        "full_text": record.full_text,
        "chapters_jsonb": None,
        "has_diarization": record.has_diarization,
        "fetched_at": fetched_at,
        "expires_at": expires_at,
    }

    stmt = pg_insert(Transcript).values(**values)
    update_cols = {
        "source": stmt.excluded.source,
        "is_generated": stmt.excluded.is_generated,
        "duration_seconds": stmt.excluded.duration_seconds,
        "snippets_jsonb": stmt.excluded.snippets_jsonb,
        "full_text": stmt.excluded.full_text,
        "chapters_jsonb": stmt.excluded.chapters_jsonb,
        "has_diarization": stmt.excluded.has_diarization,
        "fetched_at": stmt.excluded.fetched_at,
        "expires_at": stmt.excluded.expires_at,
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=[Transcript.video_id, Transcript.language],
        set_=update_cols,
    )

    await session.execute(stmt)


async def purge_transcript(session: AsyncSession, video_id: str) -> int:
    """Delete every cached language row for ``video_id``. Returns the rowcount.

    Does not commit; the caller owns the transaction.
    """
    stmt = delete(Transcript).where(Transcript.video_id == video_id)
    result = await session.execute(stmt)
    # ``rowcount`` on asyncpg is reliable for DELETEs.
    return int(result.rowcount or 0)


async def stats(session: AsyncSession) -> dict[str, Any]:
    """Return a ``CacheStatsResponse``-shaped dict describing the cache.

    Shape matches ``app.schemas.CacheStatsResponse``::

        {
            "total_rows": int,
            "by_source": {source: count, ...},
            "oldest_cached_at": datetime | None,
            "newest_cached_at": datetime | None,
        }
    """
    total_stmt = select(
        func.count(Transcript.video_id),
        func.min(Transcript.fetched_at),
        func.max(Transcript.fetched_at),
    )
    total_row = (await session.execute(total_stmt)).one()
    total_rows = int(total_row[0] or 0)
    oldest_cached_at: datetime | None = total_row[1]
    newest_cached_at: datetime | None = total_row[2]

    by_source_stmt = select(Transcript.source, func.count(Transcript.video_id)).group_by(
        Transcript.source
    )
    by_source_rows = (await session.execute(by_source_stmt)).all()
    by_source: dict[str, int] = {src: int(count) for src, count in by_source_rows}

    return {
        "total_rows": total_rows,
        "by_source": by_source,
        "oldest_cached_at": oldest_cached_at,
        "newest_cached_at": newest_cached_at,
    }


__all__ = [
    "get_transcript",
    "put_transcript",
    "purge_transcript",
    "stats",
    "TranscriptSource",
]
