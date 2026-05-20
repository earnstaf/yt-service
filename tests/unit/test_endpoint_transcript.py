"""Unit tests for GET /v1/transcript."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.schemas import JobAcceptedResponse, TranscriptResponse, TranscriptSnippetOut

from ._endpoint_helpers import build_app_with_auth_stub, client_for


def _make_response(*, cache_hit: bool = True, source: str = "youtube_captions") -> TranscriptResponse:
    snip = TranscriptSnippetOut(
        start=0.0,
        duration=4.2,
        text="Welcome to the keynote",
        speaker=None,
        deep_link="https://youtu.be/OMhKgQmeMhI?t=0",
    )
    return TranscriptResponse(
        video_id="OMhKgQmeMhI",
        source=source,  # type: ignore[arg-type]
        language="en",
        is_generated=True,
        duration_seconds=1847.3,
        snippet_count=1,
        cached_at=datetime(2026, 5, 20, 14, 2, 11, tzinfo=timezone.utc),
        cache_hit=cache_hit,
        chapters=None,
        snippets=[snip],
        full_text="Welcome to the keynote",
    )


@pytest.mark.asyncio
async def test_get_transcript_cache_hit_returns_200_json(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.transcript_service.get_or_fetch",
        AsyncMock(return_value=("transcript", _make_response(cache_hit=True))),
    )

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            params={"v": "OMhKgQmeMhI"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["video_id"] == "OMhKgQmeMhI"
    assert body["source"] == "youtube_captions"
    assert body["cache_hit"] is True


@pytest.mark.asyncio
async def test_get_transcript_captions_returns_200(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.transcript_service.get_or_fetch",
        AsyncMock(return_value=("transcript", _make_response(cache_hit=False))),
    )

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            params={"v": "OMhKgQmeMhI"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    assert resp.json()["cache_hit"] is False


@pytest.mark.asyncio
async def test_get_transcript_returns_202_for_queued_job(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    accepted = JobAcceptedResponse(
        job_id="job_abc",
        status="queued",
        video_id="OMhKgQmeMhI",
        poll_url="/v1/jobs/job_abc",
        estimated_seconds=90,
    )
    monkeypatch.setattr(
        "app.transcript_service.get_or_fetch",
        AsyncMock(return_value=("job_accepted", accepted)),
    )

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            params={"v": "OMhKgQmeMhI"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == "job_abc"
    assert body["poll_url"] == "/v1/jobs/job_abc"


@pytest.mark.asyncio
async def test_get_transcript_format_text_returns_plain_text(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.transcript_service.get_or_fetch",
        AsyncMock(return_value=("transcript", _make_response())),
    )

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            params={"v": "OMhKgQmeMhI", "format": "text"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "Welcome to the keynote"


@pytest.mark.asyncio
async def test_get_transcript_format_srt_returns_subrip(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.transcript_service.get_or_fetch",
        AsyncMock(return_value=("transcript", _make_response())),
    )

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            params={"v": "OMhKgQmeMhI", "format": "srt"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-subrip")
    assert "1\n00:00:00,000 --> 00:00:04,200\nWelcome to the keynote" in resp.text


@pytest.mark.asyncio
async def test_get_transcript_invalid_video_id_returns_400(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            params={"v": "not a valid id"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_video_id"


@pytest.mark.asyncio
async def test_get_transcript_missing_v_returns_validation_error(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
