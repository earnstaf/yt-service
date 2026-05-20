"""Unit tests for auth on /v1 routes.

Uses the REAL ``require_scopes`` against a mocked DB session/lookup so the
header parsing, scope check, and error wrapping are all exercised.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.main import create_app
from app.redis_client import get_redis_client


def _make_token(scopes: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(id="tok_test", scopes=list(scopes), name="test")


def _build_app_with_real_auth(monkeypatch, *, token: SimpleNamespace | None) -> Any:
    """Construct the app with the REAL require_scopes but mocked lookup_token."""
    app = create_app()

    async def _session_dep() -> AsyncIterator[Any]:
        yield AsyncMock()

    app.dependency_overrides[get_session] = _session_dep

    class _Redis:
        async def incr(self, *_args, **_kw): return 1
        async def expire(self, *_args, **_kw): return True
        async def ttl(self, *_args, **_kw): return 60

    app.dependency_overrides[get_redis_client] = lambda: _Redis()

    async def _lookup(_session, plain: str):
        if token is not None and plain == "validtoken":
            return token
        return None

    monkeypatch.setattr("app.auth.lookup_token", _lookup)
    return app


@pytest.mark.asyncio
async def test_missing_authorization_header_returns_401(monkeypatch) -> None:
    app = _build_app_with_real_auth(monkeypatch, token=_make_token(("read",)))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/transcript?v=OMhKgQmeMhI")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_malformed_bearer_returns_401(monkeypatch) -> None:
    app = _build_app_with_real_auth(monkeypatch, token=_make_token(("read",)))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/transcript?v=OMhKgQmeMhI",
            headers={"Authorization": "Token abc"},  # wrong scheme
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unknown_token_returns_401(monkeypatch) -> None:
    app = _build_app_with_real_auth(monkeypatch, token=None)  # nothing matches
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/transcript?v=OMhKgQmeMhI",
            headers={"Authorization": "Bearer somethingbad"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_without_required_scope_returns_403(monkeypatch) -> None:
    # Token has only 'batch' but the route needs 'read'.
    app = _build_app_with_real_auth(monkeypatch, token=_make_token(("batch",)))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/transcript?v=OMhKgQmeMhI",
            headers={"Authorization": "Bearer validtoken"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"] == "insufficient_scope"
