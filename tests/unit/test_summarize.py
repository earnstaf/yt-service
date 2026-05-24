"""Unit tests for the summarize task (P2 D1)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain import Snippet, TranscriptRecord
from app.exceptions import InvalidRequestError, NotFoundError
from app.llm.fallback import LLMResponse
from app.tasks import summarize as sum_mod


def _record() -> TranscriptRecord:
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
        full_text="hello world",
    )


def _llm_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("0.001"),
        provider="anthropic_direct",
        model="claude-sonnet-4-6",
        latency_ms=500,
    )


def test_hash_normalization_is_nfc_lowered() -> None:
    """NBSP and casing differences hash to the same value after normalization."""
    h1 = sum_mod._sha256("SE Team")
    h2 = sum_mod._sha256("se team")
    assert h1 == h2


def test_parse_summary_json_extracts_timestamps() -> None:
    text = '{"summary": "ok", "key_timestamps": [{"t": 42, "label": "pricing"}]}'
    summary, ts = sum_mod._parse_summary_response(text)
    assert summary == "ok"
    assert ts == [sum_mod.KeyTimestamp(t=42, label="pricing")]


def test_parse_summary_handles_code_fences() -> None:
    text = '```json\n{"summary": "fenced", "key_timestamps": []}\n```'
    summary, ts = sum_mod._parse_summary_response(text)
    assert summary == "fenced"
    assert ts == []


def test_parse_summary_falls_back_to_prose_on_bad_json() -> None:
    text = "Just a plain summary, no JSON here."
    summary, ts = sum_mod._parse_summary_response(text)
    assert summary == text
    assert ts == []


@pytest.mark.asyncio
async def test_summarize_returns_cached_when_present() -> None:
    """A cached summary row short-circuits the LLM call."""
    session = AsyncMock()
    cached = MagicMock()
    cached.summary = "cached summary"
    cached.provider_used = "anthropic_direct/claude-sonnet-4-6"
    cached.tokens_in = 100
    cached.tokens_out = 50
    cached.cost_usd = Decimal("0.001")
    cached.key_timestamps_jsonb = [{"t": 42, "label": "pricing"}]

    with (
        patch.object(sum_mod, "_lookup_cached_summary", new=AsyncMock(return_value=cached)),
        patch.object(sum_mod, "llm_execute", new=AsyncMock()) as mock_llm,
        patch.object(sum_mod, "get_transcript", new=AsyncMock()) as mock_get,
    ):
        result = await sum_mod.summarize(
            session,
            video_id="OMhKgQmeMhI",
            style="exec_brief",
            audience="SE team",
            custom_prompt=None,
            max_tokens=800,
            include_timestamps=True,
            provider_override=None,
        )

    assert result.cached is True
    assert result.summary == "cached summary"
    assert len(result.key_timestamps) == 1
    mock_llm.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_calls_llm_on_cache_miss() -> None:
    """Cache miss → LLM call → persist + return fresh result."""
    session = AsyncMock()
    record = _record()

    with (
        patch.object(sum_mod, "_lookup_cached_summary", new=AsyncMock(return_value=None)),
        patch.object(sum_mod, "get_transcript", new=AsyncMock(return_value=record)),
        patch.object(
            sum_mod,
            "llm_execute",
            new=AsyncMock(return_value=_llm_response('{"summary": "fresh", "key_timestamps": []}')),
        ) as mock_llm,
        patch.object(sum_mod, "_persist_summary", new=AsyncMock()) as mock_persist,
    ):
        result = await sum_mod.summarize(
            session,
            video_id="OMhKgQmeMhI",
            style="exec_brief",
            audience="SE team",
            custom_prompt=None,
            max_tokens=800,
            include_timestamps=True,
            provider_override=None,
        )

    assert result.cached is False
    assert result.summary == "fresh"
    assert result.provider_used == "anthropic_direct/claude-sonnet-4-6"
    mock_llm.assert_awaited_once()
    mock_persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_summarize_routes_exec_deep_to_specific_task() -> None:
    """style=exec_deep must dispatch to ``summarize_exec_deep``, not ``summarize``."""
    session = AsyncMock()
    record = _record()

    with (
        patch.object(sum_mod, "_lookup_cached_summary", new=AsyncMock(return_value=None)),
        patch.object(sum_mod, "get_transcript", new=AsyncMock(return_value=record)),
        patch.object(
            sum_mod,
            "llm_execute",
            new=AsyncMock(return_value=_llm_response('{"summary": "deep"}')),
        ) as mock_llm,
        patch.object(sum_mod, "_persist_summary", new=AsyncMock()),
    ):
        await sum_mod.summarize(
            session,
            video_id="OMhKgQmeMhI",
            style="exec_deep",
            audience="execs",
            custom_prompt=None,
            max_tokens=2000,
            include_timestamps=True,
            provider_override=None,
        )

    kwargs = mock_llm.await_args.kwargs
    assert kwargs["task"] == "summarize_exec_deep"


@pytest.mark.asyncio
async def test_summarize_custom_requires_prompt() -> None:
    """style=custom without custom_prompt raises InvalidRequestError."""
    session = AsyncMock()
    with pytest.raises(InvalidRequestError, match="custom_prompt"):
        await sum_mod.summarize(
            session,
            video_id="OMhKgQmeMhI",
            style="custom",
            audience="",
            custom_prompt=None,
            max_tokens=800,
            include_timestamps=False,
            provider_override=None,
        )


@pytest.mark.asyncio
async def test_summarize_missing_transcript_raises_not_found() -> None:
    """No cached transcript → NotFoundError telling the caller to fetch first."""
    session = AsyncMock()
    with (
        patch.object(sum_mod, "_lookup_cached_summary", new=AsyncMock(return_value=None)),
        patch.object(sum_mod, "get_transcript", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(NotFoundError, match="not cached"):
            await sum_mod.summarize(
                session,
                video_id="OMhKgQmeMhI",
                style="exec_brief",
                audience="",
                custom_prompt=None,
                max_tokens=800,
                include_timestamps=False,
                provider_override=None,
            )


def test_enrich_with_deep_links_adds_per_entry_link() -> None:
    """Each timestamp gets a youtu.be?t=<seconds> deep link."""
    timestamps = [
        sum_mod.KeyTimestamp(t=0, label="intro"),
        sum_mod.KeyTimestamp(t=412, label="pricing"),
    ]
    enriched = sum_mod.enrich_with_deep_links(timestamps, "OMhKgQmeMhI")
    assert enriched[0]["deep_link"] == "https://youtu.be/OMhKgQmeMhI?t=0"
    assert enriched[1]["deep_link"] == "https://youtu.be/OMhKgQmeMhI?t=412"
