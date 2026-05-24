"""Speaker diarization via pyannote.audio (P2 C1).

Hard requirements:

- ``HUGGINGFACE_TOKEN`` must be set and the user must have accepted the gated
  terms for ``pyannote/segmentation-3.0`` and ``pyannote/speaker-diarization-3.1``
  on huggingface.co. If either is missing, diarization is marked unavailable
  and the job fails fast with a clear error.
- Per JC-032, diarization only runs on whisper-source transcripts. Captions-
  sourced transcripts get a refusal: their timestamps may not align with a
  freshly downloaded audio file.

Public functions:

- :func:`is_available` — cheap check used by callers to short-circuit.
- :func:`diarize` — async wrapper around the pyannote pipeline.
- :func:`run_diarization_job` — RQ worker entry point used by
  :mod:`app.worker` (the pipeline glue).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config import settings
from app.domain import Snippet
from app.logging import get_logger

_logger = get_logger("diarization")

# Module-level lazy pipeline. Initialized on first call to keep import cheap
# and to defer the heavy torch import until a worker actually needs it.
_pipeline: Any | None = None
_pipeline_load_failed: str | None = None


def _load_pipeline() -> Any | None:
    """Lazy-load the pyannote pipeline. Returns ``None`` if unavailable."""
    global _pipeline, _pipeline_load_failed
    if _pipeline is not None:
        return _pipeline
    if _pipeline_load_failed is not None:
        return None

    token = settings.huggingface_token
    if not token:
        _pipeline_load_failed = "HUGGINGFACE_TOKEN not configured"
        _logger.warning("diarization_unavailable", reason=_pipeline_load_failed)
        return None

    try:
        from pyannote.audio import Pipeline  # noqa: PLC0415 — heavy lazy import
    except Exception as exc:  # noqa: BLE001
        _pipeline_load_failed = f"pyannote import failed: {exc}"
        _logger.warning("diarization_unavailable", reason=_pipeline_load_failed)
        return None

    try:
        _pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=token,
        )
    except Exception as exc:  # noqa: BLE001
        _pipeline_load_failed = (
            "pyannote model load failed (accept gated terms on huggingface.co): " f"{exc}"
        )
        _logger.warning("diarization_unavailable", reason=_pipeline_load_failed)
        return None
    return _pipeline


def is_available() -> bool:
    """True iff the diarization pipeline can be loaded right now."""
    return _load_pipeline() is not None


def _align_speakers(snippets: list[Snippet], turns: list[tuple[str, float, float]]) -> list[Snippet]:
    """Assign speaker labels to snippets via interval overlap.

    Each turn ``(speaker_label, start, end)`` is matched against snippets
    whose ``[start, start+duration]`` overlaps. A 200ms tolerance is applied
    on both ends. Snippets with no overlapping turn keep ``speaker=None``.
    """
    tol = 0.2
    new_snippets: list[Snippet] = []
    for s in snippets:
        s_start = s.start - tol
        s_end = s.start + s.duration + tol
        best_label: str | None = None
        best_overlap = 0.0
        for label, t_start, t_end in turns:
            overlap = max(0.0, min(s_end, t_end) - max(s_start, t_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = label
        if best_label is not None:
            new_snippets.append(
                Snippet(
                    start=s.start,
                    duration=s.duration,
                    text=s.text,
                    speaker=best_label,
                    deep_link=s.deep_link,
                )
            )
        else:
            new_snippets.append(s)
    return new_snippets


async def diarize(audio_path: Path, snippets: list[Snippet]) -> list[Snippet]:
    """Run pyannote on ``audio_path`` and return snippets with speakers set.

    Raises ``RuntimeError`` if the pipeline isn't available — callers should
    have checked :func:`is_available` first.
    """
    pipeline = _load_pipeline()
    if pipeline is None:
        raise RuntimeError(_pipeline_load_failed or "diarization unavailable")

    def _run() -> list[tuple[str, float, float]]:
        annotation = pipeline(str(audio_path))
        # Annotation.itertracks(yield_label=True) yields (Segment, _, label)
        return [
            (str(label), float(seg.start), float(seg.end))
            for seg, _, label in annotation.itertracks(yield_label=True)
        ]

    turns = await asyncio.to_thread(_run)
    return _align_speakers(snippets, turns)


__all__ = ["diarize", "is_available"]
