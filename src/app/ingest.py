"""Channel/playlist ingestion (P3 B1).

Expands a YouTube channel or playlist URL into individual video IDs, then
dispatches each video through the existing transcript orchestrator. Each
video either resolves immediately (cache hit / fresh captions) or queues a
Whisper job. The response summarizes per-video outcomes.

We deliberately do NOT batch transcripts into a single Whisper job — each
video already has independent locking and caching in P1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from app import transcript_service
from app.domain import ChannelRef, TranscriptRequest
from app.exceptions import InvalidChannelError
from app.logging import get_logger
from app.parsing import parse_channel_or_playlist
from app.youtube import expand_channel_or_playlist

_logger = get_logger("ingest")


@dataclass(frozen=True, slots=True)
class IngestVideoOutcome:
    """Per-video outcome in an ingest run."""

    video_id: str
    status: Literal["cached", "queued", "skipped", "failed"]
    job_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Aggregate result of one ingest run."""

    ingest_id: str
    source: str
    video_count: int
    videos: list[IngestVideoOutcome]


async def ingest_channel_or_playlist(
    session: AsyncSession,
    redis_client: Any,
    *,
    url: str,
    max_videos: int = 100,
    since: date | None = None,
    include: list[str] | None = None,
    language: str = "en",
    callback_url: str | None = None,
    token_id: str = "",
    whisper_rate_limit_hook=None,
) -> IngestResult:
    """Expand a channel or playlist URL and dispatch transcripts for each video.

    Returns an :class:`IngestResult` with per-video status. Does NOT wait for
    queued Whisper jobs to complete — clients use the per-video ``job_id`` to
    poll, or rely on ``callback_url`` for completion notifications (P3 monitors).

    ``whisper_rate_limit_hook`` is awaited before each per-video Whisper enqueue
    so a single ingest can't bypass the per-token Whisper bucket (codex H3 fix).
    """
    try:
        ref: ChannelRef = parse_channel_or_playlist(url)
    except InvalidChannelError:
        raise

    videos = await expand_channel_or_playlist(ref, max_videos=max_videos, since=since)
    ingest_id = f"ing_{ULID()}"
    outcomes: list[IngestVideoOutcome] = []
    include = include or []

    for vsum in videos:
        try:
            req = TranscriptRequest(
                video_id=vsum.video_id,
                language=language,
                force=None,
                wait_seconds=0,
                include=include,
                callback_url=callback_url,
                token_id=token_id,
            )
            kind, payload = await transcript_service.get_or_fetch(
                session,
                redis_client,
                req,
                token_id=token_id,
                whisper_rate_limit_hook=whisper_rate_limit_hook,
            )
            if kind == "transcript":
                outcomes.append(IngestVideoOutcome(video_id=vsum.video_id, status="cached"))
            else:
                outcomes.append(
                    IngestVideoOutcome(
                        video_id=vsum.video_id,
                        status="queued",
                        job_id=getattr(payload, "job_id", None),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ingest_video_failed", video_id=vsum.video_id, error=str(exc))
            outcomes.append(
                IngestVideoOutcome(
                    video_id=vsum.video_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    return IngestResult(
        ingest_id=ingest_id,
        source=f"{ref.kind}:{ref.value}",
        video_count=len(outcomes),
        videos=outcomes,
    )


__all__ = ["ingest_channel_or_playlist", "IngestResult", "IngestVideoOutcome"]
