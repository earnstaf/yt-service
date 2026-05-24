"""Sentiment timeline for a cached transcript (P4, feature-flagged).

Spec §5.4 + §7.11. Disabled by default at the server level via
``settings.feature_sentiment`` — JC-003 flips this to true for our build.

Granularity:
- ``overall``: one score for the entire video.
- ``chapter``: score per chapter (requires include=chapters to have been computed).
- ``snippet``: score per snippet (expensive; capped at 200 snippets per call).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_transcript
from app.config import get_settings
from app.exceptions import FeatureDisabledError, NotFoundError
from app.llm import execute as llm_execute
from app.logging import get_logger
from app.models import Sentiment

_logger = get_logger("tasks.sentiment")


_SYSTEM_PROMPT = (
    "You score sentiment of YouTube video transcript segments. "
    "For each segment, return a numeric score in [-1.0, 1.0] and a label from "
    "{negative, neutral, positive}. Return JSON only:\n"
    "{\"segments\": [{\"id\": <int>, \"score\": <float>, \"label\": <string>}]}"
)


Granularity = Literal["overall", "chapter", "snippet"]


@dataclass(frozen=True, slots=True)
class SentimentResult:
    video_id: str
    granularity: str
    overall_score: float
    overall_label: str
    timeline: list[dict[str, Any]]
    provider_used: str


def _aggregate_label(score: float) -> str:
    if score > 0.2:
        return "positive"
    if score < -0.2:
        return "negative"
    return "neutral"


def _parse_segments(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):].lstrip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].rstrip()
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return []
    segs = payload.get("segments")
    return segs if isinstance(segs, list) else []


def _build_segments_prompt(record, granularity: str) -> tuple[list[dict[str, Any]], str]:
    """Build the [{id, start, end, text}] list and the user prompt."""
    segments: list[dict[str, Any]] = []
    if granularity == "overall":
        segments.append({"id": 0, "start": 0.0, "end": record.duration_seconds or 0.0, "text": record.full_text[:50_000]})
    elif granularity == "chapter":
        chapters = record.chapters or []
        if not chapters:
            # Refuse to compute chapter-granularity sentiment before chapters
            # exist. Returning an "overall" fallback would poison the cache
            # under the chapter key (codex H1 fix). Caller should request
            # /v1/transcript?include=chapters first.
            from app.exceptions import NotFoundError

            raise NotFoundError(
                "chapter-granularity sentiment requires chapters; "
                "request /v1/transcript?v=<id>&include=chapters first"
            )
        else:
            for idx, ch in enumerate(chapters):
                text = " ".join(
                    s.text for s in record.snippets if ch.start <= s.start < ch.end
                )
                segments.append({"id": idx, "start": ch.start, "end": ch.end, "text": text[:5_000]})
    else:  # snippet
        for idx, s in enumerate(record.snippets[:200]):
            segments.append(
                {"id": idx, "start": s.start, "end": s.start + s.duration, "text": s.text}
            )

    prompt_lines = ["Score the sentiment of each segment below. Return JSON.\n"]
    for s in segments:
        prompt_lines.append(f"[{s['id']}] {s['text']}")
    return segments, "\n".join(prompt_lines)


async def compute_sentiment(
    session: AsyncSession,
    *,
    video_id: str,
    granularity: Granularity = "chapter",
    language: str = "en",
    token_id: str | None = None,
) -> SentimentResult:
    """Compute sentiment timeline. Returns cached result if present."""
    if not get_settings().feature_sentiment:
        raise FeatureDisabledError("sentiment endpoint is disabled at the server level")

    existing = (
        await session.execute(
            select(Sentiment)
            .where(Sentiment.video_id == video_id)
            .where(Sentiment.granularity == granularity)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return SentimentResult(
            video_id=video_id,
            granularity=granularity,
            overall_score=float(existing.overall_score),
            overall_label=existing.overall_label,
            timeline=list(existing.timeline_jsonb or []),
            provider_used=existing.provider_used,
        )

    record = await get_transcript(session, video_id, language)
    if record is None:
        raise NotFoundError(f"transcript not cached for {video_id!r}")

    segments, user_prompt = _build_segments_prompt(record, granularity)
    resp = await llm_execute(
        task="sentiment",
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=2000,
        video_id=video_id,
        token_id=token_id,
    )
    scored = _parse_segments(resp.text)
    by_id = {int(s.get("id", -1)): s for s in scored if isinstance(s, dict)}

    timeline: list[dict[str, Any]] = []
    total_score = 0.0
    total_duration = 0.0
    for seg in segments:
        sid = seg["id"]
        scored_seg = by_id.get(sid, {})
        score = float(scored_seg.get("score", 0.0))
        label = str(scored_seg.get("label") or _aggregate_label(score))
        duration = max(0.0, float(seg["end"]) - float(seg["start"]))
        timeline.append(
            {"start": seg["start"], "end": seg["end"], "score": score, "label": label}
        )
        total_score += score * duration
        total_duration += duration

    overall_score = round(total_score / total_duration, 3) if total_duration > 0 else 0.0
    overall_label = _aggregate_label(overall_score)
    provider = f"{resp.provider}/{resp.model}"

    stmt = pg_insert(Sentiment).values(
        video_id=video_id,
        granularity=granularity,
        overall_score=overall_score,
        overall_label=overall_label,
        timeline_jsonb=timeline,
        provider_used=provider,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Sentiment.video_id, Sentiment.granularity],
        set_={
            "overall_score": stmt.excluded.overall_score,
            "overall_label": stmt.excluded.overall_label,
            "timeline_jsonb": stmt.excluded.timeline_jsonb,
            "provider_used": stmt.excluded.provider_used,
        },
    )
    await session.execute(stmt)
    await session.commit()

    return SentimentResult(
        video_id=video_id,
        granularity=granularity,
        overall_score=overall_score,
        overall_label=overall_label,
        timeline=timeline,
        provider_used=provider,
    )


__all__ = ["compute_sentiment", "SentimentResult"]
