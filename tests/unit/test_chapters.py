"""Unit tests for chapter detection (P2 B1)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app import chapters as chapters_mod
from app.domain import Chapter, Snippet, TranscriptRecord
from app.llm.fallback import LLMResponse


def _record(chapters_value: list[Chapter] | None = None, full_text: str = "hello world") -> TranscriptRecord:
    return TranscriptRecord(
        video_id="OMhKgQmeMhI",
        language="en",
        source="whisper_openai",
        is_generated=False,
        duration_seconds=120.0,
        snippet_count=2,
        cached_at=datetime.now(timezone.utc),
        snippets=[
            Snippet(start=0.0, duration=2.0, text="hello"),
            Snippet(start=60.0, duration=2.0, text="world"),
        ],
        full_text=full_text,
        chapters=chapters_value,
        has_diarization=False,
    )


@pytest.mark.asyncio
async def test_get_or_compute_returns_cached_when_chapters_present() -> None:
    """If record.chapters is not None (even []), return as-is."""
    session = AsyncMock()
    cached = [Chapter(start=0, end=60, title="Intro")]
    record = _record(chapters_value=cached)

    result = await chapters_mod.get_or_compute_chapters(session, record)

    assert result == cached
    # No LLM call, no put_chapters call
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_compute_returns_empty_cached_means_dont_retry() -> None:
    """`chapters=[]` is a valid cached value meaning 'tried, got nothing'."""
    session = AsyncMock()
    record = _record(chapters_value=[])

    result = await chapters_mod.get_or_compute_chapters(session, record)

    assert result == []
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_compute_uses_yt_chapters_when_available() -> None:
    """yt-dlp returns chapters → persist + return them, skip LLM."""
    session = AsyncMock()
    record = _record(chapters_value=None)
    yt_chapters = [Chapter(start=0, end=30, title="A"), Chapter(start=30, end=60, title="B")]

    with (
        patch.object(chapters_mod, "fetch_yt_chapters", new=AsyncMock(return_value=yt_chapters)),
        patch.object(chapters_mod, "derive_chapters_from_transcript", new=AsyncMock()) as mock_llm,
        patch.object(chapters_mod, "put_chapters", new=AsyncMock()) as mock_put,
    ):
        result = await chapters_mod.get_or_compute_chapters(session, record)

    assert result == yt_chapters
    mock_llm.assert_not_called()  # LLM not consulted
    mock_put.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_compute_falls_back_to_llm() -> None:
    """No yt-dlp chapters → derive via LLM → persist + return."""
    session = AsyncMock()
    record = _record(chapters_value=None)
    derived = [Chapter(start=0, end=120, title="Whole video")]

    with (
        patch.object(chapters_mod, "fetch_yt_chapters", new=AsyncMock(return_value=None)),
        patch.object(chapters_mod, "derive_chapters_from_transcript", new=AsyncMock(return_value=derived)),
        patch.object(chapters_mod, "put_chapters", new=AsyncMock()) as mock_put,
    ):
        result = await chapters_mod.get_or_compute_chapters(session, record)

    assert result == derived
    mock_put.assert_awaited_once()


@pytest.mark.asyncio
async def test_derive_chapters_refuses_overlong_transcript() -> None:
    """Token-cap guard: huge transcripts return [] without an LLM call."""
    long_text = "x" * 300_000
    record = _record(chapters_value=None, full_text=long_text)

    with patch.object(chapters_mod, "llm_execute", new=AsyncMock()) as mock_llm:
        result = await chapters_mod.derive_chapters_from_transcript(record)

    assert result == []
    mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_derive_chapters_handles_bad_json() -> None:
    """LLM returns malformed JSON → return [] gracefully, don't 500."""
    record = _record(chapters_value=None)
    bad_response = LLMResponse(
        text="this is not JSON at all",
        tokens_in=10,
        tokens_out=10,
        cost_usd=None,  # type: ignore[arg-type]
        provider="gemini_direct",
        model="gemini-2.5-flash",
        latency_ms=100,
    )

    with patch.object(chapters_mod, "llm_execute", new=AsyncMock(return_value=bad_response)):
        result = await chapters_mod.derive_chapters_from_transcript(record)

    assert result == []


@pytest.mark.asyncio
async def test_derive_chapters_parses_valid_json() -> None:
    """LLM returns valid JSON → chapters parsed and validated."""
    record = _record(chapters_value=None)
    good_response = LLMResponse(
        text='{"chapters": [{"start": 0, "end": 60, "title": "Intro"}, {"start": 60, "end": 120, "title": "Main"}]}',
        tokens_in=10,
        tokens_out=10,
        cost_usd=None,  # type: ignore[arg-type]
        provider="gemini_direct",
        model="gemini-2.5-flash",
        latency_ms=100,
    )

    with patch.object(chapters_mod, "llm_execute", new=AsyncMock(return_value=good_response)):
        result = await chapters_mod.derive_chapters_from_transcript(record)

    assert len(result) == 2
    assert result[0].title == "Intro"
    assert result[1].start == 60.0


@pytest.mark.asyncio
async def test_fetch_yt_chapters_returns_none_when_metadata_empty() -> None:
    """yt-dlp returns no chapters field → None."""
    with patch.object(chapters_mod, "fetch_video_metadata", new=AsyncMock(return_value={"duration": 100})):
        result = await chapters_mod.fetch_yt_chapters("abc")
    assert result is None
