"""Frozen domain dataclasses passed between adapters and the cache layer.

These types are the canonical in-memory shapes for transcript data. They are
deliberately separated from the Pydantic schemas in `app.schemas`:

- `Snippet`, `CaptionsResult`, `WhisperResult` are returned from external
  adapters (`app.youtube`, `app.whisper`) so those modules never import
  Pydantic.
- `TranscriptRecord` is what `app.cache` reads/writes; the API layer converts
  it to a `TranscriptResponse` (with `model_dump`) just before serialization.
- `JobPayload` is the TypedDict mirror of the `jobs.payload_jsonb` blob —
  storing the literal keys makes round-trips through Postgres lossless.

Slots are on so accidental attribute assignment fails loudly at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict

# Source of a transcript row. Whisper sources are kept distinct so the API
# layer can surface which backend actually produced the text.
TranscriptSource = Literal["youtube_captions", "whisper_openai", "whisper_local"]


@dataclass(frozen=True, slots=True)
class Snippet:
    """A single caption / Whisper segment.

    `speaker` is `None` until diarization (P2) populates it. `deep_link` is
    populated in P1 by `app.deep_links.with_deep_links` once the orchestrator
    knows the video id.
    """

    start: float
    duration: float
    text: str
    speaker: str | None = None
    deep_link: str = ""


@dataclass(frozen=True, slots=True)
class Chapter:
    """A chapter span. Only populated when P2 chapter detection runs."""

    start: float
    end: float
    title: str


@dataclass(frozen=True, slots=True)
class CaptionsResult:
    """Output of `app.youtube.fetch_captions`. `None` snippets list means no caption track."""

    video_id: str
    language: str
    is_generated: bool
    snippets: list[Snippet]
    duration_seconds: float | None
    full_text: str


@dataclass(frozen=True, slots=True)
class WhisperResult:
    """Output of `app.whisper.transcribe`. Always has a duration (we measured the audio)."""

    video_id: str
    source: Literal["whisper_openai", "whisper_local"]
    language: str
    snippets: list[Snippet]
    duration_seconds: float
    full_text: str


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """Canonical cached transcript shape — what `app.cache` reads/writes.

    `chapters` and `has_diarization` are reserved for P2. `cached_at` is the
    persisted `fetched_at` column, not a fresh now() — equality compares
    cleanly across cache reads.
    """

    video_id: str
    language: str
    source: TranscriptSource
    is_generated: bool
    duration_seconds: float | None
    snippet_count: int
    cached_at: datetime
    snippets: list[Snippet]
    full_text: str
    chapters: list[Chapter] | None = None
    has_diarization: bool = False


class JobPayload(TypedDict):
    """Exact JSON shape stored in `jobs.payload_jsonb` for Whisper jobs.

    Keys are stable across phases — `webhook` delivery, status polling, and
    worker entrypoints all read this blob.
    """

    video_id: str
    language: str
    force_whisper: bool
    include: list[str]
    callback_url: str | None


@dataclass(frozen=True, slots=True)
class TranscriptRequest:
    """Typed value object passed to `transcript_service.get_or_fetch` (P1 D3).

    Lives here so the orchestrator (`app.transcript_service`) and the route
    handler (`app.main`) agree on the field set without circular imports.
    """

    video_id: str
    language: str = "en"
    force: Literal["whisper", "refresh"] | None = None
    wait_seconds: int = 0
    include: list[str] = field(default_factory=list)
    callback_url: str | None = None
    token_id: str | None = None
