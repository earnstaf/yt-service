"""Structural diff between two cached transcripts (P4).

Spec §5.4 + §7.12. Both videos must already be cached. Uses the LLM to
identify topics added/removed/shifted plus key quotes from each side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_transcript
from app.exceptions import NotFoundError
from app.llm import execute as llm_execute
from app.logging import get_logger

_logger = get_logger("tasks.diff")


Focus = Literal["topics_and_emphasis", "exact_changes", "competitive_positioning"]


_SYSTEM_PROMPTS: dict[str, str] = {
    "topics_and_emphasis": (
        "Compare two YouTube video transcripts. Identify topics added in B, "
        "topics removed from A, and topics whose emphasis shifted. Return JSON "
        "only:\n"
        "{\n"
        '  "added_in_b": [{"topic": <str>, "evidence": <short quote>}],\n'
        '  "removed_from_a": [{"topic": <str>, "evidence": <short quote>}],\n'
        '  "shifted_emphasis": [{"topic": <str>, "direction": "more"|"less", "delta_pct": <int>}],\n'
        '  "key_quotes_a": [<short quote>],\n'
        '  "key_quotes_b": [<short quote>],\n'
        '  "executive_summary": <2-4 sentence prose>\n'
        "}"
    ),
    "exact_changes": (
        "Compare two YouTube video transcripts. Identify exact textual changes "
        "between matched sections (renames, deletions, insertions). Use the same "
        "JSON schema as topics_and_emphasis."
    ),
    "competitive_positioning": (
        "Compare two YouTube video transcripts focused on competitive product/market "
        "positioning. Highlight how each side positions itself relative to the other. "
        "Use the same JSON schema as topics_and_emphasis."
    ),
}


@dataclass(frozen=True, slots=True)
class DiffResult:
    video_a: str
    video_b: str
    focus: str
    added_in_b: list[dict[str, Any]]
    removed_from_a: list[dict[str, Any]]
    shifted_emphasis: list[dict[str, Any]]
    key_quotes_a: list[str]
    key_quotes_b: list[str]
    executive_summary: str
    provider_used: str


def _parse_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):].lstrip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].rstrip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return {}


async def diff_transcripts(
    session: AsyncSession,
    *,
    video_a: str,
    video_b: str,
    focus: Focus = "topics_and_emphasis",
    language: str = "en",
    token_id: str | None = None,
    provider_override: str | None = None,
) -> DiffResult:
    """Compute a structural diff between two cached transcripts."""
    record_a = await get_transcript(session, video_a, language)
    if record_a is None:
        raise NotFoundError(f"transcript not cached for {video_a!r}; fetch /v1/transcript first")
    record_b = await get_transcript(session, video_b, language)
    if record_b is None:
        raise NotFoundError(f"transcript not cached for {video_b!r}; fetch /v1/transcript first")

    system_prompt = _SYSTEM_PROMPTS.get(focus, _SYSTEM_PROMPTS["topics_and_emphasis"])
    # Naive prompt: feed both transcripts in. Map-reduce for very long inputs
    # is a P4+ optimization; for v1 we cap each side at 80k chars.
    text_a = record_a.full_text[:80_000]
    text_b = record_b.full_text[:80_000]
    dur_a = f"{record_a.duration_seconds:.0f}s" if record_a.duration_seconds else "unknown"
    dur_b = f"{record_b.duration_seconds:.0f}s" if record_b.duration_seconds else "unknown"
    user_prompt = (
        f"Video A (id={video_a}, duration={dur_a}):\n{text_a}\n\n"
        f"---\n\nVideo B (id={video_b}, duration={dur_b}):\n{text_b}"
    )

    resp = await llm_execute(
        task="diff",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=2500,
        video_id=f"{video_a}|{video_b}",
        token_id=token_id,
        provider_override=provider_override,
    )
    parsed = _parse_response(resp.text)

    return DiffResult(
        video_a=video_a,
        video_b=video_b,
        focus=focus,
        added_in_b=list(parsed.get("added_in_b") or []),
        removed_from_a=list(parsed.get("removed_from_a") or []),
        shifted_emphasis=list(parsed.get("shifted_emphasis") or []),
        key_quotes_a=[str(q) for q in (parsed.get("key_quotes_a") or [])][:20],
        key_quotes_b=[str(q) for q in (parsed.get("key_quotes_b") or [])][:20],
        executive_summary=str(parsed.get("executive_summary") or ""),
        provider_used=f"{resp.provider}/{resp.model}",
    )


__all__ = ["diff_transcripts", "DiffResult"]
