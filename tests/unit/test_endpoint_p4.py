"""Unit tests for the P4 intelligence endpoints: topics, sentiment, diff."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.exceptions import FeatureDisabledError
from app.tasks.diff import DiffResult
from app.tasks.sentiment import SentimentResult
from app.tasks.topics import TopicResult

from ._endpoint_helpers import build_app_with_auth_stub, client_for, make_token_stub


def _intel_token():
    return make_token_stub(scopes=("read", "intelligence"))


# ---------------- topics ----------------


@pytest.mark.asyncio
async def test_topics_happy_path(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=_intel_token())
    result = TopicResult(
        video_id="OMhKgQmeMhI",
        topics=["security", "MDR pricing"],
        entities={"companies": ["Tanium"], "people": [], "products": ["Falcon"]},
        claims=[{"text": "Tanium reduced MTTR by 60%", "t": 412}],
        questions_raised=["How does pricing compare?"],
        provider_used="llmapi/gemini-2.5-flash",
        cached=False,
    )
    monkeypatch.setattr("app.tasks.topics.extract_topics", AsyncMock(return_value=result))

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/topics",
            json={"video_id": "OMhKgQmeMhI"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["topics"] == ["security", "MDR pricing"]
    assert body["provider_used"] == "llmapi/gemini-2.5-flash"


# ---------------- sentiment ----------------


@pytest.mark.asyncio
async def test_sentiment_happy_path(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=_intel_token())
    result = SentimentResult(
        video_id="OMhKgQmeMhI",
        granularity="chapter",
        overall_score=0.42,
        overall_label="positive",
        timeline=[
            {"start": 0.0, "end": 245.0, "score": 0.61, "label": "positive"},
            {"start": 245.0, "end": 612.0, "score": -0.12, "label": "neutral"},
        ],
        provider_used="gemini_direct/gemini-2.5-flash",
    )
    monkeypatch.setattr(
        "app.tasks.sentiment.compute_sentiment", AsyncMock(return_value=result)
    )

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/sentiment",
            json={"video_id": "OMhKgQmeMhI", "granularity": "chapter"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "chapter"
    assert body["overall"] == {"score": 0.42, "label": "positive"}
    assert len(body["timeline"]) == 2


@pytest.mark.asyncio
async def test_sentiment_feature_disabled(monkeypatch) -> None:
    """When FEATURE_SENTIMENT=false the task raises and the route surfaces 403."""
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=_intel_token())
    monkeypatch.setattr(
        "app.tasks.sentiment.compute_sentiment",
        AsyncMock(side_effect=FeatureDisabledError("sentiment endpoint is disabled")),
    )

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/sentiment",
            json={"video_id": "OMhKgQmeMhI"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 403
    assert resp.json()["error"] == "feature_disabled"


# ---------------- diff ----------------


@pytest.mark.asyncio
async def test_diff_happy_path(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=_intel_token())
    result = DiffResult(
        video_a="aaaaaaaaaaa",
        video_b="bbbbbbbbbbb",
        focus="topics_and_emphasis",
        added_in_b=[{"topic": "new product", "evidence": "..."}],
        removed_from_a=[],
        shifted_emphasis=[{"topic": "pricing", "direction": "more", "delta_pct": 30}],
        key_quotes_a=["A quote 1"],
        key_quotes_b=["B quote 1"],
        executive_summary="B emphasizes pricing more than A.",
        provider_used="anthropic_direct/claude-sonnet-4-6",
    )
    monkeypatch.setattr("app.tasks.diff.diff_transcripts", AsyncMock(return_value=result))

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/diff",
            json={"video_a": "aaaaaaaaaaa", "video_b": "bbbbbbbbbbb"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["focus"] == "topics_and_emphasis"
    assert len(body["added_in_b"]) == 1
    assert body["executive_summary"].startswith("B emphasizes")


# ---------------- scope guards ----------------


@pytest.mark.asyncio
async def test_topics_requires_intelligence_scope(monkeypatch) -> None:
    """Tokens without `intelligence` scope are rejected (spec §6)."""
    read_only = make_token_stub(scopes=("read",))
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=read_only)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/topics",
            json={"video_id": "OMhKgQmeMhI"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 403
