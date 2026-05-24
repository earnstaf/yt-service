"""Topic + entity + claim extraction for a cached transcript (P4).

Spec §5.4 + §7.10. Output is a JSON dict the LLM produces; we validate the
shape, persist into the ``topics`` table, and serve from cache on subsequent
calls unless ``refresh=True``.

Routing per JC-004: ``topics`` primary = ``llmapi/gemini-2.5-flash``, with
``gemini_direct`` as first fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_transcript
from app.exceptions import NotFoundError
from app.llm import execute as llm_execute
from app.logging import get_logger
from app.models import Topic

_logger = get_logger("tasks.topics")


_SYSTEM_PROMPT = (
    "You extract structured intelligence from a YouTube video transcript. "
    "Return JSON only — no prose, no markdown fences — matching this schema:\n"
    "{\n"
    '  "topics": [string, ...],\n'
    '  "entities": {"companies": [string], "people": [string], "products": [string]},\n'
    '  "claims": [{"text": string, "approximate_timestamp_seconds": integer}],\n'
    '  "questions_raised": [string]\n'
    "}\n"
    "Topics: 3-8 high-level themes. Entities: ONLY explicitly named. "
    "Claims: factual or comparative assertions with approximate timestamps. "
    "Questions: open questions raised or implied."
)


@dataclass(frozen=True, slots=True)
class TopicResult:
    video_id: str
    topics: list[str]
    entities: dict[str, list[str]]
    claims: list[dict[str, Any]]
    questions_raised: list[str]
    provider_used: str
    cached: bool


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


def _result_from_payload(video_id: str, payload: dict[str, Any], provider: str, cached: bool) -> TopicResult:
    entities = payload.get("entities") or {}
    if not isinstance(entities, dict):
        entities = {}
    return TopicResult(
        video_id=video_id,
        topics=[str(t) for t in (payload.get("topics") or [])][:20],
        entities={
            "companies": [str(x) for x in (entities.get("companies") or [])][:50],
            "people": [str(x) for x in (entities.get("people") or [])][:50],
            "products": [str(x) for x in (entities.get("products") or [])][:50],
        },
        claims=[
            {
                "text": str(c.get("text", ""))[:1000],
                "t": int(c.get("approximate_timestamp_seconds", c.get("t", 0))),
            }
            for c in (payload.get("claims") or [])
            if isinstance(c, dict)
        ][:50],
        questions_raised=[str(q) for q in (payload.get("questions_raised") or [])][:30],
        provider_used=provider,
        cached=cached,
    )


def _transcript_to_prompt(text: str) -> str:
    if len(text) > 200_000:
        text = text[:200_000]
    return text


async def extract_topics(
    session: AsyncSession,
    *,
    video_id: str,
    refresh: bool = False,
    language: str = "en",
    token_id: str | None = None,
    provider_override: str | None = None,
) -> TopicResult:
    """Extract topics + entities + claims + questions for ``video_id``."""
    # provider_override bypasses cache so admin spot-checks always exercise the
    # requested provider.
    if not refresh and provider_override is None:
        existing = (
            await session.execute(select(Topic).where(Topic.video_id == video_id))
        ).scalar_one_or_none()
        if existing is not None:
            payload = {
                "topics": existing.topics_jsonb,
                "entities": existing.entities_jsonb,
                "claims": existing.claims_jsonb,
                "questions_raised": existing.questions_jsonb,
            }
            return _result_from_payload(video_id, payload, existing.provider_used, cached=True)

    record = await get_transcript(session, video_id, language)
    if record is None:
        raise NotFoundError(
            f"transcript not cached for {video_id!r}; fetch /v1/transcript first"
        )

    resp = await llm_execute(
        task="topics",
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_transcript_to_prompt(record.full_text),
        max_tokens=2000,
        video_id=video_id,
        token_id=token_id,
        provider_override=provider_override,
    )
    parsed = _parse_response(resp.text)
    provider = f"{resp.provider}/{resp.model}"
    result = _result_from_payload(video_id, parsed, provider, cached=False)

    # Persist (upsert by video_id).
    stmt = pg_insert(Topic).values(
        video_id=video_id,
        topics_jsonb=result.topics,
        entities_jsonb=result.entities,
        claims_jsonb=result.claims,
        questions_jsonb=result.questions_raised,
        provider_used=provider,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Topic.video_id],
        set_={
            "topics_jsonb": stmt.excluded.topics_jsonb,
            "entities_jsonb": stmt.excluded.entities_jsonb,
            "claims_jsonb": stmt.excluded.claims_jsonb,
            "questions_jsonb": stmt.excluded.questions_jsonb,
            "provider_used": stmt.excluded.provider_used,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return result


__all__ = ["extract_topics", "TopicResult"]
