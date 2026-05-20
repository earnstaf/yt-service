"""Unit tests for POST /v1/transcript:batch."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.exceptions import InvalidVideoIdError
from app.schemas import JobAcceptedResponse, TranscriptResponse, TranscriptSnippetOut

from ._endpoint_helpers import build_app_with_auth_stub, client_for


def _make_response(video_id: str = "OMhKgQmeMhI") -> TranscriptResponse:
    snip = TranscriptSnippetOut(
        start=0.0,
        duration=1.0,
        text="hi",
        speaker=None,
        deep_link=f"https://youtu.be/{video_id}?t=0",
    )
    return TranscriptResponse(
        video_id=video_id,
        source="youtube_captions",
        language="en",
        is_generated=True,
        duration_seconds=10.0,
        snippet_count=1,
        cached_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        cache_hit=True,
        chapters=None,
        snippets=[snip],
        full_text="hi",
    )


@pytest.mark.asyncio
async def test_batch_empty_list_returns_400(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    async with client_for(app) as client:
        resp = await client.post(
            "/v1/transcript:batch",
            json={"videos": [], "lang": "en"},
            headers={"Authorization": "Bearer yt_stub"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_batch_too_large_returns_413(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    videos = [f"OMhKgQmeM{i:02d}" for i in range(51)]
    async with client_for(app) as client:
        resp = await client.post(
            "/v1/transcript:batch",
            json={"videos": videos, "lang": "en"},
            headers={"Authorization": "Bearer yt_stub"},
        )
    assert resp.status_code == 413
    assert resp.json()["error"] == "batch_too_large"


@pytest.mark.asyncio
async def test_batch_success_returns_items(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)

    async def fake_get_or_fetch(*args, **kwargs):
        req = args[2]
        if req.video_id == "AAAAAAAAAAA":
            return "transcript", _make_response(video_id="AAAAAAAAAAA")
        return "job_accepted", JobAcceptedResponse(
            job_id="job_x",
            status="queued",
            video_id=req.video_id,
            poll_url="/v1/jobs/job_x",
            estimated_seconds=90,
        )

    monkeypatch.setattr("app.transcript_service.get_or_fetch", fake_get_or_fetch)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/transcript:batch",
            json={"videos": ["AAAAAAAAAAA", "BBBBBBBBBBB"], "lang": "en"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # Spec §5.5: batch responds with a bare array of per-video objects (H10).
    assert isinstance(body, list)
    assert len(body) == 2
    # Discriminate by structural shape since the wire format no longer
    # carries the `kind` discriminator.
    transcripts = [i for i in body if "snippets" in i]
    jobs_ = [i for i in body if "poll_url" in i and "snippets" not in i]
    assert len(transcripts) == 1
    assert len(jobs_) == 1


@pytest.mark.asyncio
async def test_batch_one_failure_returns_envelope_other_succeeds(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)

    async def fake_get_or_fetch(session, redis_client, ts_request, token_id=None, **_kwargs):
        if ts_request.video_id == "BBBBBBBBBBB":
            raise InvalidVideoIdError("simulated failure")
        return "transcript", _make_response(video_id=ts_request.video_id)

    monkeypatch.setattr("app.transcript_service.get_or_fetch", fake_get_or_fetch)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/transcript:batch",
            json={"videos": ["AAAAAAAAAAA", "BBBBBBBBBBB"], "lang": "en"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    assert len(items) == 2
    # First item is the successful transcript; second is an error envelope.
    error_items = [i for i in items if "error" in i and "snippets" not in i]
    transcript_items = [i for i in items if "snippets" in i]
    assert len(error_items) == 1
    assert len(transcript_items) == 1
    assert error_items[0]["error"] == "invalid_video_id"


@pytest.mark.asyncio
async def test_batch_invalid_id_in_input_returns_envelope(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.transcript_service.get_or_fetch",
        AsyncMock(return_value=("transcript", _make_response())),
    )
    async with client_for(app) as client:
        resp = await client.post(
            "/v1/transcript:batch",
            json={"videos": ["not a real id"], "lang": "en"},
            headers={"Authorization": "Bearer yt_stub"},
        )
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    assert items[0]["error"] == "invalid_video_id"
