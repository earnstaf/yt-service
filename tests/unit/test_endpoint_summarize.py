"""Unit tests for POST /v1/summarize (P2 E1)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.exceptions import InsufficientScopeError
from app.tasks.summarize import KeyTimestamp, SummaryResult

from ._endpoint_helpers import build_app_with_auth_stub, client_for, make_token_stub


def _result(cached: bool = False) -> SummaryResult:
    return SummaryResult(
        video_id="OMhKgQmeMhI",
        style="exec_brief",
        audience="SE team",
        summary="The keynote covered three main themes.",
        key_timestamps=[KeyTimestamp(t=412, label="Pricing")],
        provider_used="anthropic_direct/claude-sonnet-4-6",
        tokens_in=1000,
        tokens_out=200,
        cost_usd=Decimal("0.0123"),
        cached=cached,
    )


@pytest.mark.asyncio
async def test_summarize_happy_path(monkeypatch) -> None:
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.tasks.summarize.summarize", AsyncMock(return_value=_result())
    )

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/summarize",
            json={
                "video_id": "OMhKgQmeMhI",
                "style": "exec_brief",
                "audience": "SE team",
            },
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["video_id"] == "OMhKgQmeMhI"
    assert body["style"] == "exec_brief"
    assert body["summary"].startswith("The keynote")
    # Per JC-014/S3: every key_timestamp gets a per-entry deep link.
    assert body["key_timestamps"][0]["deep_link"] == "https://youtu.be/OMhKgQmeMhI?t=412"


@pytest.mark.asyncio
async def test_summarize_provider_override_rejected_without_admin(monkeypatch) -> None:
    """Per JC-037: provider_override requires admin scope."""
    non_admin = make_token_stub(token_id="tok_nonadmin", scopes=("read", "summarize"))
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=non_admin)
    # If the override check isn't enforced this would be a 200; with it, 403.
    monkeypatch.setattr(
        "app.tasks.summarize.summarize", AsyncMock(return_value=_result())
    )

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/summarize",
            json={
                "video_id": "OMhKgQmeMhI",
                "style": "exec_brief",
                "provider_override": "anthropic_direct/claude-sonnet-4-6",
            },
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 403
    assert resp.json()["error"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_summarize_provider_override_allowed_with_admin(monkeypatch) -> None:
    """Admin tokens may pass provider_override; the override flows through."""
    admin = make_token_stub(token_id="tok_admin", scopes=("admin", "summarize"))
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=admin)
    mock_summarize = AsyncMock(return_value=_result())
    monkeypatch.setattr("app.tasks.summarize.summarize", mock_summarize)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/summarize",
            json={
                "video_id": "OMhKgQmeMhI",
                "style": "exec_brief",
                "provider_override": "anthropic_direct/claude-sonnet-4-6",
            },
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 200
    # The override value reached the task.
    kwargs = mock_summarize.await_args.kwargs
    assert kwargs["provider_override"] == "anthropic_direct/claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_summarize_invalid_provider_override_rejected_at_schema(monkeypatch) -> None:
    """Malformed provider_override fails schema validation (422 → invalid_request 400)."""
    admin = make_token_stub(scopes=("admin", "summarize"))
    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch, token=admin)

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/summarize",
            json={
                "video_id": "OMhKgQmeMhI",
                "style": "exec_brief",
                "provider_override": "not-a-provider-or-slash",
            },
            headers={"Authorization": "Bearer yt_stub"},
        )

    # Schema-level regex mismatch surfaces as the invalid_request envelope.
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_summarize_custom_style_requires_custom_prompt(monkeypatch) -> None:
    """style=custom without custom_prompt → 400 invalid_request from the task layer."""
    from app.exceptions import InvalidRequestError

    app, _, _, _ = build_app_with_auth_stub(monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.tasks.summarize.summarize",
        AsyncMock(side_effect=InvalidRequestError("style=custom requires non-empty custom_prompt")),
    )

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/summarize",
            json={"video_id": "OMhKgQmeMhI", "style": "custom"},
            headers={"Authorization": "Bearer yt_stub"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
