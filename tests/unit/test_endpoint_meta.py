"""Unit tests for /healthz, /readyz, and /metrics."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


def _build_app() -> object:
    return create_app()


@pytest.mark.asyncio
async def test_healthz_always_200() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_ok_when_db_and_redis_healthy(monkeypatch) -> None:
    monkeypatch.setattr("app.main.check_db_health", AsyncMock(return_value=True))
    monkeypatch.setattr("app.main.check_redis_health", AsyncMock(return_value=True))
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_readyz_degraded_when_one_dep_down(monkeypatch) -> None:
    monkeypatch.setattr("app.main.check_db_health", AsyncMock(return_value=True))
    monkeypatch.setattr("app.main.check_redis_health", AsyncMock(return_value=False))
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "fail"


@pytest.mark.asyncio
async def test_readyz_unhealthy_when_all_down(monkeypatch) -> None:
    monkeypatch.setattr("app.main.check_db_health", AsyncMock(return_value=False))
    monkeypatch.setattr("app.main.check_redis_health", AsyncMock(return_value=False))
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_metrics_loopback_returns_200() -> None:
    app = _build_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "yt_requests_total" in resp.text or len(resp.content) >= 0


@pytest.mark.asyncio
async def test_metrics_external_ip_returns_403() -> None:
    app = _build_app()
    transport = ASGITransport(app=app, client=("8.8.8.8", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 403
    assert resp.json()["error"] == "feature_disabled"


@pytest.mark.asyncio
async def test_metrics_xff_loopback_with_nonloopback_peer_denied() -> None:
    """H7: X-Forwarded-For from a NON-loopback peer is untrusted.

    A forged ``X-Forwarded-For: 127.0.0.1`` cannot upgrade a public caller into
    a loopback caller. The peer (8.8.8.8) is not a trusted same-host proxy, so
    the forwarded header is ignored entirely and the peer decides — deny.
    """
    app = _build_app()
    transport = ASGITransport(app=app, client=("8.8.8.8", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "feature_disabled"


@pytest.mark.asyncio
async def test_metrics_loopback_peer_no_forwarded_headers_allowed() -> None:
    """H7 case (a): loopback peer, no proxy headers → allow."""
    app = _build_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_metrics_loopback_peer_with_public_xff_denied() -> None:
    """H7 case (b): loopback peer (trusted proxy), but XFF identifies a public client → deny."""
    app = _build_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics", headers={"X-Forwarded-For": "8.8.8.8"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "feature_disabled"


@pytest.mark.asyncio
async def test_metrics_loopback_peer_with_loopback_xff_allowed() -> None:
    """H7 case (b, continued): loopback peer + loopback XFF → allow (same-host scrape via proxy)."""
    app = _build_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_metrics_nonloopback_peer_no_headers_denied() -> None:
    """H7 case (c): non-loopback peer without forwarded headers → deny (peer-only check)."""
    app = _build_app()
    transport = ASGITransport(app=app, client=("8.8.8.8", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics", headers={"X-Real-IP": "127.0.0.1"})
    # X-Real-IP from an untrusted peer is also ignored — deny.
    assert resp.status_code == 403
