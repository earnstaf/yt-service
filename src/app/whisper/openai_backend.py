"""OpenAI Whisper backend.

Calls the OpenAI ``audio.transcriptions`` endpoint. Files larger than
``settings.whisper_chunk_bytes`` are split first by :mod:`app.whisper.audio`
and re-stitched with proper time offsets.

Error policy (per plan P-5 / pinned H-10):
- Retryable (network, timeouts, rate limits, 5xx) -> :class:`_OpenAIWhisperRetryable`
  so the dispatcher can fall back to local.
- Hard misconfiguration (401 auth, 400 bad request) -> :class:`WhisperFailedError`
  directly so the dispatcher does NOT silently mask the issue with local Whisper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import openai

from app.config import settings
from app.domain import Snippet, WhisperResult
from app.exceptions import WhisperFailedError
from app.whisper import audio as audio_utils


class _OpenAIWhisperRetryable(Exception):
    """Signal to the dispatcher that an OpenAI failure is fallback-eligible.

    Internal to the Whisper package; never leaks out of
    :mod:`app.whisper.__init__` as a public exception type.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def _video_id_from_path(path: Path) -> str:
    """Recover the yt-dlp-style video id from a downloaded audio filename.

    yt-dlp writes ``<id>.<ext>`` (or ``<id>_part_NNN.<ext>`` for split parts),
    so the id is the first dot-segment of the stem with any ``_part_*`` suffix
    stripped.
    """
    stem = path.stem
    if "_part_" in stem:
        stem = stem.split("_part_", 1)[0]
    return stem.split(".", 1)[0]


def _client() -> openai.AsyncOpenAI:
    """Lazily-built OpenAI async client.

    Reads ``settings.openai_api_key`` at call time so tests can override env
    after import. The client itself is cheap to construct so no caching.
    """
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)


def _segments_from_response(resp: object, time_offset: float) -> tuple[list[Snippet], float, str]:
    """Convert an OpenAI ``verbose_json`` response into snippets.

    Returns ``(snippets, last_end_seconds, text)``. ``time_offset`` is added to
    each segment start so chunked responses align with the original timeline.
    """
    segments = getattr(resp, "segments", None) or []
    snippets: list[Snippet] = []
    last_end = time_offset
    text_parts: list[str] = []
    for seg in segments:
        # ``seg`` is a pydantic model on modern SDKs and a dict on older ones —
        # support both shapes.
        start = float(_getattr_or_item(seg, "start", 0.0))
        end = float(_getattr_or_item(seg, "end", start))
        text = str(_getattr_or_item(seg, "text", "")).strip()
        duration = max(end - start, 0.0)
        snippets.append(
            Snippet(
                start=start + time_offset,
                duration=duration,
                text=text,
                speaker=None,
                deep_link="",
            )
        )
        text_parts.append(text)
        last_end = max(last_end, end + time_offset)
    return snippets, last_end, " ".join(text_parts)


def _getattr_or_item(obj: object, key: str, default: object) -> object:
    """Read ``key`` from ``obj`` as attribute first, then mapping item."""
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


async def _transcribe_part(client: openai.AsyncOpenAI, part: Path) -> object:
    """Call the OpenAI transcription endpoint for one file.

    Raises :class:`WhisperFailedError` on auth/400 errors and
    :class:`_OpenAIWhisperRetryable` on transient infrastructure errors.
    """
    try:
        with part.open("rb") as fh:
            return await client.audio.transcriptions.create(
                model=settings.whisper_openai_model,
                file=fh,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
    except (openai.AuthenticationError, openai.BadRequestError) as exc:
        raise WhisperFailedError(
            f"openai whisper: {exc}",
            details={"part": str(part)},
        ) from exc
    except (
        openai.RateLimitError,
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.InternalServerError,
    ) as exc:
        raise _OpenAIWhisperRetryable(exc) from exc


async def transcribe(audio_path: Path, video_id: str | None = None) -> WhisperResult:
    """Transcribe ``audio_path`` via the OpenAI Whisper API.

    The file is split locally if it exceeds ``settings.whisper_chunk_bytes``;
    per-part responses are stitched with monotonically-increasing offsets.
    """
    vid = video_id or _video_id_from_path(audio_path)
    client = _client()

    # split_audio is sync and may call ffmpeg; run it off the event loop so a
    # long split does not block.
    try:
        parts = await asyncio.to_thread(
            audio_utils.split_audio, audio_path, settings.whisper_chunk_bytes
        )
    except RuntimeError as exc:
        raise WhisperFailedError(
            f"openai whisper: split failed: {exc}",
            details={"path": str(audio_path)},
        ) from exc

    all_snippets: list[Snippet] = []
    all_text: list[str] = []
    duration_acc = 0.0
    language: str = ""

    try:
        for part in parts:
            resp = await _transcribe_part(client, part)
            if not language:
                language = str(_getattr_or_item(resp, "language", "") or "")
            snippets, last_end, text = _segments_from_response(resp, time_offset=duration_acc)
            all_snippets.extend(snippets)
            all_text.append(text)
            # Advance the offset by the part's duration when available; fall back to
            # ``last_end - duration_acc`` so the next chunk's t=0 sits at the right
            # spot in the merged timeline.
            part_duration = _getattr_or_item(resp, "duration", None)
            if isinstance(part_duration, (int, float)):
                duration_acc += float(part_duration)
            else:
                duration_acc = last_end

        return WhisperResult(
            video_id=vid,
            source="whisper_openai",
            language=language or "en",
            snippets=all_snippets,
            duration_seconds=duration_acc,
            full_text=" ".join(t for t in all_text if t),
        )
    finally:
        # M1: clean up split part files. The original ``audio_path`` is
        # excluded — the worker's finally-block owns its cleanup. Only the
        # ``_part_NNN`` children produced by ``split_audio`` are removed here.
        for part in parts:
            if part == audio_path:
                continue
            try:
                audio_utils.cleanup(part)
            except Exception:  # noqa: BLE001 — cleanup must never mask the real error
                pass
