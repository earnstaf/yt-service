"""Integration tests for token persistence and the bootstrap-admin flow.

These exercise ``app.auth.lookup_token`` and ``app.auth.bootstrap_admin_token``
against a real Postgres database. They are gated by the ``integration`` marker
(P-14) so the default ``pytest`` run skips them. Run with::

    pytest -m integration

Database fixtures are owned by the broader integration suite (a fresh schema
plus the ``Token`` table is enough); these tests assume an async session
factory is configured via ``app.db.get_session_factory`` and that the schema
has been migrated.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from sqlalchemy import delete  # noqa: E402  -- after pytestmark by design
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.auth import (  # noqa: E402
    bootstrap_admin_token,
    hash_token,
    lookup_token,
    make_token,
    make_token_id,
)
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Token  # noqa: E402


async def _clear_tokens(session: AsyncSession) -> None:
    """Wipe the ``tokens`` table so each test starts from a known-empty state."""
    await session.execute(delete(Token))
    await session.commit()


@pytest.fixture
async def session() -> AsyncSession:
    """Yield an async session with the tokens table wiped on entry and exit."""
    factory = get_session_factory()
    async with factory() as s:
        await _clear_tokens(s)
        try:
            yield s
        finally:
            await _clear_tokens(s)


@pytest.mark.asyncio
async def test_lookup_token_finds_created_token(session: AsyncSession) -> None:
    plain = make_token()
    row = Token(
        id=make_token_id(),
        name="test-token",
        token_hash=hash_token(plain),
        scopes=["read"],
    )
    session.add(row)
    await session.commit()

    found = await lookup_token(session, plain)
    assert found is not None
    assert found.id == row.id
    assert found.scopes == ["read"]
    # last_used_at gets stamped by the lookup.
    assert found.last_used_at is not None


@pytest.mark.asyncio
async def test_lookup_token_returns_none_for_revoked_token(session: AsyncSession) -> None:
    plain = make_token()
    row = Token(
        id=make_token_id(),
        name="revoked-token",
        token_hash=hash_token(plain),
        scopes=["read"],
    )
    session.add(row)
    await session.commit()

    # Revoke and confirm.
    from sqlalchemy import func

    row.revoked_at = func.now()  # type: ignore[assignment]
    await session.commit()

    found = await lookup_token(session, plain)
    assert found is None


@pytest.mark.asyncio
async def test_lookup_token_returns_none_for_unknown_plain(session: AsyncSession) -> None:
    # No rows seeded: every lookup must miss.
    found = await lookup_token(session, "yt_definitely_not_a_real_token_value")
    assert found is None


@pytest.mark.asyncio
async def test_bootstrap_admin_token_noops_when_admin_exists(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed an existing admin token.
    existing = Token(
        id=make_token_id(),
        name="seed-admin",
        token_hash=hash_token(make_token()),
        scopes=["admin"],
    )
    session.add(existing)
    await session.commit()

    # Configure a bootstrap value: it must be ignored because admin already exists.
    settings = get_settings()
    monkeypatch.setattr(settings, "yt_bootstrap_admin_token", "should-not-be-used", raising=False)

    result = await bootstrap_admin_token(session)
    assert result is None


@pytest.mark.asyncio
async def test_bootstrap_admin_token_inserts_on_first_start(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    plain = "yt_test_bootstrap_value_please_rotate"
    monkeypatch.setattr(settings, "yt_bootstrap_admin_token", plain, raising=False)

    result = await bootstrap_admin_token(session)
    assert result == plain

    found = await lookup_token(session, plain)
    assert found is not None
    assert "admin" in (found.scopes or ())
    # Bootstrap row carries the full default scope bundle so the operator
    # can perform any P1 action immediately.
    for required in ("read", "batch", "summarize", "intelligence", "monitor"):
        assert required in (found.scopes or ())


@pytest.mark.asyncio
async def test_bootstrap_admin_token_noops_when_env_unset(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "yt_bootstrap_admin_token", None, raising=False)

    result = await bootstrap_admin_token(session)
    assert result is None
