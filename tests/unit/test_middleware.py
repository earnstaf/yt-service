"""Unit tests for :mod:`app.middleware`."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.exceptions import RateLimitedError
from app.middleware import _limit_for_category, _parse_limit, enforce_rate_limit


def test_parse_limit_minute() -> None:
    assert _parse_limit("60/minute") == (60, 60)


def test_parse_limit_hour() -> None:
    assert _parse_limit("30/hour") == (30, 3600)


def test_parse_limit_second() -> None:
    assert _parse_limit("5/second") == (5, 1)


def test_parse_limit_case_insensitive_and_whitespace() -> None:
    assert _parse_limit("  10 / MINUTE ") == (10, 60)


def test_parse_limit_invalid_period() -> None:
    with pytest.raises(ValueError, match="invalid rate limit period"):
        _parse_limit("60/fortnight")


def test_parse_limit_invalid_count() -> None:
    with pytest.raises(ValueError, match="invalid rate limit count"):
        _parse_limit("foo/minute")


def test_parse_limit_missing_slash() -> None:
    with pytest.raises(ValueError, match="invalid rate limit spec"):
        _parse_limit("60-minute")


def test_limit_for_category_pulls_from_settings() -> None:
    # rate_limit_read default is "60/minute"
    count, window = _limit_for_category("read")
    assert count == 60
    assert window == 60


def _make_request(token_id: str | None = "tok_test", client_host: str = "127.0.0.1") -> MagicMock:
    state = SimpleNamespace()
    if token_id is not None:
        state.token = SimpleNamespace(id=token_id)
    req = MagicMock()
    req.state = state
    req.client = SimpleNamespace(host=client_host)
    return req


@pytest.mark.asyncio
async def test_enforce_rate_limit_under_limit_allows() -> None:
    redis_client = MagicMock()
    redis_client.incr = AsyncMock(return_value=1)
    redis_client.expire = AsyncMock(return_value=True)
    redis_client.ttl = AsyncMock(return_value=60)
    req = _make_request()
    # First call sets expire on count == 1.
    await enforce_rate_limit("read", req, redis_client)
    redis_client.incr.assert_awaited_once()
    redis_client.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_enforce_rate_limit_at_limit_allows() -> None:
    redis_client = MagicMock()
    redis_client.incr = AsyncMock(return_value=60)  # limit is 60/min
    redis_client.expire = AsyncMock()
    redis_client.ttl = AsyncMock(return_value=30)
    req = _make_request()
    await enforce_rate_limit("read", req, redis_client)  # 60 == limit, no raise


@pytest.mark.asyncio
async def test_enforce_rate_limit_exceeded_raises_with_retry_after() -> None:
    redis_client = MagicMock()
    redis_client.incr = AsyncMock(return_value=61)
    redis_client.expire = AsyncMock()
    redis_client.ttl = AsyncMock(return_value=42)
    req = _make_request()
    with pytest.raises(RateLimitedError) as exc_info:
        await enforce_rate_limit("read", req, redis_client)
    assert exc_info.value.details["retry_after"] == 42
    assert exc_info.value.details["limit"] == 60


@pytest.mark.asyncio
async def test_enforce_rate_limit_falls_back_to_window_when_ttl_negative() -> None:
    redis_client = MagicMock()
    redis_client.incr = AsyncMock(return_value=100)
    redis_client.expire = AsyncMock()
    redis_client.ttl = AsyncMock(return_value=-1)
    req = _make_request()
    with pytest.raises(RateLimitedError) as exc_info:
        await enforce_rate_limit("read", req, redis_client)
    # Fallback is the window length (60s for read).
    assert exc_info.value.details["retry_after"] == 60


@pytest.mark.asyncio
async def test_enforce_rate_limit_anonymous_uses_client_ip() -> None:
    redis_client = MagicMock()
    redis_client.incr = AsyncMock(return_value=1)
    redis_client.expire = AsyncMock()
    redis_client.ttl = AsyncMock(return_value=60)
    req = _make_request(token_id=None, client_host="10.0.0.5")
    await enforce_rate_limit("read", req, redis_client)
    # Key uses the client IP since no token.
    call_args = redis_client.incr.call_args.args
    assert "10.0.0.5" in call_args[0]
