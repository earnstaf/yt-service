"""Bearer-token authentication, hashing, and scope-checking for yt-transcript-service.

This module is the single place that:

- Mints raw token plaintext (``yt_`` prefix + 32 URL-safe bytes).
- Hashes plaintext with argon2id for storage in the ``tokens`` table.
- Verifies an inbound ``Authorization: Bearer <token>`` header against the
  current set of non-revoked rows.
- Exposes a FastAPI dependency factory (``require_scopes``) that handlers
  use to gate access by scope (spec §6).
- Runs the first-start bootstrap that converts the ``YT_BOOTSTRAP_ADMIN_TOKEN``
  env value into a real ``tokens`` row when no admin token exists yet (P-15).

Design notes:

- Token IDs use a distinct ``tok_`` prefix so they are visually distinguishable
  from job IDs (``job_``) in logs and admin output.
- ``lookup_token`` is O(N) over active tokens because argon2 is intentionally
  non-comparable across rows: we have to verify against each candidate hash.
  Acceptable for the single-org use case targeted by P1 (<100 tokens). If we
  ever grow past that, the right move is a secondary lookup key (e.g. an HMAC
  prefix of the plaintext stored alongside the hash) rather than weaker hashing.
- ``rate_overrides`` lives on the Token row but is NOT consulted here. P2+
  wires it into the rate-limit middleware (P-12).
- The dependency stashes the resolved ``Token`` on ``request.state.token`` so
  the rate-limit middleware (P-10) can key by ``token.id`` without re-running
  the verification path.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import argon2
import argon2.exceptions
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.exceptions import InsufficientScopeError, UnauthorizedError
from app.logging import get_logger
from app.models import Token

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# Header parser. Case-insensitive on the scheme, allows arbitrary whitespace
# between scheme and credential per RFC 7235 §2.1. Capturing group is the
# raw token string (trimmed below).
BEARER_RE: re.Pattern[str] = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)

# argon2 hasher with library defaults. Defaults are tuned to be both safe and
# reasonably fast (~50-100ms per verify on commodity hardware). We don't pin
# specific time/memory/parallelism here so we follow the library's evolving
# best practice.
_HASHER = argon2.PasswordHasher()


def make_token() -> str:
    """Return a fresh token plaintext: ``yt_`` + 32 URL-safe bytes.

    ``secrets.token_urlsafe(32)`` yields ~43 chars of base64url, giving a
    total length of ~46 — well above the 30-char floor the unit tests check.
    """
    return "yt_" + secrets.token_urlsafe(32)


def make_token_id() -> str:
    """Return a token row id: ``tok_`` + 12 URL-safe bytes.

    Distinct prefix from job IDs (``job_``) so the two never look alike in
    logs, admin output, or accidental copy/paste.
    """
    return "tok_" + secrets.token_urlsafe(12)


def make_webhook_secret() -> str:
    """Return a fresh 64-char hex webhook signing secret (256 bits of entropy)."""
    return secrets.token_hex(32)


def hash_token(plain: str) -> str:
    """Hash ``plain`` with argon2id and return the encoded string.

    The output is the full argon2 encoded form (``$argon2id$v=19$m=...$t=...$p=...$salt$hash``)
    which embeds the parameters needed to verify later.
    """
    return _HASHER.hash(plain)


def verify_token_hash(stored_hash: str, plain: str) -> bool:
    """Return True if ``plain`` matches ``stored_hash``, False otherwise.

    Wraps ``argon2``'s ``VerifyMismatchError`` (and the broader ``VerificationError``)
    so callers can use a plain boolean idiom. Any other exception is allowed to
    propagate because it signals a misconfigured hash string, not a bad guess.
    """
    try:
        return _HASHER.verify(stored_hash, plain)
    except argon2.exceptions.VerifyMismatchError:
        return False
    except argon2.exceptions.InvalidHashError:
        # Stored hash is corrupted or in an unknown format. Treat as a miss
        # rather than crashing the request path.
        return False


async def lookup_token(session: AsyncSession, plain: str) -> Token | None:
    """Find the live ``Token`` row matching ``plain``, or return None.

    Performance note: argon2 hashes are not comparable across rows, so this is
    O(N) verifies per request, where N is the number of non-revoked tokens. The
    single-org P1 target keeps N small (<100). If that ever changes, introduce
    a lookup index (e.g. a non-secret HMAC prefix stored alongside the hash)
    rather than swapping argon2 for a faster but weaker primitive.

    On match, updates ``last_used_at`` to ``now()`` and commits the session so
    the timestamp survives even when the surrounding request later rolls back
    on an unrelated failure.
    """
    stmt = select(Token).where(Token.revoked_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        if verify_token_hash(row.token_hash, plain):
            # Snapshot the attributes we'll need downstream BEFORE any commit.
            # Commits expire ORM attributes by default; subsequent calls (rate
            # limit middleware, route handlers) that re-access ``token.id`` or
            # ``token.scopes`` would otherwise hit a DetachedInstanceError if
            # the request's session has already been closed by the dependency
            # generator. Holding the snapshot decouples lifetime.
            from types import SimpleNamespace  # noqa: PLC0415

            snapshot = SimpleNamespace(
                id=row.id,
                name=row.name,
                scopes=list(row.scopes or []),
                webhook_secret=row.webhook_secret,
                rate_overrides=dict(row.rate_overrides or {}),
            )

            from sqlalchemy import func as _func  # noqa: PLC0415

            row.last_used_at = _func.now()  # type: ignore[assignment]
            await session.commit()
            return snapshot  # type: ignore[return-value]
    return None


def require_scopes(
    *required_scopes: str,
) -> Callable[[Request, AsyncSession], Awaitable[Token]]:
    """Return a FastAPI dependency that enforces a bearer token with all ``required_scopes``.

    Behavior:

    - Missing or malformed ``Authorization`` header → ``UnauthorizedError``.
    - Header present but token not found / revoked → ``UnauthorizedError``.
    - Token present but missing any required scope → ``InsufficientScopeError``
      with the missing scope listed in the message.
    - On success, returns the ``Token`` row AND stashes it on
      ``request.state.token`` so downstream middleware (P-10 rate limiter)
      can key by ``token.id``.
    """

    async def dep(
        request: Request,
        session: AsyncSession = Depends(get_session),
    ) -> Token:
        header = request.headers.get("authorization")
        if not header:
            raise UnauthorizedError("missing or malformed Authorization header")
        match = BEARER_RE.match(header)
        if not match:
            raise UnauthorizedError("missing or malformed Authorization header")
        plain = match.group(1).strip()
        if not plain:
            raise UnauthorizedError("missing or malformed Authorization header")

        token = await lookup_token(session, plain)
        if token is None:
            raise UnauthorizedError("invalid token")

        token_scopes = set(token.scopes or ())
        # 'admin' is the master scope: it implies every other scope.
        if "admin" not in token_scopes:
            for required in required_scopes:
                if required not in token_scopes:
                    raise InsufficientScopeError(f"token lacks scope: {required}")

        # Stash for downstream middleware. ``request.state`` is the FastAPI
        # idiom for per-request context.
        request.state.token = token
        return token

    return dep


# Postgres advisory lock id used to serialize bootstrap admin insertion across
# concurrently starting workers (M3). Application-scoped 32-bit int; not
# shared with any other subsystem.
_BOOTSTRAP_ADMIN_LOCK_ID = 0xFE000001


async def bootstrap_admin_token(session: AsyncSession) -> str | None:
    """Insert a bootstrap admin token from ``YT_BOOTSTRAP_ADMIN_TOKEN`` on first start.

    Behavior per JC-011 / P-15:

    - If the env var is unset (None), this is a no-op and returns None.
    - If any token row with the ``admin`` scope already exists, this is a
      no-op and returns None. The bootstrap path runs exactly once over the
      lifetime of the database.
    - Otherwise, insert a new ``tokens`` row hashing the env value as the
      bootstrap admin token. Returns the plaintext (which the caller already
      has from the env). The plaintext is what the operator stored in
      ``.env``; we never print it again, only log the new token_id.

    M3: a Postgres advisory lock wraps the check-then-insert so two workers
    starting concurrently cannot both insert an admin row. The lock is
    session-scoped (auto-released on disconnect) but we still explicitly
    unlock at the end so subsequent unrelated work on the session is clean.
    Non-Postgres backends (SQLite in tests) skip the lock — the race is
    academic in that environment.
    """
    settings = get_settings()
    plain = settings.yt_bootstrap_admin_token
    if not plain:
        return None

    from sqlalchemy import text  # noqa: PLC0415 — lazy

    bind = session.get_bind()
    dialect_name = getattr(bind.dialect, "name", "") if bind is not None else ""
    is_pg = dialect_name == "postgresql"

    if is_pg:
        try:
            await session.execute(
                text("SELECT pg_advisory_lock(:k)"),
                {"k": _BOOTSTRAP_ADMIN_LOCK_ID},
            )
        except Exception:  # noqa: BLE001 — degrade gracefully if lock unavailable
            is_pg = False

    try:
        # Check for any existing admin token. We avoid SQLAlchemy's generic-
        # ARRAY ``contains`` (raises on Postgres: "@> not implemented for the
        # base ARRAY type") by doing the membership check Python-side. Single-
        # org service, <100 tokens at steady state, so O(N) is fine.
        stmt = select(Token).where(Token.revoked_at.is_(None))
        all_tokens = (await session.execute(stmt)).scalars().all()
        for tok in all_tokens:
            if "admin" in (tok.scopes or []):
                return None

        token_id = make_token_id()
        row = Token(
            id=token_id,
            name="bootstrap-admin",
            token_hash=hash_token(plain),
            scopes=["admin", "read", "batch", "summarize", "intelligence", "monitor"],
            webhook_secret=None,
            rate_overrides=None,
        )
        session.add(row)
        await session.commit()

        log.info(
            "bootstrap admin token activated",
            token_id=token_id,
            scopes=row.scopes,
            # NEVER include `plain` here. The redact processor would mask it,
            # but defense in depth — we just don't pass it.
        )
        return plain
    finally:
        if is_pg:
            try:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": _BOOTSTRAP_ADMIN_LOCK_ID},
                )
            except Exception:  # noqa: BLE001 — unlock failure is non-fatal
                pass


__all__ = [
    "BEARER_RE",
    "make_token",
    "make_token_id",
    "make_webhook_secret",
    "hash_token",
    "verify_token_hash",
    "lookup_token",
    "require_scopes",
    "bootstrap_admin_token",
]
