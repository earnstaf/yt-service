"""Pydantic v2 request/response models for every P1 endpoint.

Each model corresponds to a payload in spec §5.5 (endpoint contracts). The
batch response union is discriminated by an extra `kind` field — `"transcript"`,
`"job_accepted"`, or `"error"` — so clients can dispatch on a single string.

Datetime fields are always serialized as UTC ISO 8601 with a `Z` suffix to
match the spec's example payloads (`"2026-05-20T14:02:11Z"`). We use a
per-field `field_serializer` so callers can pass either naive UTC or
timezone-aware datetimes and the output is deterministic.

Note: this module does NOT use `from __future__ import annotations`. Pydantic
v2 reads `Literal[...]` types eagerly during model construction, and the
PEP-563 deferred evaluation has been known to break discriminator detection.
"""

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _to_z_iso(value: datetime | None) -> str | None:
    """Render a datetime as `YYYY-MM-DDTHH:MM:SSZ` (or `...Z` with fractional).

    Naive datetimes are assumed UTC. Aware datetimes are converted to UTC first.
    Returns None pass-through so optional fields stay optional.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        aware = value.replace(tzinfo=timezone.utc)
    else:
        aware = value.astimezone(timezone.utc)
    # Drop the `+00:00` Python emits and append the spec's `Z` suffix.
    return aware.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Snippet + Chapter (used inside TranscriptResponse)
# ---------------------------------------------------------------------------


class TranscriptSnippetOut(BaseModel):
    """One transcript line in the API response. Mirrors spec §5.5 200 example."""

    model_config = ConfigDict(extra="forbid")

    start: float
    duration: float
    text: str
    speaker: str | None = None
    deep_link: str

    @field_validator("start", "duration", mode="before")
    @classmethod
    def _coerce_numeric(cls, value: Any) -> Any:
        """Accept ints from JSON; Pydantic v2 keeps strict typing otherwise."""
        if isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        return value


class ChapterOut(BaseModel):
    """Chapter span. Always null in P1 responses; structure reserved for P2."""

    model_config = ConfigDict(extra="forbid")

    start: float
    end: float
    title: str

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_numeric(cls, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        return value


# ---------------------------------------------------------------------------
# /v1/transcript responses
# ---------------------------------------------------------------------------


class TranscriptResponse(BaseModel):
    """200 response for GET /v1/transcript. Field order matches spec §5.5."""

    model_config = ConfigDict(extra="forbid")

    video_id: str
    source: Literal["youtube_captions", "whisper_openai", "whisper_local"]
    language: str
    is_generated: bool
    duration_seconds: float | None
    snippet_count: int
    cached_at: datetime
    cache_hit: bool
    chapters: list[ChapterOut] | None = None
    snippets: list[TranscriptSnippetOut]
    full_text: str
    # P2 enrichment status fields (JC-031): present only when include=speakers
    # is requested and diarization isn't pre-computed.
    diarization_status: Literal["queued", "captions_source_unsupported"] | None = None
    diarization_job_id: str | None = None
    has_diarization: bool | None = None
    # `kind` is the discriminator for the BatchResponseItem union. We exclude
    # it from the wire JSON (spec §5.5 examples do not include it) but keep
    # the field on the model so TypeAdapter / discriminated union validation
    # still works internally. See H10.
    kind: Literal["transcript"] = Field(default="transcript", exclude=True)

    @field_serializer("cached_at")
    def _ser_cached_at(self, value: datetime) -> str:
        rendered = _to_z_iso(value)
        # cached_at is required, so _to_z_iso never returns None here.
        assert rendered is not None  # noqa: S101 (defensive, always true)
        return rendered


class JobAcceptedResponse(BaseModel):
    """202 response for GET /v1/transcript (or batch item) when a job is queued/running."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["queued", "running", "complete", "failed"]
    video_id: str
    poll_url: str
    estimated_seconds: int
    # P2: explicit job_type so polling clients can distinguish whisper vs
    # enrichment jobs. Defaults to "whisper" for back-compat with P1 clients.
    job_type: Literal["whisper", "enrichment"] = "whisper"
    kind: Literal["job_accepted"] = Field(default="job_accepted", exclude=True)


class JobStatusResponse(BaseModel):
    """GET /v1/jobs/{job_id} response. Joins the `jobs` table.

    `video_id_resolved` is the parsed canonical id (the request may have used
    a URL). `transcript_url` is populated once status == "complete" and points
    at the cached transcript route so polling clients can pivot.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    video_id: str
    job_type: Literal[
        "captions",
        "whisper",
        "enrichment",
        "ingest",
        "summarize",
        "topics",
        "sentiment",
        "diff",
    ]
    status: Literal["queued", "running", "complete", "failed"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    video_id_resolved: str | None = None
    transcript_url: str | None = None

    @field_serializer("started_at", "finished_at")
    def _ser_dt(self, value: datetime | None) -> str | None:
        return _to_z_iso(value)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class ErrorEnvelope(BaseModel):
    """Uniform error payload. `job_id` and `poll_url` populated only for 409 `job_in_progress`."""

    model_config = ConfigDict(extra="forbid")

    error: str
    message: str
    details: dict[str, Any] | None = None
    job_id: str | None = None
    poll_url: str | None = None
    kind: Literal["error"] = Field(default="error", exclude=True)


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


# Discriminated union — each branch carries a unique literal `kind` so clients
# can switch on a single string instead of probing fields.
BatchResponseItem = Annotated[
    Union[TranscriptResponse, JobAcceptedResponse, ErrorEnvelope],
    Field(discriminator="kind"),
]


class BatchRequest(BaseModel):
    """POST /v1/transcript:batch request.

    Note on the 50-video cap: the spec returns 413 `batch_too_large` for >50.
    A pydantic `ValueError` here surfaces as FastAPI 422; the outer exception
    handler in `app.main` rewrites the 422 into a 413 `batch_too_large`
    envelope. Keeping the limit in the schema means we never reach route logic
    with an oversize list.
    """

    model_config = ConfigDict(extra="forbid")

    videos: list[str]
    lang: str = "en"
    include: list[str] = Field(default_factory=list)
    callback_url: str | None = None

    @field_validator("videos")
    @classmethod
    def _cap_video_count(cls, value: list[str]) -> list[str]:
        if len(value) > 50:
            raise ValueError("batch_too_large: max 50 videos per request")
        if len(value) == 0:
            raise ValueError("invalid_request: videos list must not be empty")
        return value


class BatchResponse(BaseModel):
    """Wrapper around the per-video discriminated union."""

    model_config = ConfigDict(extra="forbid")

    items: list[BatchResponseItem]


# ---------------------------------------------------------------------------
# Cache + health
# ---------------------------------------------------------------------------


class CacheStatsResponse(BaseModel):
    """GET /v1/cache/stats response."""

    model_config = ConfigDict(extra="forbid")

    total_rows: int
    by_source: dict[str, int]
    oldest_cached_at: datetime | None = None
    newest_cached_at: datetime | None = None

    @field_serializer("oldest_cached_at", "newest_cached_at")
    def _ser_dt(self, value: datetime | None) -> str | None:
        return _to_z_iso(value)


class CachePurgeResponse(BaseModel):
    """DELETE /v1/cache/{video_id} response."""

    model_config = ConfigDict(extra="forbid")

    video_id: str
    rows_deleted: int


class HealthResponse(BaseModel):
    """GET /readyz response. `/healthz` returns a tiny fixed dict; this is for the deep check."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "unhealthy"]
    checks: dict[str, str]


# ---------------------------------------------------------------------------
# /v1/summarize (P2)
# ---------------------------------------------------------------------------


_PROVIDER_OVERRIDE_RE = r"^(anthropic_direct|openai_direct|gemini_direct|llmapi)/[\w\-.]+$"


class SummarizeRequest(BaseModel):
    """POST /v1/summarize body per spec §5.5."""

    model_config = ConfigDict(extra="forbid")

    video_id: str
    style: Literal[
        "exec_brief", "exec_deep", "technical", "bulleted", "competitive_intel", "custom"
    ] = "exec_brief"
    audience: str = ""
    custom_prompt: str | None = None
    max_tokens: int = 800
    include_timestamps: bool = True
    # Admin-only. The route enforces ``require_scopes("admin")`` when this is set.
    provider_override: str | None = Field(default=None, pattern=_PROVIDER_OVERRIDE_RE)

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(cls, v: int) -> int:
        if v < 1 or v > 8000:
            raise ValueError("invalid_request: max_tokens must be in [1, 8000]")
        return v


class KeyTimestampOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: int
    label: str
    deep_link: str


class SummarizeResponse(BaseModel):
    """POST /v1/summarize response per spec §5.5."""

    model_config = ConfigDict(extra="forbid")

    video_id: str
    style: str
    audience: str
    summary: str
    key_timestamps: list[KeyTimestampOut]
    provider_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    cached: bool


__all__ = [
    "TranscriptSnippetOut",
    "ChapterOut",
    "TranscriptResponse",
    "JobAcceptedResponse",
    "JobStatusResponse",
    "ErrorEnvelope",
    "BatchRequest",
    "BatchResponse",
    "BatchResponseItem",
    "CacheStatsResponse",
    "CachePurgeResponse",
    "HealthResponse",
    "SummarizeRequest",
    "SummarizeResponse",
    "KeyTimestampOut",
]
