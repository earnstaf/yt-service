"""OpenAI provider client (async). Also serves the LLMAPI openai-compatible base."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING

from app.exceptions import LLMFailedError
from app.llm.cost import compute_cost

if TYPE_CHECKING:
    from app.llm.fallback import LLMResponse


async def acomplete(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout_seconds: float,
    api_key: str,
    base_url: str | None = None,
    provider_label: str = "openai_direct",
) -> "LLMResponse":
    """Single OpenAI (or openai-compatible) call. ``base_url`` overrides for LLMAPI."""
    import openai  # noqa: PLC0415

    from app.llm.fallback import LLMResponse  # noqa: PLC0415

    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
    started = time.monotonic()
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMFailedError(f"{provider_label}: {type(exc).__name__}: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    text = resp.choices[0].message.content or ""
    usage = resp.usage
    tokens_in = usage.prompt_tokens if usage else 0
    tokens_out = usage.completion_tokens if usage else 0
    cost: Decimal = compute_cost(model, tokens_in, tokens_out)

    return LLMResponse(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        provider=provider_label,
        model=model,
        latency_ms=elapsed_ms,
    )
