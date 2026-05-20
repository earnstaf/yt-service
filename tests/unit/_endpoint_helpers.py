"""Shared helpers for endpoint tests.

We build a fresh FastAPI app per test using :func:`app.main.create_app` and
install dependency overrides for ``get_session``, ``get_redis_client``, the
auth scope dependency, and (optionally) ``transcript_service.get_or_fetch``.

The auth override is keyed off a stub that returns a fake :class:`Token` row
with the configured scopes, bypassing the database lookup entirely. Tests
that want to exercise the *real* auth path can swap the override.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app import jobs as jobs_module
from app import transcript_service
from app.auth import require_scopes as real_require_scopes
from app.db import get_session
from app.exceptions import InsufficientScopeError, UnauthorizedError
from app.main import create_app
from app.redis_client import get_redis_client


def make_token_stub(
    token_id: str = "tok_test",
    scopes: tuple[str, ...] = ("read", "batch", "summarize", "intelligence", "monitor", "admin"),
) -> SimpleNamespace:
    """A dataclass-shaped object compatible with the parts of ``Token`` we use."""
    return SimpleNamespace(id=token_id, scopes=list(scopes), name="test")


def make_require_scopes_override(
    token: SimpleNamespace,
    has_scopes: tuple[str, ...] | None = None,
):
    """Build a replacement ``require_scopes`` dependency factory.

    The returned factory mirrors the real one's signature: pass the required
    scopes and get a dependency callable. The dependency stashes ``token`` on
    ``request.state.token`` and raises ``InsufficientScopeError`` if any
    required scope is missing from ``has_scopes`` (which defaults to
    ``token.scopes``).
    """
    effective_scopes = set(has_scopes) if has_scopes is not None else set(token.scopes)

    def factory(*required_scopes: str):
        async def dep(request: Request) -> SimpleNamespace:
            # Mirror the real behavior: 'admin' grants everything.
            if "admin" not in effective_scopes:
                for required in required_scopes:
                    if required not in effective_scopes:
                        raise InsufficientScopeError(f"token lacks scope: {required}")
            request.state.token = token
            return token

        return dep

    return factory


class FakeRedis:
    """In-memory subset of redis.asyncio.Redis used by the rate-limit path.

    Records the calls so tests can assert. Implements just the slice
    :func:`app.middleware.enforce_rate_limit` and the lock helpers in
    :mod:`app.jobs` actually touch.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.locks: dict[str, bytes] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    async def set(self, key: str, value: bytes, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.locks:
            return False
        self.locks[key] = value
        return True

    async def delete(self, key: str) -> int:
        existed = self.locks.pop(key, None) is not None
        return 1 if existed else 0

    async def eval(self, script: str, num_keys: int, *args: Any) -> int:
        """Minimal Lua eval shim for the compare-and-delete release script.

        Recognizes the H9 release script by structural match (``GET`` then
        conditional ``DEL``). Returns 1 on owner match, 0 otherwise.
        """
        keys = list(args[:num_keys])
        argv = list(args[num_keys:])
        if "redis.call('GET', KEYS[1])" in script and "redis.call('DEL', KEYS[1])" in script:
            key = keys[0]
            expected = argv[0] if argv else None
            current = self.locks.get(key)
            if current == expected:
                self.locks.pop(key, None)
                return 1
            return 0
        raise NotImplementedError("FakeRedis.eval only models the release script")

    async def ping(self) -> bool:
        return True


def install_overrides(
    app: FastAPI,
    *,
    token: SimpleNamespace | None = None,
    has_scopes: tuple[str, ...] | None = None,
    session: Any | None = None,
    redis_client: Any | None = None,
) -> tuple[Any, FakeRedis | Any, SimpleNamespace]:
    """Install standard overrides on ``app`` and return ``(session, redis, token)``."""
    tok = token or make_token_stub()
    sess = session or AsyncMock()
    rc = redis_client or FakeRedis()

    async def _session_dep() -> AsyncIterator[Any]:
        yield sess

    app.dependency_overrides[get_session] = _session_dep
    app.dependency_overrides[get_redis_client] = lambda: rc

    # Patch require_scopes by replacing the function in app.auth so route
    # registration picked up the override at create_app() time. Because the
    # dependency is closed over by the route, we cannot swap it after the
    # router is built — install_overrides MUST be called BEFORE building the
    # app. Use :func:`build_app_with_auth_stub` for that flow.
    return sess, rc, tok


def build_app_with_auth_stub(
    *,
    token: SimpleNamespace | None = None,
    has_scopes: tuple[str, ...] | None = None,
    session: Any | None = None,
    redis_client: Any | None = None,
    monkeypatch: Any | None = None,
) -> tuple[FastAPI, Any, FakeRedis | Any, SimpleNamespace]:
    """Build a fresh app with ``require_scopes`` swapped before route registration.

    Returns ``(app, session, redis_client, token)``.
    """
    if monkeypatch is None:
        raise RuntimeError("monkeypatch fixture required")

    tok = token or make_token_stub()
    factory = make_require_scopes_override(tok, has_scopes=has_scopes)

    # Swap require_scopes in both modules so create_app -> _build_v1_router picks it up.
    import app.auth as auth_module
    import app.main as main_module

    monkeypatch.setattr(auth_module, "require_scopes", factory)
    monkeypatch.setattr(main_module, "require_scopes", factory)

    app = create_app()
    sess, rc, tok = install_overrides(
        app,
        token=tok,
        has_scopes=has_scopes,
        session=session,
        redis_client=redis_client,
    )
    return app, sess, rc, tok


def restore_require_scopes(monkeypatch: Any) -> None:
    """Helper to restore the real require_scopes — monkeypatch handles teardown."""
    _ = monkeypatch  # noqa — monkeypatch.undo() runs automatically


def client_for(app: FastAPI, *, client_host: str = "127.0.0.1") -> AsyncClient:
    """Build an ``httpx.AsyncClient`` wrapping the ASGI app."""
    transport = ASGITransport(app=app, client=(client_host, 12345))
    return AsyncClient(transport=transport, base_url="http://test")


__all__ = [
    "FakeRedis",
    "build_app_with_auth_stub",
    "client_for",
    "install_overrides",
    "make_require_scopes_override",
    "make_token_stub",
]
