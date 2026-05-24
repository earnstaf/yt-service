"""Task → provider/model routing table.

Per spec §7.3 with the JC-004 deviation: ``topics`` primary is LLMAPI's
gemini-2.5-flash (aggressive cost optimization on a low-stakes task) with
gemini_direct as first fallback. Everything else keeps the spec defaults.

Each value is a ``RouteEntry``: a primary plus an ordered fallback list. The
fallback executor (:func:`app.llm.fallback.execute`) walks the list left-to-
right on retryable failures and bubbles ``LLMFailedError`` if every entry
fails or has no API key.

The table lands in full now (P2) so P4 only adds tasks/prompts, not routing.
"""

from __future__ import annotations

from typing import TypedDict


class RouteEntry(TypedDict):
    primary: str
    fallbacks: list[str]


TASK_ROUTING: dict[str, RouteEntry] = {
    "chapters": {
        "primary": "gemini_direct/gemini-2.5-flash",
        "fallbacks": [
            "anthropic_direct/claude-sonnet-4-6",
            "openai_direct/gpt-4o-mini",
        ],
    },
    "summarize": {
        "primary": "anthropic_direct/claude-sonnet-4-6",
        "fallbacks": [
            "openai_direct/gpt-4o",
            "llmapi/claude-sonnet-4-6",
        ],
    },
    "summarize_exec_deep": {
        "primary": "anthropic_direct/claude-opus-4-7",
        "fallbacks": [
            "openai_direct/gpt-4o",
            "llmapi/claude-opus-4-7",
        ],
    },
    # JC-004: LLMAPI primary for topics. Other tasks keep direct-API primary.
    "topics": {
        "primary": "llmapi/gemini-2.5-flash",
        "fallbacks": [
            "gemini_direct/gemini-2.5-flash",
            "openai_direct/gpt-4o-mini",
        ],
    },
    "sentiment": {
        "primary": "gemini_direct/gemini-2.5-flash",
        "fallbacks": [
            "llmapi/gemini-2.5-flash",
        ],
    },
    "diff": {
        "primary": "anthropic_direct/claude-sonnet-4-6",
        "fallbacks": [
            "openai_direct/gpt-4o",
            "llmapi/claude-sonnet-4-6",
        ],
    },
}


def split_provider_model(entry: str) -> tuple[str, str]:
    """Parse a ``provider/model`` string into ``(provider, model)`` parts.

    Raises ``ValueError`` if the format is wrong. Used by the fallback
    executor and by ``provider_override`` validation.
    """
    if "/" not in entry:
        raise ValueError(f"expected 'provider/model', got {entry!r}")
    provider, model = entry.split("/", 1)
    if not provider or not model:
        raise ValueError(f"expected 'provider/model', got {entry!r}")
    return provider, model
