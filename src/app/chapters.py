"""Chapter detection (P2).

Two sources:

1. **YouTube-provided** via yt-dlp ``--skip-download --print-json`` metadata
   (already exposed by :func:`app.youtube.fetch_video_metadata`).
2. **LLM-derived** fallback when YouTube has no chapters.

:func:`get_or_compute_chapters` is the single public entry point. Idempotent:
once a row has a non-NULL ``chapters_jsonb`` value (even ``[]``), it is
returned as-is. JC-035 semantics: ``[]`` means "tried, got nothing"; do
not re-derive.

LLM derivation is best-effort. On JSON parse failure, on token-cap rejection,
or on LLM provider failure we persist ``[]`` and move on rather than 502 the
caller. The user can request transcripts without chapters either way.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import put_chapters
from app.domain import Chapter, TranscriptRecord
from app.exceptions import LLMFailedError
from app.llm import execute as llm_execute
from app.logging import get_logger
from app.youtube import fetch_video_metadata

_logger = get_logger("chapters")

# JC-036: refuse LLM derivation above ~50k tokens of context. Yt-dlp chapters
# still attempted first; if missing, persist ``[]``.
_MAX_FULLTEXT_CHARS = 200_000

# Hard upper bound on the chapter count we accept from the LLM. Keeps the
# response reasonable for UIs and guards against runaway prompts.
_MAX_CHAPTERS = 32


_SYSTEM_PROMPT = (
    "You segment a YouTube video transcript into chapters. "
    "Return JSON only — no prose, no markdown fences. "
    "Schema: {\"chapters\": [{\"start\": <seconds>, \"end\": <seconds>, \"title\": <short string>}]}. "
    "Choose 4-12 chapters with cohesive themes. "
    "First chapter starts at 0.0. Each chapter's end equals the next chapter's start. "
    "The last chapter's end equals the video's duration. Titles are short noun phrases."
)


async def fetch_yt_chapters(video_id: str) -> list[Chapter] | None:
    """Parse YouTube's published chapters via yt-dlp metadata. None if absent."""
    meta = await fetch_video_metadata(video_id)
    if not meta:
        return None
    raw = meta.get("chapters")
    if not raw:
        return None
    chapters: list[Chapter] = []
    for c in raw:
        try:
            chapters.append(
                Chapter(
                    start=float(c["start_time"]),
                    end=float(c["end_time"]),
                    title=str(c.get("title") or "Chapter").strip()[:200],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return chapters or None


def _build_user_prompt(record: TranscriptRecord) -> str:
    """Construct the LLM user prompt: transcript with [MM:SS] anchors every ~30s."""
    duration = record.duration_seconds or 0.0
    lines: list[str] = [f"Video duration: {duration:.1f} seconds.\n\nTranscript with timestamps:\n"]
    last_anchor = -30.0
    for snip in record.snippets:
        if snip.start - last_anchor >= 30.0:
            mm = int(snip.start) // 60
            ss = int(snip.start) % 60
            lines.append(f"[{mm:02d}:{ss:02d}] {snip.text}")
            last_anchor = snip.start
        else:
            lines.append(snip.text)
    return "\n".join(lines)


def _parse_llm_chapters(text: str, duration_seconds: float) -> list[Chapter]:
    """Best-effort parse of the LLM response. Returns [] on any failure."""
    # Strip code fences if the model ignores instructions
    cleaned = text.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):].lstrip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].rstrip()
    try:
        payload: dict[str, Any] = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return []
    raw = payload.get("chapters")
    if not isinstance(raw, list):
        return []
    chapters: list[Chapter] = []
    for entry in raw[:_MAX_CHAPTERS]:
        try:
            start = float(entry["start"])
            end = float(entry["end"])
            title = str(entry["title"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not title:
            continue
        if start < 0 or end <= start:
            continue
        if duration_seconds and end > duration_seconds + 5.0:
            end = duration_seconds
        chapters.append(Chapter(start=start, end=end, title=title[:200]))
    # Enforce monotonic starts; drop anything out of order.
    monotonic: list[Chapter] = []
    last_start = -1.0
    for c in chapters:
        if c.start > last_start:
            monotonic.append(c)
            last_start = c.start
    return monotonic


async def derive_chapters_from_transcript(record: TranscriptRecord) -> list[Chapter]:
    """Ask the LLM to chapter the transcript. Returns [] on any failure path."""
    if len(record.full_text) > _MAX_FULLTEXT_CHARS:
        _logger.info(
            "chapters_llm_skipped_too_long",
            video_id=record.video_id,
            full_text_chars=len(record.full_text),
        )
        return []
    user_prompt = _build_user_prompt(record)
    try:
        resp = await llm_execute(
            task="chapters",
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=1024,
            video_id=record.video_id,
        )
    except LLMFailedError as exc:
        _logger.warning("chapters_llm_failed", video_id=record.video_id, error=str(exc))
        return []
    return _parse_llm_chapters(resp.text, record.duration_seconds or 0.0)


async def get_or_compute_chapters(
    session: AsyncSession,
    record: TranscriptRecord,
) -> list[Chapter]:
    """Return chapters for a cached transcript, computing on first call.

    Resolution order:
    1. ``record.chapters is not None`` (including ``[]``) → return as-is.
    2. yt-dlp chapter metadata → persist + return.
    3. LLM derivation → persist (even ``[]``) + return.
    """
    if record.chapters is not None:
        return record.chapters

    yt_chapters = await fetch_yt_chapters(record.video_id)
    if yt_chapters is not None:
        await put_chapters(session, record.video_id, record.language, yt_chapters)
        await session.commit()
        return yt_chapters

    derived = await derive_chapters_from_transcript(record)
    # JC-035: persist ``[]`` to mean "tried, nothing useful — don't retry".
    await put_chapters(session, record.video_id, record.language, derived)
    await session.commit()
    return derived


__all__ = [
    "fetch_yt_chapters",
    "derive_chapters_from_transcript",
    "get_or_compute_chapters",
]
