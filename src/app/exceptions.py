"""Exception hierarchy for yt-transcript-service.

Every exception maps 1:1 to a spec §5.6 error code and HTTP status. The FastAPI
exception handler in `app.main` walks any `YTServiceError` subclass and produces
an `ErrorEnvelope` (see `to_error_envelope` below) plus the right status code.

Design notes:
- Subclasses set `error_code` and `status_code` as class attrs so the handler
  never has to import each subclass individually.
- `JobInProgressError` carries enough state for the API layer to render the
  "duplicate submit" envelope without re-querying the jobs table.
- This module imports only stdlib so A0 has no foreign imports.
"""

from __future__ import annotations

from typing import Any


class YTServiceError(Exception):
    """Base for every domain error. Subclasses MUST set `error_code` and `status_code`."""

    error_code: str = "internal_error"
    status_code: int = 500

    def __init__(self, message: str = "", details: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.error_code)
        self.message = message or self.error_code
        self.details = details


class InvalidVideoIdError(YTServiceError):
    """400 — input could not be parsed into a YouTube video id."""

    error_code = "invalid_video_id"
    status_code = 400


class InvalidRequestError(YTServiceError):
    """400 — schema/parameter validation failure."""

    error_code = "invalid_request"
    status_code = 400


class InvalidChannelError(YTServiceError):
    """400 — channel/playlist URL cannot be resolved (P3+)."""

    error_code = "invalid_channel"
    status_code = 400


class UnauthorizedError(YTServiceError):
    """401 — missing or bad bearer token."""

    error_code = "unauthorized"
    status_code = 401


class InsufficientScopeError(YTServiceError):
    """403 — token does not carry a required scope."""

    error_code = "insufficient_scope"
    status_code = 403


class FeatureDisabledError(YTServiceError):
    """403 — server feature flag is off (e.g. sentiment)."""

    error_code = "feature_disabled"
    status_code = 403


class VideoUnavailableError(YTServiceError):
    """404 — private, deleted, or region-locked video."""

    error_code = "video_unavailable"
    status_code = 404


class NoAudioStreamError(YTServiceError):
    """404 — yt-dlp could not locate an audio-only stream."""

    error_code = "no_audio"
    status_code = 404


class NotFoundError(YTServiceError):
    """404 — generic not-found (job, monitor, transcript row)."""

    error_code = "not_found"
    status_code = 404


class JobInProgressError(YTServiceError):
    """409 — duplicate Whisper submit while one is still running.

    Carries `existing_job_id` and `poll_url` so the orchestrator can convert this
    into either a 409 (explicit retry conflict path) or a 202 (normal GET path,
    per JC-016) without another DB hit.
    """

    error_code = "job_in_progress"
    status_code = 409

    def __init__(
        self,
        existing_job_id: str,
        poll_url: str,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or "job already in progress", details)
        self.existing_job_id = existing_job_id
        self.poll_url = poll_url


class VideoTooLongError(YTServiceError):
    """413 — exceeds MAX_VIDEO_DURATION_SECONDS (default 4h)."""

    error_code = "video_too_long"
    status_code = 413


class BatchTooLargeError(YTServiceError):
    """413 — more than 50 videos in one batch request."""

    error_code = "batch_too_large"
    status_code = 413


class RateLimitedError(YTServiceError):
    """429 — per-token or per-IP rate limit. Handler adds Retry-After header."""

    error_code = "rate_limited"
    status_code = 429


class YouTubeBlockedError(YTServiceError):
    """502 — `IpBlocked` from the captions library."""

    error_code = "youtube_ip_blocked"
    status_code = 502


class WhisperFailedError(YTServiceError):
    """502 — every Whisper backend (openai + local) failed."""

    error_code = "whisper_failed"
    status_code = 502


class LLMFailedError(YTServiceError):
    """502 — all LLM providers failed for an intelligence task (P2+)."""

    error_code = "llm_failed"
    status_code = 502


class QueueFullError(YTServiceError):
    """503 — queue depth above threshold; refuse new work."""

    error_code = "queue_full"
    status_code = 503


class InternalError(YTServiceError):
    """500 — unexpected; logged with full traceback by the handler."""

    error_code = "internal_error"
    status_code = 500


def to_error_envelope(exc: YTServiceError) -> dict[str, Any]:
    """Serialize a `YTServiceError` into the ErrorEnvelope dict shape.

    Mirrors `app.schemas.ErrorEnvelope`. Kept as a plain dict here so callers
    that do not want a Pydantic dependency (loggers, worker code) can still
    produce envelopes.

    Note: the ``kind`` discriminator used internally for the batch response
    union is intentionally omitted from this dict — spec §5.5 examples do
    not include it on the wire.
    """
    envelope: dict[str, Any] = {
        "error": exc.error_code,
        "message": exc.message,
        "details": exc.details,
        "job_id": None,
        "poll_url": None,
    }
    if isinstance(exc, JobInProgressError):
        envelope["job_id"] = exc.existing_job_id
        envelope["poll_url"] = exc.poll_url
    return envelope
