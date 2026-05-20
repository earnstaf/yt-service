"""Unit tests for GET /v1/jobs/{job_id}."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._endpoint_helpers import build_app_with_auth_stub, client_for


def _make_job(status: str = "complete") -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job_abc",
        video_id="OMhKgQmeMhI",
        job_type="whisper",
        status=status,
        started_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 20, 10, 1, tzinfo=timezone.utc) if status == "complete" else None,
        error=None,
    )


@pytest.mark.asyncio
async def test_get_job_complete_returns_status_and_transcript_url(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr("app.jobs.get_job", AsyncMock(return_value=_make_job(status="complete")))

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/jobs/job_abc",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "job_abc"
    assert body["status"] == "complete"
    assert body["transcript_url"] == "/v1/transcript?v=OMhKgQmeMhI"


@pytest.mark.asyncio
async def test_get_job_running_omits_transcript_url(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr("app.jobs.get_job", AsyncMock(return_value=_make_job(status="running")))

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/jobs/job_abc",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    assert resp.json()["transcript_url"] is None


@pytest.mark.asyncio
async def test_get_job_unknown_returns_404(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr("app.jobs.get_job", AsyncMock(return_value=None))

    async with client_for(app) as client:
        resp = await client.get(
            "/v1/jobs/job_missing",
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
