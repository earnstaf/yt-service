"""Unit tests for DELETE /v1/cache/{video_id} and GET /v1/cache/stats."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from ._endpoint_helpers import build_app_with_auth_stub, client_for


@pytest.mark.asyncio
async def test_delete_cache_with_admin_returns_rows_deleted(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr("app.cache.purge_transcript", AsyncMock(return_value=2))

    async with client_for(app) as client:
        resp = await client.delete(
            "/v1/cache/OMhKgQmeMhI",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["video_id"] == "OMhKgQmeMhI"
    assert body["rows_deleted"] == 2


@pytest.mark.asyncio
async def test_delete_cache_without_admin_returns_403(monkeypatch) -> None:
    # Token has 'read' but not 'admin'.
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, has_scopes=("read",))
    async with client_for(app) as client:
        resp = await client.delete(
            "/v1/cache/OMhKgQmeMhI",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 403
    assert resp.json()["error"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_cache_stats_returns_aggregated_counts(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.cache.stats",
        AsyncMock(
            return_value={
                "total_rows": 42,
                "by_source": {"youtube_captions": 30, "whisper_openai": 12},
                "oldest_cached_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "newest_cached_at": datetime(2026, 5, 20, tzinfo=timezone.utc),
            }
        ),
    )

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/cache/stats",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_rows"] == 42
    assert body["by_source"]["youtube_captions"] == 30
