"""LLM fallback executor + canonical ``LLMResponse`` dataclass.

Every callsite in the app reaches an LLM via :func:`execute`. Responsibilities:

- Resolve the task → provider/model list via :data:`app.llm.routing.TASK_ROUTING`.
- Skip providers whose API key env is empty (LLMAPI may legitimately be absent).
- Per-call timeout (60s default), single attempt per provider — the fallback
  IS the retry strategy.
- Daily cost cap check (JC-033/-034): sum of ``llm_call_log.cost_usd`` for
  the current UTC date; raise :class:`DailyCostCapExceededError` (503) if
  over. Cached 60s per worker process — approximate, documented.
- On success: write a row to ``llm_call_log``, increment
  ``yt_llm_cost_usd_total`` and ``yt_llm_calls_total{status=ok}``.
- On all-failed: raise :class:`LLMFailedError` (502).
- ``provider_override`` (admin-only at the route): single shot, no fallback chain.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.db import get_session_factory
from app.exceptions import DailyCostCapExceededError, LLMFailedError
from app.llm import anthropic_client, gemini_client, llmapi_client, openai_client
from app.llm.providers import PROVIDERS
from app.llm.routing import TASK_ROUTING, split_provider_model
from app.logging import get_logger
from app.metrics import LLM_CALLS, LLM_COST_USD, LLM_LATENCY
from app.models import LLMCallLog

_logger = get_logger("llm.fallback")

_DEFAULT_TIMEOUT = 60.0
_DEFAULT_MAX_TOKENS = 2048

# 60s per-worker cache of (date_utc_iso, total_cost). JC-034.
_cost_cache: dict[str, tuple[float, Decimal]] = {}
_COST_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Pinned response shape every provider client returns. See P2 A1 plan."""

    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    provider: str
    model: str
    latency_ms: int


def _api_key_for(provider: str) -> str | None:
    """Return the configured API key for a provider, or None if missing/empty."""
    settings = get_settings()
    entry = PROVIDERS.get(provider, {})
    env_name = entry.get("api_key_env", "")
    if not env_name:
        return None
    # Settings fields are lowercased versions of the env var.
    field = env_name.lower()
    value = getattr(settings, field, None)
    if value is None:
        return None
    value = str(value)
    return value if value else None


async def _check_daily_cost_cap() -> None:
    """Raise :class:`DailyCostCapExceededError` if today's spend > cap.

    Uses an in-process 60s cache to avoid hammering Postgres on every call.
    Approximate at worker boundaries — documented limitation (JC-034).
    """
    settings = get_settings()
    cap = Decimal(str(settings.max_daily_llm_cost_usd))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached = _cost_cache.get(today)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _COST_CACHE_TTL_SECONDS:
        spent = cached[1]
    else:
        spent = await _query_today_spend()
        _cost_cache[today] = (now, spent)
    if spent > cap:
        # Seconds until next UTC midnight — used by the route layer as Retry-After.
        from datetime import timedelta

        now_utc = datetime.now(timezone.utc)
        next_midnight = datetime.combine(
            (now_utc + timedelta(days=1)).date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        retry_after = int((next_midnight - now_utc).total_seconds())
        raise DailyCostCapExceededError(
            f"daily LLM cost cap reached: spent ${spent} > cap ${cap}",
            details={
                "spent_usd": str(spent),
                "cap_usd": str(cap),
                "retry_after": retry_after,
            },
        )


async def _query_today_spend() -> Decimal:
    """Sum ``llm_call_log.cost_usd`` for the current UTC date.

    Computes the UTC day bounds in Python so we don't depend on the database
    session timezone (which JC-034 says must be UTC).
    """
    from datetime import timedelta

    factory = get_session_factory()
    today_utc = datetime.now(timezone.utc).date()
    start = datetime.combine(today_utc, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    async with factory() as session:
        stmt = select(func.coalesce(func.sum(LLMCallLog.cost_usd), 0)).where(
            LLMCallLog.ts >= start, LLMCallLog.ts < end
        )
        total = (await session.execute(stmt)).scalar_one()
        return Decimal(str(total))


async def _log_call(
    *,
    task: str,
    provider: str,
    model: str,
    status: str,
    latency_ms: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: Decimal,
    video_id: str | None,
    token_id: str | None,
    error: str | None,
) -> None:
    """Insert a row in ``llm_call_log``. Own session per call (decoupled from request)."""
    factory = get_session_factory()
    async with factory() as session:
        row = LLMCallLog(
            task=task,
            provider=provider,
            model=model,
            status=status,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            video_id=video_id,
            token_id=token_id,
            error=error,
        )
        session.add(row)
        await session.commit()


async def _dispatch_one(
    *,
    provider: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
    api_key: str,
) -> LLMResponse:
    """Route a single attempt to the right provider client wrapper."""
    common = dict(
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        timeout_seconds=timeout,
        api_key=api_key,
    )
    if provider == "anthropic_direct":
        return await anthropic_client.acomplete(**common)
    if provider == "openai_direct":
        return await openai_client.acomplete(**common, provider_label="openai_direct")
    if provider == "gemini_direct":
        return await gemini_client.acomplete(**common)
    if provider == "llmapi":
        return await llmapi_client.acomplete(**common)
    raise LLMFailedError(f"unknown provider: {provider!r}")


async def execute(
    *,
    task: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    timeout_seconds: float = _DEFAULT_TIMEOUT,
    video_id: str | None = None,
    token_id: str | None = None,
    provider_override: str | None = None,
) -> LLMResponse:
    """Execute an LLM task with provider fallback and observability.

    Raises:
        DailyCostCapExceededError: today's spend > ``max_daily_llm_cost_usd``.
        LLMFailedError: every provider in the chain failed or was unconfigured.
    """
    await _check_daily_cost_cap()

    if provider_override is not None:
        chain = [provider_override]
    else:
        route = TASK_ROUTING.get(task)
        if route is None:
            raise LLMFailedError(f"no routing defined for task: {task!r}")
        chain = [route["primary"], *route["fallbacks"]]

    last_error: str | None = None
    for entry in chain:
        try:
            provider, model = split_provider_model(entry)
        except ValueError as exc:
            last_error = str(exc)
            continue

        api_key = _api_key_for(provider)
        if api_key is None:
            # LLMAPI is optional; everything else missing means misconfig
            # but we still try the next entry. The chain typically has at
            # least one configured provider.
            _logger.info("llm_provider_skipped_no_key", provider=provider, task=task)
            continue

        started = time.monotonic()
        try:
            resp = await _dispatch_one(
                provider=provider,
                model=model,
                system=system_prompt,
                user=user_prompt,
                max_tokens=max_tokens,
                timeout=timeout_seconds,
                api_key=api_key,
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            last_error = f"{type(exc).__name__}: {exc}"
            LLM_CALLS.labels(task=task, provider=provider, model=model, status="error").inc()
            LLM_LATENCY.labels(provider=provider, model=model).observe(latency_ms / 1000)
            await _log_call(
                task=task,
                provider=provider,
                model=model,
                status="error",
                latency_ms=latency_ms,
                tokens_in=None,
                tokens_out=None,
                cost_usd=Decimal("0"),
                video_id=video_id,
                token_id=token_id,
                error=last_error,
            )
            _logger.warning(
                "llm_call_failed",
                task=task,
                provider=provider,
                model=model,
                error=last_error,
            )
            continue

        # Success.
        LLM_CALLS.labels(task=task, provider=provider, model=model, status="ok").inc()
        LLM_COST_USD.labels(provider=provider, task=task).inc(float(resp.cost_usd))
        LLM_LATENCY.labels(provider=provider, model=model).observe(resp.latency_ms / 1000)
        await _log_call(
            task=task,
            provider=provider,
            model=model,
            status="ok",
            latency_ms=resp.latency_ms,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            cost_usd=resp.cost_usd,
            video_id=video_id,
            token_id=token_id,
            error=None,
        )
        # Invalidate cached daily spend so subsequent callers see the new cost.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _cost_cache.pop(today, None)
        return resp

    raise LLMFailedError(
        "all providers failed or unconfigured",
        details={"task": task, "last_error": last_error or "no providers tried"},
    )


__all__ = ["execute", "LLMResponse"]


def _reset_cost_cache_for_tests() -> None:
    """Test helper — wipe the in-process cost cache between tests."""
    _cost_cache.clear()
