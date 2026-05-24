"""Unit tests for the LLM fallback executor (P2 A1).

Hermetic — every provider client is monkeypatched. No network or DB.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.exceptions import DailyCostCapExceededError, LLMFailedError
from app.llm import execute, fallback as fallback_mod
from app.llm.cost import compute_cost
from app.llm.fallback import LLMResponse, _reset_cost_cache_for_tests
from app.llm.routing import split_provider_model


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear daily-cost cache and stub out the DB-touching helpers."""
    _reset_cost_cache_for_tests()
    monkeypatch.setattr(fallback_mod, "_query_today_spend", AsyncMock(return_value=Decimal("0")))
    monkeypatch.setattr(fallback_mod, "_log_call", AsyncMock(return_value=None))
    # Pretend every provider has an API key so we never skip on key absence
    # unless a test overrides this.
    monkeypatch.setattr(fallback_mod, "_api_key_for", lambda provider: f"key-for-{provider}")


def _ok_response(provider: str, model: str) -> LLMResponse:
    return LLMResponse(
        text="hello",
        tokens_in=10,
        tokens_out=5,
        cost_usd=compute_cost(model, 10, 5),
        provider=provider,
        model=model,
        latency_ms=100,
    )


def test_split_provider_model_parses() -> None:
    assert split_provider_model("anthropic_direct/claude-sonnet-4-6") == (
        "anthropic_direct",
        "claude-sonnet-4-6",
    )


def test_split_provider_model_rejects_bad_format() -> None:
    with pytest.raises(ValueError):
        split_provider_model("just-model")


@pytest.mark.asyncio
async def test_execute_primary_success() -> None:
    """Happy path: primary provider returns, no fallback consulted."""
    with patch.object(
        fallback_mod, "_dispatch_one", new=AsyncMock(return_value=_ok_response("anthropic_direct", "claude-sonnet-4-6"))
    ) as mock_dispatch:
        resp = await execute(task="summarize", system_prompt="sys", user_prompt="usr")
    assert resp.provider == "anthropic_direct"
    assert mock_dispatch.await_count == 1


@pytest.mark.asyncio
async def test_execute_falls_back_on_primary_failure() -> None:
    """Primary raises → fallback chain advances → success on fallback."""
    calls: list[str] = []

    async def fake_dispatch(*, provider: str, model: str, **_: object) -> LLMResponse:
        calls.append(provider)
        if provider == "anthropic_direct":
            raise RuntimeError("provider down")
        return _ok_response(provider, model)

    with patch.object(fallback_mod, "_dispatch_one", side_effect=fake_dispatch):
        resp = await execute(task="summarize", system_prompt="sys", user_prompt="usr")
    assert resp.provider == "openai_direct"
    # primary tried, then first fallback tried
    assert calls == ["anthropic_direct", "openai_direct"]


@pytest.mark.asyncio
async def test_execute_all_fail_raises_llm_failed() -> None:
    """When every provider in the chain fails, LLMFailedError surfaces."""
    async def always_fail(**_: object) -> LLMResponse:
        raise RuntimeError("provider down")

    with patch.object(fallback_mod, "_dispatch_one", side_effect=always_fail):
        with pytest.raises(LLMFailedError) as exc_info:
            await execute(task="summarize", system_prompt="sys", user_prompt="usr")
    assert "all providers" in str(exc_info.value)


@pytest.mark.asyncio
async def test_daily_cost_cap_blocks_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost > cap → DailyCostCapExceededError (503), not LLMFailedError (502)."""
    monkeypatch.setattr(
        fallback_mod,
        "_query_today_spend",
        AsyncMock(return_value=Decimal("9999.00")),
    )
    with pytest.raises(DailyCostCapExceededError) as exc_info:
        await execute(task="summarize", system_prompt="sys", user_prompt="usr")
    assert exc_info.value.status_code == 503
    assert exc_info.value.error_code == "daily_cost_cap"


@pytest.mark.asyncio
async def test_missing_api_key_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured primary (e.g., LLMAPI without API_KEY) is skipped silently."""
    def key_for(provider: str) -> str | None:
        # llmapi is unconfigured; everything else has a key.
        if provider == "llmapi":
            return None
        return f"key-for-{provider}"

    monkeypatch.setattr(fallback_mod, "_api_key_for", key_for)

    calls: list[str] = []

    async def fake_dispatch(*, provider: str, model: str, **_: object) -> LLMResponse:
        calls.append(provider)
        return _ok_response(provider, model)

    # topics primary is llmapi (per JC-004). Without LLMAPI_API_KEY, it should
    # silently fall through to gemini_direct.
    with patch.object(fallback_mod, "_dispatch_one", side_effect=fake_dispatch):
        resp = await execute(task="topics", system_prompt="sys", user_prompt="usr")
    assert resp.provider == "gemini_direct"
    assert calls == ["gemini_direct"]  # llmapi never attempted


@pytest.mark.asyncio
async def test_provider_override_skips_fallback_chain() -> None:
    """``provider_override`` uses a single provider; no fallback on failure."""
    async def always_fail(**_: object) -> LLMResponse:
        raise RuntimeError("provider down")

    with patch.object(fallback_mod, "_dispatch_one", side_effect=always_fail) as mock_dispatch:
        with pytest.raises(LLMFailedError):
            await execute(
                task="summarize",
                system_prompt="sys",
                user_prompt="usr",
                provider_override="llmapi/claude-sonnet-4-6",
            )
    # Single attempt — no walk through the fallback list.
    assert mock_dispatch.await_count == 1


@pytest.mark.asyncio
async def test_unknown_task_raises() -> None:
    with pytest.raises(LLMFailedError):
        await execute(task="not_a_task", system_prompt="s", user_prompt="u")
