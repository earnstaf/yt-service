"""Anthropic provider client (async).

Wraps the official ``anthropic`` SDK in the canonical ``LLMResponse`` shape.
The SDK handles retries internally; we add a single per-call timeout and
let the fallback executor catch retryable failures.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING

from app.exceptions import LLMFailedError
from app.llm.cost import compute_cost

if TYPE_CHECKING:
    from app.llm.fallback import LLMResponse


_PROVIDER_NAME = "anthropic_direct"


async def acomplete(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout_seconds: float,
    api_key: str,
) -> "LLMResponse":
    """Single Anthropic call. Raises on any error; caller handles fallback."""
    import anthropic  # noqa: PLC0415 — lazy so missing dep doesn't break import

    from app.llm.fallback import LLMResponse  # noqa: PLC0415 — avoid cycle

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)
    started = time.monotonic()
    try:
        msg = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001 — surface to fallback layer
        raise LLMFailedError(f"anthropic: {type(exc).__name__}: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    text = "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")
    tokens_in = msg.usage.input_tokens
    tokens_out = msg.usage.output_tokens
    cost: Decimal = compute_cost(model, tokens_in, tokens_out)

    return LLMResponse(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        provider=_PROVIDER_NAME,
        model=model,
        latency_ms=elapsed_ms,
    )
