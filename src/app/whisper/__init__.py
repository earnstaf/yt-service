"""Whisper dispatcher.

Single public entry point :func:`transcribe` that routes to the configured
backend and applies the fallback policy from plan P-5:

- ``settings.whisper_backend == "local"`` — call local directly.
- ``settings.whisper_backend == "openai"`` — try OpenAI first.
  - Retryable infrastructure failure (network, timeout, rate limit, 5xx)
    AND ``settings.whisper_fallback_on_openai_error`` -> fall back to local.
  - Retryable infrastructure failure AND fallback disabled ->
    :class:`WhisperFailedError`.
  - Hard misconfiguration (auth, 400) -> :class:`WhisperFailedError` directly,
    NO fallback (silent local-mode would mask the bug).

The audio file is the dispatcher's input. Downloading is the job runner's
responsibility (see :mod:`app.whisper.audio`).
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.domain import WhisperResult
from app.exceptions import WhisperFailedError
from app.whisper import local_backend, openai_backend
from app.whisper.openai_backend import _OpenAIWhisperRetryable

__all__ = ["transcribe"]


async def transcribe(audio_path: Path, video_id: str | None = None) -> WhisperResult:
    """Dispatch transcription to the configured backend.

    ``video_id`` is passed through so the resulting :class:`WhisperResult`
    carries the orchestrator's canonical id rather than relying on the
    yt-dlp filename stem.
    """
    backend = settings.whisper_backend

    if backend == "local":
        return await local_backend.transcribe(audio_path, video_id=video_id)

    # backend == "openai"
    try:
        return await openai_backend.transcribe(audio_path, video_id=video_id)
    except WhisperFailedError:
        # Hard error from OpenAI — never silently fall back per P-5.
        raise
    except _OpenAIWhisperRetryable as exc:
        if settings.whisper_fallback_on_openai_error:
            return await local_backend.transcribe(audio_path, video_id=video_id)
        raise WhisperFailedError(
            f"openai whisper failed and fallback disabled: {exc.original}",
            details={"path": str(audio_path)},
        ) from exc.original
