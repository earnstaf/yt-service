"""Admin CLI for yt-transcript-service token management.

Entry point: ``python -m app.admin``. Commands:

    python -m app.admin tokens create --name <name> --scopes read,batch
    python -m app.admin tokens create --name <name> --scopes admin
    python -m app.admin tokens list
    python -m app.admin tokens revoke --id <tok_id>

Per spec §6: token plaintext is printed exactly once at creation time, never
again. Only the argon2 hash is persisted. The CLI prints a "STORE THIS NOW"
warning above the plaintext so operators don't lose it.

Implementation notes:

- Click 8.x. Commands are async-on-the-inside but synchronous to Click by
  wrapping each body in ``asyncio.run(...)``.
- Sessions are opened via :func:`app.db.get_session_factory` directly rather
  than through the FastAPI request dependency.
- Failures use ``click.ClickException`` (or ``sys.exit`` for richer cases) so
  the process exits non-zero, which CI scripts and shell pipelines depend on.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import click
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_token, make_token, make_token_id, make_webhook_secret
from app.db import get_session_factory
from app.models import Token


# Allowed scope tokens. Reject anything outside this set so a typo doesn't
# silently create a token that can never authenticate against a real route.
ALLOWED_SCOPES: frozenset[str] = frozenset(
    {"read", "batch", "summarize", "intelligence", "monitor", "admin"}
)


# ---- Helpers ---------------------------------------------------------------


def _format_dt(value: datetime | None) -> str:
    """Render a tz-aware datetime as compact ISO-8601, or ``-`` for None."""
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_scopes(raw: str) -> list[str]:
    """Split a comma-separated scopes string into a clean list, preserving order.

    Rejects scopes not in :data:`ALLOWED_SCOPES` so a typo (``"admon"``) never
    silently creates an unusable token row. See N2 in the review notes.
    """
    out: list[str] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        scope = piece.strip()
        if not scope:
            continue
        if scope in seen:
            continue
        if scope not in ALLOWED_SCOPES:
            raise click.BadParameter(
                f"unknown scope {scope!r}; allowed: {sorted(ALLOWED_SCOPES)}"
            )
        seen.add(scope)
        out.append(scope)
    if not out:
        raise click.BadParameter("--scopes must contain at least one non-empty scope")
    return out


# ---- Async command bodies --------------------------------------------------


async def _create_token(name: str, scopes: list[str]) -> None:
    """Create a new token row with an always-generated webhook secret.

    H5: every token gets a webhook signing secret so any caller can later opt
    into webhook delivery without needing a separate "upgrade" admin path.
    The plaintext token AND the webhook secret are printed once and then
    only their derivatives are persisted (argon2 hash; secret stored raw,
    treated as a credential).
    """
    plaintext = make_token()
    token_id = make_token_id()
    webhook_secret = make_webhook_secret()

    factory = get_session_factory()
    session: AsyncSession
    async with factory() as session:
        row = Token(
            id=token_id,
            name=name,
            token_hash=hash_token(plaintext),
            scopes=scopes,
            webhook_secret=webhook_secret,
            rate_overrides=None,
        )
        session.add(row)
        await session.commit()

    click.echo("STORE THIS NOW. IT WILL NOT BE SHOWN AGAIN.")
    click.echo(f"token:          {plaintext}")
    click.echo("STORE THIS NOW. IT WILL NOT BE SHOWN AGAIN.")
    click.echo(f"webhook_secret: {webhook_secret}")
    click.echo("")
    click.echo(f"id:     {token_id}")
    click.echo(f"name:   {name}")
    click.echo(f"scopes: {','.join(scopes)}")


async def _list_tokens() -> None:
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(Token).order_by(Token.created_at)
        rows = (await session.execute(stmt)).scalars().all()

    headers = ("tok_id", "name", "scopes", "created", "last_used", "revoked", "webhook")
    widths = [len(h) for h in headers]
    table: list[tuple[str, str, str, str, str, str, str]] = []
    for r in rows:
        record = (
            r.id,
            r.name,
            ",".join(r.scopes or ()),
            _format_dt(r.created_at),
            _format_dt(r.last_used_at),
            _format_dt(r.revoked_at),
            # Surface only whether a webhook secret is set; never print the
            # secret itself even partially.
            "true" if r.webhook_secret else "false",
        )
        table.append(record)
        for i, cell in enumerate(record):
            widths[i] = max(widths[i], len(cell))

    def _row(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    click.echo(_row(headers))
    click.echo("  ".join("-" * w for w in widths))
    if not table:
        click.echo("(no tokens)")
        return
    for record in table:
        click.echo(_row(record))


async def _revoke_token(token_id: str) -> bool:
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(Token).where(Token.id == token_id)
        row = (await session.execute(stmt)).scalars().first()
        if row is None:
            return False
        if row.revoked_at is not None:
            # Idempotent: already revoked, still report success.
            return True
        row.revoked_at = func.now()  # type: ignore[assignment]
        await session.commit()
    return True


# ---- Click wiring ----------------------------------------------------------


@click.group()
def cli() -> None:
    """yt-transcript-service admin CLI."""


@cli.group()
def tokens() -> None:
    """Manage API tokens."""


@tokens.command("create")
@click.option("--name", required=True, help="Human-readable label for the token.")
@click.option(
    "--scopes",
    required=True,
    help="Comma-separated scopes (e.g. 'read,batch,summarize').",
)
def tokens_create(name: str, scopes: str) -> None:
    """Create a new API token. Plaintext is printed ONCE; only the hash is stored.

    Every token receives a webhook signing secret automatically — the
    ``--webhook`` flag is no longer required. See H5 in the review notes.
    """
    parsed = _parse_scopes(scopes)
    asyncio.run(_create_token(name=name, scopes=parsed))


@tokens.command("list")
def tokens_list() -> None:
    """List all tokens (id, name, scopes, timestamps, revoked)."""
    asyncio.run(_list_tokens())


@tokens.command("revoke")
@click.option("--id", "token_id", required=True, help="Token id (e.g. tok_xxx).")
def tokens_revoke(token_id: str) -> None:
    """Revoke a token by id. Sets ``revoked_at`` to now."""
    ok = asyncio.run(_revoke_token(token_id))
    if not ok:
        click.echo(f"token not found: {token_id}", err=True)
        sys.exit(1)
    click.echo(f"revoked: {token_id}")


if __name__ == "__main__":  # pragma: no cover
    cli()
