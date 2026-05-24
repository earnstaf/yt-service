"""Provider registry per spec §7.3.

Each entry describes a logical provider name (NOT a model). The model is
chosen at the routing layer (:mod:`app.llm.routing`). The ``optional`` flag
marks providers whose API key may legitimately be absent — they are silently
skipped in fallback chains rather than treated as errors.
"""

from __future__ import annotations

from typing import TypedDict


class ProviderEntry(TypedDict, total=False):
    type: str
    base_url: str
    api_key_env: str
    optional: bool


PROVIDERS: dict[str, ProviderEntry] = {
    "anthropic_direct": {
        "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openai_direct": {
        "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "gemini_direct": {
        "type": "gemini",
        "api_key_env": "GEMINI_API_KEY",
    },
    "llmapi": {
        "type": "openai_compatible",
        "base_url": "https://api.llmapi.ai/v1",
        "api_key_env": "LLMAPI_API_KEY",
        "optional": True,
    },
}
