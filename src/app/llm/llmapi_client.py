"""LLMAPI provider client — openai-compatible base URL.

Thin shim over :func:`app.llm.openai_client.acomplete` with the LLMAPI base
URL and ``provider_label='llmapi'``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import settings
from app.llm import openai_client

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
) -> "LLMResponse":
    return await openai_client.acomplete(
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        base_url=settings.llmapi_base_url,
        provider_label="llmapi",
    )
