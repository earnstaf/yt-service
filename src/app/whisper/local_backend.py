"""Local Whisper backend via ``faster-whisper``.

CPU-bound. The dispatcher in :mod:`app.whisper` falls back here when the
OpenAI backend hits a retryable infrastructure failure (per plan P-5).

The model object is expensive to construct, so it is cached at module scope
after the first call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config import settings
from app.domain import Snippet, WhisperResult
from app.exceptions import WhisperFailedError

# Module-level singleton. Populated lazily on first call so import-time cost is
# zero and tests that never exercise this backend pay nothing.
_MODEL: Any | None = None


def _video_id_from_path(path: Path) -> str:
    """Mirror of the helper in :mod:`openai_backend` — see there for shape."""
    stem = path.stem
    if "_part_" in stem:
        stem = stem.split("_part_", 1)[0]
    return stem.split(".", 1)[0]


def _get_model() -> Any:
    """Construct (or return cached) ``faster_whisper.WhisperModel``.

    Forces ``device="cpu"`` and ``compute_type="int8"`` for predictable VPS
    behavior. Anyone running on GPU can override via the config flag once we
    add one.
    """
    global _MODEL
    if _MODEL is None:
        # Import lazily so a system without faster-whisper installed can still
        # import the package as long as it never invokes the local backend.
        from faster_whisper import WhisperModel

        _MODEL = WhisperModel(
            settings.whisper_local_model,
            device="cpu",
            compute_type="int8",
        )
    return _MODEL


def _transcribe_sync(audio_path: Path) -> WhisperResult:
    """Run faster-whisper synchronously; the public coroutine threads this."""
    model = _get_model()
    vid = _video_id_from_path(audio_path)

    segments_iter, info = model.transcribe(str(audio_path), word_timestamps=False)

    snippets: list[Snippet] = []
    text_parts: list[str] = []
    last_end = 0.0

    for segment in segments_iter:
        start = float(segment.start)
        end = float(segment.end)
        text = (segment.text or "").strip()
        snippets.append(
            Snippet(
                start=start,
                duration=max(end - start, 0.0),
                text=text,
                speaker=None,
                deep_link="",
            )
        )
        text_parts.append(text)
        last_end = max(last_end, end)

    duration = float(getattr(info, "duration", last_end) or last_end)

    return WhisperResult(
        video_id=vid,
        source="whisper_local",
        language=str(getattr(info, "language", "") or "en"),
        snippets=snippets,
        duration_seconds=duration,
        full_text=" ".join(t for t in text_parts if t),
    )


async def transcribe(audio_path: Path, video_id: str | None = None) -> WhisperResult:
    """Transcribe ``audio_path`` with the local faster-whisper model.

    Wraps the blocking call in :func:`asyncio.to_thread`. Any exception is
    folded into :class:`WhisperFailedError` because local Whisper is the last
    line of defence — there is nothing to fall back to.
    """
    try:
        result = await asyncio.to_thread(_transcribe_sync, audio_path)
    except Exception as exc:  # noqa: BLE001 — faster-whisper raises bare Exceptions
        raise WhisperFailedError(
            f"local whisper: {exc}",
            details={"path": str(audio_path)},
        ) from exc

    if video_id and result.video_id != video_id:
        # Caller knows the canonical id; respect it.
        from dataclasses import replace

        result = replace(result, video_id=video_id)
    return result
