"""Integration tests for the transcript cache layer.

Exercises ``app.cache`` against a real Postgres database. Marked
``integration`` (P-14) so the default ``pytest`` run skips them. Run with::

    pytest -m integration

Database fixtures are owned by the broader integration suite (the same
``session`` fixture pattern used by ``test_admin_tokens.py``).

TTL expiry: instead of fighting freezegun's interaction with the asyncpg
event loop, we set ``expires_at`` directly in the past using a ``ttl_days``
override and then advance via the dedicated negative-ttl path. The
"expired row returns None" assertion is what we actually care about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

from sqlalchemy import delete  # noqa: E402  -- after pytestmark by design
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.cache import (  # noqa: E402
    get_transcript,
    purge_transcript,
    put_transcript,
    stats,
)
from app.db import get_session_factory  # noqa: E402
from app.domain import Snippet, TranscriptRecord  # noqa: E402
from app.models import Transcript  # noqa: E402

VIDEO_ID = "OMhKgQmeMhI"
SECOND_VIDEO_ID = "abcdefghijk"
THIRD_VIDEO_ID = "ZZZ12345678"


async def _clear_transcripts(session: AsyncSession) -> None:
    """Wipe the ``transcripts`` table so each test starts empty."""
    await session.execute(delete(Transcript))
    await session.commit()


@pytest.fixture
async def session() -> AsyncSession:
    """Yield an async session with the transcripts table wiped on entry and exit."""
    factory = get_session_factory()
    async with factory() as s:
        await _clear_transcripts(s)
        try:
            yield s
        finally:
            await _clear_transcripts(s)


def _make_record(
    video_id: str = VIDEO_ID,
    language: str = "en",
    source: str = "youtube_captions",
) -> TranscriptRecord:
    """Build a sample ``TranscriptRecord`` for round-trip testing."""
    snippets = [
        Snippet(start=0.0, duration=4.2, text="Welcome", speaker=None, deep_link=""),
        Snippet(start=4.2, duration=3.1, text="To the show", speaker=None, deep_link=""),
    ]
    return TranscriptRecord(
        video_id=video_id,
        language=language,
        source=source,  # type: ignore[arg-type]
        is_generated=True,
        duration_seconds=7.3,
        snippet_count=len(snippets),
        cached_at=datetime.now(timezone.utc),
        snippets=snippets,
        full_text="Welcome To the show",
    )


@pytest.mark.asyncio
async def test_put_then_get_returns_same_record(session: AsyncSession) -> None:
    record = _make_record()
    await put_transcript(session, record, ttl_days=30)
    await session.commit()

    fetched = await get_transcript(session, VIDEO_ID, "en")

    assert fetched is not None
    assert fetched.video_id == record.video_id
    assert fetched.language == record.language
    assert fetched.source == record.source
    assert fetched.full_text == record.full_text
    assert fetched.snippet_count == len(record.snippets)
    assert [s.text for s in fetched.snippets] == [s.text for s in record.snippets]
    assert [s.start for s in fetched.snippets] == [s.start for s in record.snippets]


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_video(session: AsyncSession) -> None:
    assert await get_transcript(session, "ZZZZZZZZZZZ", "en") is None


@pytest.mark.asyncio
async def test_get_returns_none_for_expired_row(session: AsyncSession) -> None:
    """Force ``expires_at`` into the past and confirm the row is treated as a miss.

    We sidestep freezegun's asyncpg quirks by overriding ``expires_at``
    directly after insert.
    """
    record = _make_record()
    await put_transcript(session, record, ttl_days=1)
    await session.commit()

    # Hack expires_at into the past.
    past = datetime.now(timezone.utc) - timedelta(days=2)
    row = await session.get(Transcript, (VIDEO_ID, "en"))
    assert row is not None
    row.expires_at = past
    await session.commit()

    assert await get_transcript(session, VIDEO_ID, "en") is None


@pytest.mark.asyncio
async def test_put_with_default_ttl_uses_settings(session: AsyncSession) -> None:
    """When ``ttl_days`` is omitted, fall back to ``settings.cache_ttl_days``."""
    record = _make_record()
    await put_transcript(session, record)
    await session.commit()

    row = await session.get(Transcript, (VIDEO_ID, "en"))
    assert row is not None
    # We can't pin the wall clock, but the expiry must be in the future.
    assert row.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_put_upserts_existing_row(session: AsyncSession) -> None:
    """Second write under same PK refreshes the payload."""
    record = _make_record()
    await put_transcript(session, record, ttl_days=30)
    await session.commit()

    updated = TranscriptRecord(
        video_id=record.video_id,
        language=record.language,
        source="whisper_openai",
        is_generated=False,
        duration_seconds=99.0,
        snippet_count=1,
        cached_at=datetime.now(timezone.utc),
        snippets=[Snippet(start=0.0, duration=1.0, text="Refreshed", deep_link="")],
        full_text="Refreshed",
    )
    await put_transcript(session, updated, ttl_days=30)
    await session.commit()

    fetched = await get_transcript(session, VIDEO_ID, "en")
    assert fetched is not None
    assert fetched.source == "whisper_openai"
    assert fetched.full_text == "Refreshed"
    assert fetched.snippet_count == 1


@pytest.mark.asyncio
async def test_purge_removes_all_languages_and_returns_count(session: AsyncSession) -> None:
    """Purge wipes EVERY language row for the video and returns the rowcount."""
    en = _make_record(language="en")
    es = _make_record(language="es")
    fr = _make_record(language="fr")
    other_video = _make_record(video_id=SECOND_VIDEO_ID, language="en")

    for rec in (en, es, fr, other_video):
        await put_transcript(session, rec, ttl_days=30)
    await session.commit()

    count = await purge_transcript(session, VIDEO_ID)
    await session.commit()

    assert count == 3
    # Other video survives.
    assert await get_transcript(session, SECOND_VIDEO_ID, "en") is not None
    assert await get_transcript(session, VIDEO_ID, "en") is None
    assert await get_transcript(session, VIDEO_ID, "es") is None
    assert await get_transcript(session, VIDEO_ID, "fr") is None


@pytest.mark.asyncio
async def test_purge_unknown_video_returns_zero(session: AsyncSession) -> None:
    count = await purge_transcript(session, "ZZZZZZZZZZZ")
    await session.commit()
    assert count == 0


@pytest.mark.asyncio
async def test_stats_aggregates_rows_by_source(session: AsyncSession) -> None:
    """Stats returns counts grouped by source plus oldest/newest fetched_at."""
    one = _make_record(video_id=VIDEO_ID, source="youtube_captions")
    two = _make_record(video_id=SECOND_VIDEO_ID, source="youtube_captions")
    three = _make_record(video_id=THIRD_VIDEO_ID, source="whisper_openai")

    for rec in (one, two, three):
        await put_transcript(session, rec, ttl_days=30)
    await session.commit()

    result = await stats(session)

    assert result["total_rows"] == 3
    assert result["by_source"] == {"youtube_captions": 2, "whisper_openai": 1}
    assert result["oldest_cached_at"] is not None
    assert result["newest_cached_at"] is not None
    assert result["oldest_cached_at"] <= result["newest_cached_at"]


@pytest.mark.asyncio
async def test_stats_on_empty_cache(session: AsyncSession) -> None:
    result = await stats(session)
    assert result["total_rows"] == 0
    assert result["by_source"] == {}
    assert result["oldest_cached_at"] is None
    assert result["newest_cached_at"] is None
