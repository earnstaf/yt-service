"""On-demand summarization of a cached transcript (P2 D1).

Per spec §5.5 ``POST /v1/summarize``:
- Style-specific prompts (exec_brief, exec_deep, technical, bulleted,
  competitive_intel, custom).
- Cached by ``(video_id, style, audience_hash, custom_hash)`` for
  ``settings.summary_cache_ttl_days`` days.
- Returns a ``SummaryResult`` dataclass that the route layer wraps in
  ``SummarizeResponse`` (with per-timestamp deep links).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_transcript
from app.config import get_settings
from app.deep_links import compute_deep_link
from app.domain import TranscriptRecord
from app.exceptions import InvalidRequestError, NotFoundError
from app.llm import execute as llm_execute
from app.llm.fallback import LLMResponse
from app.logging import get_logger
from app.models import Summary

_logger = get_logger("tasks.summarize")

Style = Literal[
    "exec_brief", "exec_deep", "technical", "bulleted", "competitive_intel", "custom"
]


_STYLE_SYSTEM_PROMPTS: dict[str, str] = {
    "exec_brief": (
        "You write tight executive briefs of YouTube video transcripts. "
        "Audience: senior leaders who want the headline and 3-5 key takeaways in under 200 words. "
        "Be specific, name the speaker if known, cite numbers verbatim. "
        "Return JSON only: {\"summary\": <prose>, \"key_timestamps\": [{\"t\": <seconds>, \"label\": <short string>}]}."
    ),
    "exec_deep": (
        "You write detailed executive analyses of YouTube video transcripts. "
        "Audience: senior leaders who want a 600-1000 word analytical brief covering thesis, evidence, counter-points, and implications. "
        "Be specific, cite verbatim quotes with timestamps. "
        "Return JSON only: {\"summary\": <prose>, \"key_timestamps\": [{\"t\": <seconds>, \"label\": <short string>}]}."
    ),
    "technical": (
        "You write technical notes from YouTube video transcripts. "
        "Audience: engineers. Focus on architecture, APIs, performance numbers, gotchas. Skip business context. "
        "Return JSON only: {\"summary\": <prose>, \"key_timestamps\": [{\"t\": <seconds>, \"label\": <short string>}]}."
    ),
    "bulleted": (
        "You convert YouTube video transcripts into a bulleted summary. "
        "Each bullet is a single sentence. No more than 12 bullets. Group into 2-3 sections if helpful. "
        "Return JSON only: {\"summary\": <markdown bullets>, \"key_timestamps\": [{\"t\": <seconds>, \"label\": <short string>}]}."
    ),
    "competitive_intel": (
        "You extract competitive intelligence from YouTube video transcripts. "
        "Identify named competitors, pricing claims, product positioning, customer wins/losses. "
        "Audience: SE/RevOps team tracking the market. "
        "Return JSON only: {\"summary\": <prose>, \"key_timestamps\": [{\"t\": <seconds>, \"label\": <short string>}]}."
    ),
}


@dataclass(frozen=True, slots=True)
class KeyTimestamp:
    t: int
    label: str


@dataclass(frozen=True, slots=True)
class SummaryResult:
    video_id: str
    style: str
    audience: str
    summary: str
    key_timestamps: list[KeyTimestamp]
    provider_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    cached: bool


def _normalize(s: str) -> str:
    """Unicode-NFC normalize + lowercase + strip whitespace before hashing."""
    return unicodedata.normalize("NFC", s).strip().lower()


def _sha256(s: str) -> str:
    return hashlib.sha256(_normalize(s).encode("utf-8")).hexdigest()


def _transcript_to_prompt_text(record: TranscriptRecord) -> str:
    """Inline [MM:SS] timestamps every ~30s so the model can cite times."""
    parts: list[str] = []
    last = -30.0
    for snip in record.snippets:
        if snip.start - last >= 30.0:
            mm = int(snip.start) // 60
            ss = int(snip.start) % 60
            parts.append(f"[{mm:02d}:{ss:02d}] {snip.text}")
            last = snip.start
        else:
            parts.append(snip.text)
    return "\n".join(parts)


def _parse_summary_response(text: str) -> tuple[str, list[KeyTimestamp]]:
    """Best-effort JSON parse. On failure, treat entire text as the summary."""
    cleaned = text.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):].lstrip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].rstrip()
    try:
        payload: dict[str, Any] = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return text.strip(), []
    summary = str(payload.get("summary", text)).strip()
    raw_ts = payload.get("key_timestamps", [])
    timestamps: list[KeyTimestamp] = []
    if isinstance(raw_ts, list):
        for entry in raw_ts[:50]:
            try:
                t = int(float(entry["t"]))
                label = str(entry["label"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if label and t >= 0:
                timestamps.append(KeyTimestamp(t=t, label=label[:200]))
    return summary, timestamps


async def _lookup_cached_summary(
    session: AsyncSession,
    video_id: str,
    style: str,
    audience_hash: str,
    custom_hash: str,
) -> Summary | None:
    """Return a non-expired cached summary row, or None."""
    from sqlalchemy import func as sa_func

    stmt = (
        select(Summary)
        .where(Summary.video_id == video_id)
        .where(Summary.style == style)
        .where(Summary.audience_hash == audience_hash)
        .where(Summary.custom_hash == custom_hash)
        .where(Summary.expires_at > sa_func.now())
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _persist_summary(
    session: AsyncSession,
    *,
    video_id: str,
    style: str,
    audience_hash: str,
    custom_hash: str,
    summary: str,
    key_timestamps: list[KeyTimestamp],
    resp: LLMResponse,
) -> None:
    """Upsert into ``summaries`` table. Caller commits."""
    ttl = get_settings().summary_cache_ttl_days
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=ttl)
    values = {
        "video_id": video_id,
        "style": style,
        "audience_hash": audience_hash,
        "custom_hash": custom_hash,
        "summary": summary,
        "key_timestamps_jsonb": [{"t": kt.t, "label": kt.label} for kt in key_timestamps],
        "provider_used": f"{resp.provider}/{resp.model}",
        "tokens_in": resp.tokens_in,
        "tokens_out": resp.tokens_out,
        "cost_usd": resp.cost_usd,
        "created_at": now,
        "expires_at": expires_at,
    }
    stmt = pg_insert(Summary).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            Summary.video_id,
            Summary.style,
            Summary.audience_hash,
            Summary.custom_hash,
        ],
        set_={k: v for k, v in values.items() if k not in {"video_id", "style", "audience_hash", "custom_hash"}},
    )
    await session.execute(stmt)


async def summarize(
    session: AsyncSession,
    *,
    video_id: str,
    style: str,
    audience: str,
    custom_prompt: str | None,
    max_tokens: int,
    include_timestamps: bool,
    provider_override: str | None,
    language: str = "en",
    token_id: str | None = None,
) -> SummaryResult:
    """Compose a summary of a cached transcript. See module docstring."""
    if style == "custom":
        if not custom_prompt:
            raise InvalidRequestError("style=custom requires non-empty custom_prompt")
        system_prompt = custom_prompt
    elif style in _STYLE_SYSTEM_PROMPTS:
        system_prompt = _STYLE_SYSTEM_PROMPTS[style]
    else:
        raise InvalidRequestError(f"unknown style: {style!r}")

    audience_hash = _sha256(audience or "")
    # Mix provider_override into custom_hash so admin spot-checks never
    # collide with the cached default-provider result.
    override_marker = f"::override={provider_override}" if provider_override else ""
    custom_hash = _sha256((custom_prompt or "") + override_marker)

    # Cache hit? Skip the cache entirely when provider_override is set so the
    # admin actually exercises the requested provider every time.
    if provider_override is None:
        cached = await _lookup_cached_summary(
            session, video_id, style, audience_hash, custom_hash
        )
        if cached is not None:
            provider_used = cached.provider_used
            timestamps_raw = cached.key_timestamps_jsonb or []
            timestamps = [
                KeyTimestamp(t=int(x["t"]), label=str(x["label"])) for x in timestamps_raw
            ]
            return SummaryResult(
                video_id=video_id,
                style=style,
                audience=audience,
                summary=cached.summary,
                key_timestamps=timestamps if include_timestamps else [],
                provider_used=provider_used,
                tokens_in=int(cached.tokens_in or 0),
                tokens_out=int(cached.tokens_out or 0),
                cost_usd=Decimal(cached.cost_usd or 0),
                cached=True,
            )

    transcript = await get_transcript(session, video_id, language)
    if transcript is None:
        raise NotFoundError(
            f"transcript not cached for video_id={video_id} lang={language}; fetch /v1/transcript first"
        )

    user_prompt = (
        f"Audience: {audience or 'general technical'}\n\n"
        f"Transcript with timestamps:\n{_transcript_to_prompt_text(transcript)}"
    )

    task_name = "summarize_exec_deep" if style == "exec_deep" else "summarize"
    resp = await llm_execute(
        task=task_name,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        video_id=video_id,
        token_id=token_id,
        provider_override=provider_override,
    )

    summary, timestamps = _parse_summary_response(resp.text)
    await _persist_summary(
        session,
        video_id=video_id,
        style=style,
        audience_hash=audience_hash,
        custom_hash=custom_hash,
        summary=summary,
        key_timestamps=timestamps,
        resp=resp,
    )
    await session.commit()

    return SummaryResult(
        video_id=video_id,
        style=style,
        audience=audience,
        summary=summary,
        key_timestamps=timestamps if include_timestamps else [],
        provider_used=f"{resp.provider}/{resp.model}",
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        cost_usd=resp.cost_usd,
        cached=False,
    )


def enrich_with_deep_links(
    timestamps: list[KeyTimestamp], video_id: str
) -> list[dict[str, Any]]:
    """Convert KeyTimestamps to schema dicts including a per-entry ``deep_link``."""
    return [
        {"t": kt.t, "label": kt.label, "deep_link": compute_deep_link(video_id, kt.t)}
        for kt in timestamps
    ]


__all__ = ["summarize", "SummaryResult", "KeyTimestamp", "enrich_with_deep_links"]
