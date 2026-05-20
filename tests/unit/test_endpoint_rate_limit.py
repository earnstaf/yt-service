"""Rate-limit integration into /v1 endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.schemas import TranscriptResponse, TranscriptSnippetOut

from ._endpoint_helpers import FakeRedis, build_app_with_auth_stub, client_for


def _make_response() -> TranscriptResponse:
    snip = TranscriptSnippetOut(
        start=0.0, duration=1.0, text="hi", speaker=None, deep_link=""
    )
    return TranscriptResponse(
        video_id="OMhKgQmeMhI",
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
async def test_rate_limit_exceeded_returns_429_with_retry_after(monkeypatch) -> None:
    """Burn through the read limit (60/min), 61st should be 429."""
    redis = FakeRedis()
    # Pre-seed the counter to 60 so the next incr lands on 61.
    redis.counts["rl:read:tok_test"] = 60
    redis.ttls["rl:read:tok_test"] = 30

    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, redis_client=redis)
    monkeypatch.setattr(
        "app.transcript_service.get_or_fetch",
        AsyncMock(return_value=("transcript", _make_response())),
    )

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/transcript",
            params={"v": "OMhKgQmeMhI"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert resp.headers.get("retry-after") == "30"


@pytest.mark.asyncio
async def test_jobs_endpoint_enforces_read_rate_limit(monkeypatch) -> None:
    """H8: ``GET /v1/jobs/{job_id}`` must apply the read rate-limit bucket.

    Pre-seed the read counter so the next request lands at 61 (limit+1)
    and assert the 429 / Retry-After contract holds for the jobs route too.
    """
    redis = FakeRedis()
    redis.counts["rl:read:tok_test"] = 60
    redis.ttls["rl:read:tok_test"] = 30

    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, redis_client=redis)
    # Stub jobs.get_job so the request would otherwise succeed.
    fake_job = type(
        "FakeJob",
        (),
        {
            "job_id": "01HXJOB",
            "video_id": "OMhKgQmeMhI",
            "job_type": "whisper",
            "status": "queued",
            "started_at": None,
            "finished_at": None,
            "error": None,
        },
    )()
    monkeypatch.setattr("app.jobs.get_job", AsyncMock(return_value=fake_job))

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/jobs/01HXJOB",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert resp.headers.get("retry-after") == "30"
