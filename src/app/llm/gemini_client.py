"""Google Gemini provider client (async)."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from app.exceptions import LLMFailedError
from app.llm.cost import compute_cost

if TYPE_CHECKING:
    from app.llm.fallback import LLMResponse


_PROVIDER_NAME = "gemini_direct"


async def acomplete(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout_seconds: float,
    api_key: str,
) -> "LLMResponse":
    """Single Gemini call. SDK is sync; wrapped in ``asyncio.to_thread``."""
    import google.generativeai as genai  # noqa: PLC0415

    from app.llm.fallback import LLMResponse  # noqa: PLC0415

    genai.configure(api_key=api_key)
    model_obj = genai.GenerativeModel(model_name=model, system_instruction=system)

    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                model_obj.generate_content,
                user,
                generation_config={"max_output_tokens": max_tokens},
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMFailedError(f"gemini: {type(exc).__name__}: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    text = resp.text or ""
    usage = getattr(resp, "usage_metadata", None)
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
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
