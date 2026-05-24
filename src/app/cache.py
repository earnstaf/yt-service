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
from app.domain import Chapter, Snippet, TranscriptRecord, TranscriptSource
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


def _chapter_to_dict(chapter: Chapter) -> dict[str, Any]:
    """Serialize a ``Chapter`` to the JSONB-safe dict shape stored in Postgres."""
    return {"start": chapter.start, "end": chapter.end, "title": chapter.title}


def _dict_to_chapter(raw: dict[str, Any]) -> Chapter:
    """Reconstruct a ``Chapter`` from a stored JSONB dict."""
    return Chapter(start=float(raw["start"]), end=float(raw["end"]), title=raw["title"])


def _row_to_record(row: Transcript) -> TranscriptRecord:
    """Map an ORM ``Transcript`` row into the canonical ``TranscriptRecord``.

    P2: ``chapters_jsonb`` is now decoded. The JSONB column carries one of
    three meanings (JC-035):

    - ``None``: never tried. The orchestrator/route will compute on first
      ``include=chapters`` request.
    - ``[]``: tried, got nothing. Don't re-derive.
    - ``[{...}, ...]``: cached non-empty list.
    """
    snippets = [_dict_to_snippet(s) for s in (row.snippets_jsonb or [])]
    chapters_raw = row.chapters_jsonb
    chapters: list[Chapter] | None
    if chapters_raw is None:
        chapters = None
    else:
        chapters = [_dict_to_chapter(c) for c in chapters_raw]
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
        chapters=chapters,
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
    chapters_payload: list[dict[str, Any]] | None
    if record.chapters is None:
        chapters_payload = None
    else:
        chapters_payload = [_chapter_to_dict(c) for c in record.chapters]

    values: dict[str, Any] = {
        "video_id": record.video_id,
        "language": record.language,
        "source": record.source,
        "is_generated": record.is_generated,
        "duration_seconds": record.duration_seconds,
        "snippets_jsonb": snippets_payload,
        "full_text": record.full_text,
        "chapters_jsonb": chapters_payload,
        "has_diarization": record.has_diarization,
        "fetched_at": fetched_at,
        "expires_at": expires_at,
    }

    stmt = pg_insert(Transcript).values(**values)
    # On conflict, preserve chapters unless the new record explicitly carries
    # them. For diarization: ALWAYS reset to ``has_diarization=False`` when
    # snippets are replaced — replacing snippets invalidates any prior speaker
    # tags. The diarization worker (which re-tags in place) uses
    # ``put_diarization`` (partial UPDATE) and never goes through this path,
    # so this reset is correct and prevents the codex-flagged invariant
    # violation where snippets get replaced but ``has_diarization`` stays True.
    update_cols: dict[str, Any] = {
        "source": stmt.excluded.source,
        "is_generated": stmt.excluded.is_generated,
        "duration_seconds": stmt.excluded.duration_seconds,
        "snippets_jsonb": stmt.excluded.snippets_jsonb,
        "full_text": stmt.excluded.full_text,
        "fetched_at": stmt.excluded.fetched_at,
        "expires_at": stmt.excluded.expires_at,
        "has_diarization": stmt.excluded.has_diarization,
    }
    if record.chapters is not None:
        update_cols["chapters_jsonb"] = stmt.excluded.chapters_jsonb
    stmt = stmt.on_conflict_do_update(
        index_elements=[Transcript.video_id, Transcript.language],
        set_=update_cols,
    )

    await session.execute(stmt)


async def put_chapters(
    session: AsyncSession,
    video_id: str,
    language: str,
    chapters: list[Chapter] | None,
) -> None:
    """Partial-update ``transcripts.chapters_jsonb`` only. Does not commit.

    JC-035 semantics:

    - ``chapters=None`` stores SQL NULL (effectively a "retry on next call").
    - ``chapters=[]`` stores an empty list to mean "tried, got nothing".
    - ``chapters=[Chapter(...), ...]`` serializes the list.

    The row must already exist (the orchestrator writes the transcript first).
    Silently no-ops if the row isn't there yet.
    """
    payload: list[dict[str, Any]] | None
    if chapters is None:
        payload = None
    else:
        payload = [_chapter_to_dict(c) for c in chapters]

    stmt = (
        Transcript.__table__.update()
        .where(Transcript.video_id == video_id)
        .where(Transcript.language == language)
        .values(chapters_jsonb=payload)
    )
    await session.execute(stmt)


async def put_diarization(
    session: AsyncSession,
    video_id: str,
    language: str,
    snippets: list[Snippet],
    has_diarization: bool,
) -> int:
    """Partial-update ``snippets_jsonb`` + ``has_diarization`` only.

    Used by the diarization worker after enrichment completes. Does not
    touch ``chapters_jsonb``, ``full_text``, ``expires_at``, or ``fetched_at``.
    Returns the affected rowcount so the caller can detect cases where the
    target transcript was purged mid-flight (rowcount == 0).
    """
    snippets_payload = [_snippet_to_dict(s) for s in snippets]
    stmt = (
        Transcript.__table__.update()
        .where(Transcript.video_id == video_id)
        .where(Transcript.language == language)
        .values(snippets_jsonb=snippets_payload, has_diarization=has_diarization)
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


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
    "put_chapters",
    "put_diarization",
    "purge_transcript",
    "stats",
    "TranscriptSource",
]
