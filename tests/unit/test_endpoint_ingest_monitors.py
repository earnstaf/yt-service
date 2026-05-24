"""Unit tests for /v1/ingest and /v1/monitors (P3)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import IngestResult, IngestVideoOutcome

from ._endpoint_helpers import build_app_with_auth_stub, client_for, make_token_stub


@pytest.mark.asyncio
async def test_ingest_dispatches_videos(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    result = IngestResult(
        ingest_id="ing_01HX",
        source="channel_handle:@TestCo",
        video_count=2,
        videos=[
            IngestVideoOutcome(video_id="OMhKgQmeMhI", status="cached"),
            IngestVideoOutcome(video_id="dQw4w9WgXcQ", status="queued", job_id="job_xyz"),
        ],
    )
    monkeypatch.setattr(
        "app.ingest.ingest_channel_or_playlist", AsyncMock(return_value=result)
    )

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/ingest",
            json={"url": "https://www.youtube.com/@TestCo", "max_videos": 5},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["video_count"] == 2
    statuses = {v["status"] for v in body["videos"]}
    assert statuses == {"cached", "queued"}


@pytest.mark.asyncio
async def test_ingest_rejects_bad_since(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/ingest",
            json={"url": "https://www.youtube.com/@TestCo", "since": "not-a-date"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_monitor_create_rejects_playlist_url(monkeypatch) -> None:
    """Monitors track channels, not playlists."""
    monitor_token = make_token_stub(scopes=("read", "monitor"))
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=monitor_token)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/monitors",
            json={
                "channel_url": "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
                "poll_interval_minutes": 60,
                "callback_url": "https://hooks.example.com/yt",
            },
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_monitor_create_happy_path(monkeypatch) -> None:
    monitor_token = make_token_stub(scopes=("read", "monitor"))
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=monitor_token)

    fake_monitor = SimpleNamespace(
        id="mon_01HX",
        channel_id="UCBR8-60-B28hp2BmDPdntcQ",
        channel_url="https://www.youtube.com/channel/UCBR8-60-B28hp2BmDPdntcQ",
        poll_interval_minutes=60,
        include_jsonb=["chapters"],
        callback_url="https://hooks.example.com/yt",
        notes=None,
        last_polled_at=None,
        last_video_id=None,
        created_by="tok_test",
        created_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        paused=False,
    )
    monkeypatch.setattr(
        "app.monitors.create_monitor", AsyncMock(return_value=fake_monitor)
    )
    # SSRF guard can't resolve example.com inside the test env — bypass.
    monkeypatch.setattr(
        "app.url_safety.validate_callback_url", lambda url: url
    )

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/monitors",
            json={
                "channel_url": "https://www.youtube.com/channel/UCBR8-60-B28hp2BmDPdntcQ",
                "poll_interval_minutes": 60,
                "include": ["chapters"],
                "callback_url": "https://hooks.example.com/yt",
            },
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "mon_01HX"
    assert body["include"] == ["chapters"]


@pytest.mark.asyncio
async def test_monitor_requires_monitor_scope(monkeypatch) -> None:
    """Creating a monitor requires the 'monitor' scope (not just 'read')."""
    read_only = make_token_stub(scopes=("read",))
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=read_only)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/monitors",
            json={
                "channel_url": "https://www.youtube.com/@TestCo",
                "poll_interval_minutes": 60,
                "callback_url": "https://hooks.example.com/yt",
            },
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 403
