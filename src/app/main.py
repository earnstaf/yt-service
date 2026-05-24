"""FastAPI application factory and route wiring for yt-transcript-service.

The factory pattern keeps the app constructable from tests (each test can
build its own instance and install ``dependency_overrides`` without leaking
state between modules). Production code imports the module-level ``app``
singleton that ``create_app()`` builds once at import time.

Lifespan responsibilities:

- ``configure_logging`` at startup so subsequent module-level loggers
  inherit the JSON processor chain.
- :func:`bootstrap_admin_token` once per process; harmless no-op when the
  env var is unset or an admin row already exists.
- :func:`close_db` and :func:`close_redis` on shutdown so the engine and
  connection pool drain cleanly.

Error handling is centralized: every :class:`app.exceptions.YTServiceError`
maps to a JSON ``ErrorEnvelope`` via :func:`to_error_envelope`. Pydantic
:class:`ValidationError` (also FastAPI's :class:`RequestValidationError`)
becomes a 400 ``invalid_request`` envelope, except for the batch
``batch_too_large`` validator message which is rewritten to 413. Anything
else is logged at ERROR with full traceback and surfaces as a 500
``internal_error`` envelope.
"""

from __future__ import annotations

import asyncio
import ipaddress
import traceback
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, chapters as chapters_mod, ingest as ingest_mod, jobs, monitors as monitors_mod, serialization, transcript_service
from app.parsing import parse_channel_or_playlist
from app.tasks import summarize as summarize_task
from app.auth import bootstrap_admin_token, require_scopes
from app.config import get_settings
from app.db import check_db_health, close_db, get_session, get_session_factory
from app.deep_links import with_deep_links
from app.domain import TranscriptRequest
from app.exceptions import (
    BatchTooLargeError,
    InvalidRequestError,
    InvalidVideoIdError,
    NotFoundError,
    RateLimitedError,
    YTServiceError,
    to_error_envelope,
)
from app.logging import configure_logging, get_logger
from app.metrics import content_type, render_metrics
from app.middleware import AccessLogMiddleware, enforce_rate_limit
from app.parsing import parse_video_id
from app.redis_client import check_redis_health, close_redis, get_redis_client
from app.schemas import (
    BatchRequest,
    BatchResponse,
    CachePurgeResponse,
    CacheStatsResponse,
    ErrorEnvelope,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    IngestVideoOutcomeOut,
    JobAcceptedResponse,
    JobStatusResponse,
    MonitorCreateRequest,
    MonitorResponse,
    SummarizeRequest,
    SummarizeResponse,
    TranscriptResponse,
    TranscriptSnippetOut,
)
from app.exceptions import InsufficientScopeError

_logger = get_logger("main")

# Hosts (or rendered IPs) treated as loopback for the metrics gate.
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001 — FastAPI signature
    """Configure logging, run bootstrap, and drain connections on shutdown."""
    settings = get_settings()
    configure_logging(settings.yt_log_level)
    _logger.info("startup", env=settings.yt_env)

    # Bootstrap admin token in its own session — never block startup on it.
    try:
        factory = get_session_factory()
        async with factory() as session:
            await bootstrap_admin_token(session)
    except Exception as exc:  # pragma: no cover — startup-only, exercised by integration env
        _logger.warning("bootstrap_admin_token_failed", error=str(exc))

    yield

    await close_db()
    await close_redis()
    _logger.info("shutdown_complete")


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


def _envelope_response(exc: YTServiceError) -> JSONResponse:
    """Render a YTServiceError as JSONResponse, adding Retry-After when present."""
    from app.exceptions import DailyCostCapExceededError  # noqa: PLC0415 — local

    headers: dict[str, str] = {}
    if isinstance(exc, (RateLimitedError, DailyCostCapExceededError)):
        retry_after = (exc.details or {}).get("retry_after")
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=exc.status_code,
        content=to_error_envelope(exc),
        headers=headers or None,
    )


async def _yt_service_exception_handler(request: Request, exc: YTServiceError) -> JSONResponse:  # noqa: ARG001
    """Handler for every domain error. Maps to the JSON envelope shape."""
    return _envelope_response(exc)


def _validation_error_to_envelope(exc: Exception) -> JSONResponse:
    """Translate a Pydantic / FastAPI validation error into the right envelope.

    JC-016 / spec §5.6: a ``batch_too_large`` validator message must surface
    as a 413 ``batch_too_large`` envelope, not 422/400. Every other validation
    failure is a 400 ``invalid_request``.
    """
    errors: list[dict[str, Any]] = []
    if isinstance(exc, RequestValidationError | ValidationError):
        errors = list(exc.errors())
    serialized = _sanitize_validation_errors(errors)
    if any("batch_too_large" in str(err.get("msg", "")) for err in errors):
        domain_exc: YTServiceError = BatchTooLargeError(
            "batch contains more than 50 videos",
            details={"errors": serialized},
        )
    else:
        domain_exc = InvalidRequestError(
            "request failed validation",
            details={"errors": serialized},
        )
    return _envelope_response(domain_exc)


def _sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip non-JSON-serializable values (e.g. ``ctx.error`` ValueError instances).

    Pydantic v2 surfaces the raw exception object inside ``ctx``. We keep
    only string-safe fields so :class:`JSONResponse` can serialize the
    envelope.
    """
    safe: list[dict[str, Any]] = []
    for err in errors:
        clean: dict[str, Any] = {}
        for key, value in err.items():
            if key == "ctx" and isinstance(value, dict):
                clean[key] = {k: str(v) for k, v in value.items()}
            elif isinstance(value, BaseException):
                clean[key] = str(value)
            elif isinstance(value, tuple):
                clean[key] = list(value)
            else:
                clean[key] = value
        safe.append(clean)
    return safe


async def _request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
    """FastAPI's built-in body-validation hook. Wired so 422 → 400/413 envelopes."""
    return _validation_error_to_envelope(exc)


async def _validation_exception_handler(request: Request, exc: ValidationError) -> JSONResponse:  # noqa: ARG001
    """Catch raw Pydantic ValidationError raised outside FastAPI's body parser."""
    return _validation_error_to_envelope(exc)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    """Anything not in the YT hierarchy becomes a 500 ``internal_error``.

    Logs the traceback at ERROR via structlog. Never reveals exception detail
    to the caller — the message is the static string ``"internal error"``.
    """
    _logger.error(
        "unhandled_exception",
        exc_type=type(exc).__name__,
        traceback=traceback.format_exc(),
    )
    envelope = {
        "error": "internal_error",
        "message": "internal error",
        "details": None,
        "job_id": None,
        "poll_url": None,
    }
    return JSONResponse(status_code=500, content=envelope)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_include(raw: str | None) -> list[str]:
    """Split a comma list into a clean ``list[str]``. Empty input → empty list."""
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _monitor_to_response(monitor) -> MonitorResponse:  # noqa: ANN001 — ORM Monitor
    """Map a Monitor ORM row to the wire response."""
    return MonitorResponse(
        id=monitor.id,
        channel_id=monitor.channel_id,
        channel_url=monitor.channel_url,
        poll_interval_minutes=monitor.poll_interval_minutes,
        include=list(monitor.include_jsonb or []),
        callback_url=monitor.callback_url,
        notes=monitor.notes,
        last_polled_at=monitor.last_polled_at,
        last_video_id=monitor.last_video_id,
        created_by=monitor.created_by,
        created_at=monitor.created_at,
        paused=monitor.paused,
    )


async def _merge_chapters(
    session: AsyncSession,
    response: TranscriptResponse,
) -> TranscriptResponse:
    """Populate ``response.chapters`` by computing/caching as needed (P2 E2)."""
    record = await cache.get_transcript(session, response.video_id, response.language)
    if record is None:
        return response
    chapters_list = await chapters_mod.get_or_compute_chapters(session, record)
    from app.schemas import ChapterOut  # local to avoid circular import at top

    return response.model_copy(
        update={
            "chapters": [
                ChapterOut(start=c.start, end=c.end, title=c.title) for c in chapters_list
            ]
        }
    )


async def _merge_speakers(
    session: AsyncSession,
    redis_client: Any,
    response: TranscriptResponse,
    token_id: str,
) -> TranscriptResponse:
    """Annotate the response with diarization status (JC-031).

    Branches:

    - Already diarized in cache → ``has_diarization=True``.
    - Captions-source transcript → ``diarization_status="captions_source_unsupported"``,
      no enqueue.
    - Whisper-source, undiarized → enqueue an enrichment job, ``diarization_status="queued"``,
      ``diarization_job_id=<id>``.
    """
    record = await cache.get_transcript(session, response.video_id, response.language)
    if record is None:
        return response

    if record.has_diarization:
        return response.model_copy(update={"has_diarization": True})

    if record.source == "youtube_captions":
        return response.model_copy(
            update={
                "has_diarization": False,
                "diarization_status": "captions_source_unsupported",
            }
        )

    # Whisper source, no diarization yet. Idempotent enqueue (SETNX lock).
    from app.exceptions import JobInProgressError  # noqa: PLC0415 — local

    try:
        job = await jobs.enqueue_diarization(
            session,
            redis_client,
            video_id=response.video_id,
            token_id=token_id,
            language=response.language,
        )
        return response.model_copy(
            update={
                "has_diarization": False,
                "diarization_status": "queued",
                "diarization_job_id": job.job_id,
            }
        )
    except JobInProgressError as exc:
        if exc.existing_job_id:
            return response.model_copy(
                update={
                    "has_diarization": False,
                    "diarization_status": "queued",
                    "diarization_job_id": exc.existing_job_id,
                }
            )
        # Stale lock without a recoverable job_id — log + surface unset.
        _logger.warning("diarization_lock_stuck", video_id=response.video_id)
        return response.model_copy(update={"has_diarization": False})
    except Exception as exc:  # noqa: BLE001 — diarization is best-effort
        _logger.warning(
            "diarization_enqueue_failed",
            video_id=response.video_id,
            error=str(exc),
        )
        return response.model_copy(update={"has_diarization": False})


def _host_is_loopback(host: str | None) -> bool:
    """Return True iff ``host`` resolves syntactically to a loopback name/IP."""
    if not host:
        return False
    candidate = host.strip()
    if not candidate:
        return False
    if candidate in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _client_host_is_loopback(request: Request) -> bool:
    """Return True if the *effective* client identity for the request is loopback.

    Trust model (H7):

    - The immediate peer (``request.client.host``) is the only host we can
      believe without external coordination. If it is NOT loopback, we IGNORE
      ``X-Forwarded-For`` / ``X-Real-IP`` entirely — those headers are
      attacker-controllable across an untrusted hop and cannot upgrade a
      non-loopback caller into a loopback one.
    - When the peer IS loopback (i.e. a same-host reverse proxy), forwarded
      headers are honored. The *forwarded* client IP then determines the
      decision: if XFF/XRI carries a non-loopback origin, deny — the
      proxy is forwarding a public request.
    - When the peer is loopback and no forwarded headers are present, allow.
    """
    client = request.client
    peer = client.host if client else None
    peer_is_loopback = _host_is_loopback(peer)

    xff = request.headers.get("x-forwarded-for")
    xri = request.headers.get("x-real-ip")

    if not peer_is_loopback:
        # Untrusted peer — its forwarded headers are not authoritative.
        return False

    if xff or xri:
        # Peer is a trusted same-host proxy. The forwarded client identity
        # decides the call. Take the first hop from XFF (canonical for
        # "the originating client") and fall back to XRI.
        forwarded_candidate: str | None = None
        if xff:
            first_hop = xff.split(",")[0].strip()
            if first_hop:
                forwarded_candidate = first_hop
        if forwarded_candidate is None and xri:
            forwarded_candidate = xri.strip()
        return _host_is_loopback(forwarded_candidate)

    return True


def _snippets_with_links(response: TranscriptResponse) -> list[TranscriptSnippetOut]:
    """Return the response snippets, populating deep_link if any is missing.

    Snippets returned by ``transcript_service`` already carry deep links; this
    is a defensive backstop for callers that bypass that path (none in P1).
    """
    return list(response.snippets)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _build_v1_router() -> APIRouter:
    """Construct the /v1 router. Pulled out so tests can introspect routes."""
    router = APIRouter(prefix="/v1")

    @router.get("/transcript", response_model=None)
    async def get_transcript(
        request: Request,
        v: str = Query(..., description="Video ID or any YouTube URL form"),
        lang: str = Query("en"),
        format: Literal["json", "text", "srt"] = Query("json"),
        force: Literal["whisper", "refresh"] | None = Query(None),
        wait: int = Query(0, ge=0),
        include: str | None = Query(None, description="Comma-separated include tokens"),
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("read")),
    ) -> Response:
        """Return a transcript, queueing a Whisper job if needed.

        Wraps :func:`app.transcript_service.get_or_fetch`. Status code 200 for
        cache/captions hits, 202 for a queued Whisper job. ``format=text``
        returns ``text/plain`` and ``format=srt`` returns ``application/x-subrip``.
        """
        try:
            video_id = parse_video_id(v)
        except InvalidVideoIdError:
            raise
        await enforce_rate_limit("read", request, redis_client)

        ts_request = TranscriptRequest(
            video_id=video_id,
            language=lang,
            force=force,
            wait_seconds=wait,
            include=_parse_include(include),
            callback_url=None,
            token_id=getattr(token, "id", None),
        )

        async def _whisper_rate_limit() -> None:
            """Apply the Whisper rate-limit bucket at the actual enqueue point."""
            await enforce_rate_limit("whisper", request, redis_client)

        kind, payload = await transcript_service.get_or_fetch(
            session,
            redis_client,
            ts_request,
            token_id=getattr(token, "id", ""),
            whisper_rate_limit_hook=_whisper_rate_limit,
        )
        if kind == "transcript":
            assert isinstance(payload, TranscriptResponse)
            request.state.source = payload.source
            request.state.cache_hit = payload.cache_hit
            include_tokens = _parse_include(include)
            # P2 enrichment merge after the orchestrator returns. Route layer
            # owns this so transcript_service stays focused on transcript
            # availability (plan E2 + JC-031).
            if "chapters" in include_tokens:
                payload = await _merge_chapters(session, payload)
            if "speakers" in include_tokens:
                payload = await _merge_speakers(
                    session, redis_client, payload, token_id=getattr(token, "id", "")
                )
            if format == "text":
                return PlainTextResponse(payload.full_text)
            if format == "srt":
                srt_snippets = with_deep_links(
                    [
                        # Convert pydantic snippets back into domain snippets so the
                        # serializer signature stays clean. Deep links are already
                        # populated; this re-call is idempotent.
                        _snippet_pydantic_to_domain(s)
                        for s in payload.snippets
                    ],
                    payload.video_id,
                )
                return PlainTextResponse(
                    serialization.to_srt(srt_snippets),
                    media_type="application/x-subrip",
                )
            # Drop unset P2 diarization fields so P1 clients don't see new
            # ``diarization_status: null`` etc. on every response. We use
            # `exclude_unset=True` so only fields explicitly set by
            # ``_merge_speakers`` (or returned from the cache as True) appear.
            return JSONResponse(
                status_code=200,
                content=payload.model_dump(mode="json", exclude_unset=True),
            )
        # job_accepted
        assert isinstance(payload, JobAcceptedResponse)
        return JSONResponse(
            status_code=202,
            content=payload.model_dump(mode="json", exclude_unset=True),
        )

    @router.post("/transcript:batch", response_model=None)
    async def post_transcript_batch(
        request: Request,
        body: BatchRequest,
        session: AsyncSession = Depends(get_session),  # noqa: ARG001 — kept for parity / future shared reads
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("batch")),
    ) -> JSONResponse:
        """Resolve up to 50 videos in one round trip.

        Each video is independent: a failure for one returns an
        :class:`ErrorEnvelope` in its slot rather than failing the whole
        batch. Items run in parallel via ``asyncio.gather`` — each task
        opens its OWN ``AsyncSession`` because SQLAlchemy's AsyncSession is
        NOT concurrency-safe (sharing one across `gather()` tasks corrupts
        the connection state). See B4 in the review notes.
        """
        await enforce_rate_limit("batch", request, redis_client)

        # H11: validate every callback URL once, up-front, so a single
        # disallowed target fails the batch fast rather than after parallel
        # work has begun. Importing here keeps the validator out of the
        # request-handler module's import graph.
        if body.callback_url is not None:
            from app.url_safety import validate_callback_url  # noqa: PLC0415 — lazy

            try:
                validate_callback_url(body.callback_url)
            except YTServiceError as exc:
                return _envelope_response(exc)

        factory = get_session_factory()

        async def _one(raw: str) -> Any:
            try:
                vid = parse_video_id(raw)
            except YTServiceError as exc:
                return to_error_envelope(exc)
            async def _whisper_rate_limit_item() -> None:
                await enforce_rate_limit("whisper", request, redis_client)

            try:
                async with factory() as item_session:
                    kind, payload = await transcript_service.get_or_fetch(
                        item_session,
                        redis_client,
                        TranscriptRequest(
                            video_id=vid,
                            language=body.lang,
                            include=list(body.include),
                            callback_url=body.callback_url,
                            token_id=getattr(token, "id", None),
                        ),
                        token_id=getattr(token, "id", ""),
                        whisper_rate_limit_hook=_whisper_rate_limit_item,
                    )
                if kind == "transcript":
                    return payload.model_dump(mode="json")
                return payload.model_dump(mode="json")
            except YTServiceError as exc:
                return to_error_envelope(exc)
            except Exception as exc:  # pragma: no cover — defensive
                _logger.error("batch_item_unhandled", error=str(exc), video=raw)
                return {
                    "error": "internal_error",
                    "message": "internal error",
                    "details": None,
                    "job_id": None,
                    "poll_url": None,
                }

        items = await asyncio.gather(*[_one(v) for v in body.videos])
        # H10: spec §5.5 returns a bare array of per-video objects, not
        # ``{"items": [...]}``. FastAPI / Starlette serializes a top-level
        # list cleanly.
        return JSONResponse(status_code=200, content=items)

    @router.get("/jobs/{job_id}", response_model=JobStatusResponse)
    async def get_job_status(
        job_id: str,
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("read")),  # noqa: ARG001
    ) -> JobStatusResponse:
        """Return the current state of ``job_id`` or 404 if unknown."""
        await enforce_rate_limit("read", request, redis_client)
        job = await jobs.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"job {job_id!r} not found")
        transcript_url: str | None = None
        if job.status == "complete":
            transcript_url = f"/v1/transcript?v={job.video_id}"
        return JobStatusResponse(
            job_id=job.job_id,
            video_id=job.video_id,
            job_type=job.job_type,
            status=job.status,
            started_at=job.started_at,
            finished_at=job.finished_at,
            error=job.error,
            video_id_resolved=job.video_id,
            transcript_url=transcript_url,
        )

    @router.delete("/cache/{video_id}", response_model=CachePurgeResponse)
    async def delete_cache(
        video_id: str,
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("admin")),  # noqa: ARG001
    ) -> CachePurgeResponse:
        """Admin-only cache purge for a single video. Returns the rowcount."""
        await enforce_rate_limit("read", request, redis_client)
        parsed = parse_video_id(video_id)
        rows = await cache.purge_transcript(session, parsed)
        await session.commit()
        return CachePurgeResponse(video_id=parsed, rows_deleted=rows)

    @router.get("/cache/stats", response_model=CacheStatsResponse)
    async def get_cache_stats(
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("read")),  # noqa: ARG001
    ) -> CacheStatsResponse:
        """Aggregate cache stats. See :class:`CacheStatsResponse`."""
        await enforce_rate_limit("read", request, redis_client)
        data = await cache.stats(session)
        return CacheStatsResponse(**data)

    @router.post("/summarize", response_model=SummarizeResponse)
    async def post_summarize(
        body: SummarizeRequest,
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("summarize")),
    ) -> SummarizeResponse:
        """Summarize a cached transcript. P2.

        ``provider_override`` requires admin scope (JC-037). The route applies
        the additional check inline so the dependency stays simple.
        """
        await enforce_rate_limit("summarize", request, redis_client)
        if body.provider_override is not None:
            token_scopes = getattr(token, "scopes", None) or []
            if "admin" not in token_scopes:
                raise InsufficientScopeError("provider_override requires admin scope")

        parsed = parse_video_id(body.video_id)
        result = await summarize_task.summarize(
            session,
            video_id=parsed,
            style=body.style,
            audience=body.audience,
            custom_prompt=body.custom_prompt,
            max_tokens=body.max_tokens,
            include_timestamps=body.include_timestamps,
            provider_override=body.provider_override,
            token_id=getattr(token, "id", None),
        )
        enriched = summarize_task.enrich_with_deep_links(result.key_timestamps, parsed)
        return SummarizeResponse(
            video_id=result.video_id,
            style=result.style,
            audience=result.audience,
            summary=result.summary,
            key_timestamps=enriched,
            provider_used=result.provider_used,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=float(result.cost_usd),
            cached=result.cached,
        )

    # -----------------------------------------------------------------------
    # P3: /v1/ingest and /v1/monitors
    # -----------------------------------------------------------------------

    @router.post("/ingest", response_model=IngestResponse)
    async def post_ingest(
        body: IngestRequest,
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("batch")),
    ) -> IngestResponse:
        """Expand a channel/playlist URL into per-video transcript dispatch (P3 B1)."""
        from datetime import date as _date

        await enforce_rate_limit("batch", request, redis_client)
        since: _date | None = None
        if body.since:
            try:
                y, m, d = body.since.split("-")
                since = _date(int(y), int(m), int(d))
            except (ValueError, TypeError) as exc:
                from app.exceptions import InvalidRequestError

                raise InvalidRequestError(f"since must be YYYY-MM-DD: {body.since!r}") from exc

        if body.callback_url:
            from app.url_safety import validate_callback_url

            validate_callback_url(body.callback_url)

        async def _whisper_rate_limit() -> None:
            await enforce_rate_limit("whisper", request, redis_client)

        result = await ingest_mod.ingest_channel_or_playlist(
            session,
            redis_client,
            url=body.url,
            max_videos=body.max_videos,
            since=since,
            include=body.include,
            callback_url=body.callback_url,
            token_id=getattr(token, "id", ""),
            whisper_rate_limit_hook=_whisper_rate_limit,
        )
        return IngestResponse(
            ingest_id=result.ingest_id,
            source=result.source,
            video_count=result.video_count,
            videos=[
                IngestVideoOutcomeOut(
                    video_id=v.video_id,
                    status=v.status,
                    job_id=v.job_id,
                    error=v.error,
                )
                for v in result.videos
            ],
        )

    @router.post("/monitors", response_model=MonitorResponse)
    async def post_monitor(
        body: MonitorCreateRequest,
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("monitor")),
    ) -> MonitorResponse:
        """Register a channel/playlist for scheduled polling (P3 C1)."""
        await enforce_rate_limit("monitor_create", request, redis_client)
        ref = parse_channel_or_playlist(body.channel_url)
        if ref.kind == "playlist":
            from app.exceptions import InvalidRequestError

            raise InvalidRequestError("monitors track channels, not playlists; pass a channel url")
        from app.url_safety import validate_callback_url
        from app.youtube import resolve_channel_id

        validate_callback_url(body.callback_url)

        # YouTube's RSS feed requires a UC... channel ID. Resolve handles now
        # so the scheduler never holds a non-canonical id (codex H1 fix).
        if ref.kind == "channel_id":
            channel_id = ref.value
        else:
            channel_id = await resolve_channel_id(ref)
            if not channel_id:
                from app.exceptions import InvalidChannelError

                raise InvalidChannelError(
                    f"could not resolve channel ID for {body.channel_url!r}"
                )

        monitor = await monitors_mod.create_monitor(
            session,
            channel_id=channel_id,
            channel_url=body.channel_url,
            poll_interval_minutes=body.poll_interval_minutes,
            include=body.include,
            callback_url=body.callback_url,
            notes=body.notes,
            created_by=getattr(token, "id", ""),
        )
        return _monitor_to_response(monitor)

    @router.get("/monitors", response_model=list[MonitorResponse])
    async def list_monitors_route(
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("read")),  # noqa: ARG001
    ) -> list[MonitorResponse]:
        await enforce_rate_limit("read", request, redis_client)
        rows = await monitors_mod.list_monitors(session, include_paused=False)
        return [_monitor_to_response(m) for m in rows]

    @router.delete("/monitors/{monitor_id}", response_model=None, status_code=204)
    async def delete_monitor_route(
        monitor_id: str,
        request: Request,
        session: AsyncSession = Depends(get_session),
        redis_client: Any = Depends(get_redis_client),
        token: Any = Depends(require_scopes("monitor")),  # noqa: ARG001
    ) -> Response:
        await enforce_rate_limit("monitor_create", request, redis_client)
        await monitors_mod.delete_monitor(session, monitor_id)
        return Response(status_code=204)

    return router


def _snippet_pydantic_to_domain(snippet: TranscriptSnippetOut):  # noqa: ANN202
    """Convert an API snippet model back into the :class:`app.domain.Snippet` shape.

    Lets the SRT serializer accept the same dataclass shape it does for cache
    rows without forcing a separate ``to_srt_from_models`` variant.
    """
    from app.domain import Snippet  # local import to keep top-of-file lean

    return Snippet(
        start=snippet.start,
        duration=snippet.duration,
        text=snippet.text,
        speaker=snippet.speaker,
        deep_link=snippet.deep_link,
    )


# ---------------------------------------------------------------------------
# Meta routes
# ---------------------------------------------------------------------------


def _attach_meta_routes(app: FastAPI) -> None:
    """Attach ``/healthz``, ``/readyz``, and ``/metrics`` outside the /v1 prefix."""

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe: cheap, never touches deps. Always 200 when the app runs."""
        return {"status": "ok"}

    @app.get("/readyz", response_model=HealthResponse)
    async def readyz() -> Response:
        """Deep readiness probe. 200 when all deps respond, 503 otherwise."""
        db_ok = await check_db_health()
        redis_ok = await check_redis_health()
        checks = {
            "db": "ok" if db_ok else "fail",
            "redis": "ok" if redis_ok else "fail",
        }
        if db_ok and redis_ok:
            status = "ok"
            http_status = 200
        elif db_ok or redis_ok:
            status = "degraded"
            http_status = 503
        else:
            status = "unhealthy"
            http_status = 503
        body = HealthResponse(status=status, checks=checks).model_dump(mode="json")
        return JSONResponse(status_code=http_status, content=body)

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        """Prometheus exposition. Loopback-only — same-host scrapers and proxies."""
        if not _client_host_is_loopback(request):
            envelope: ErrorEnvelope = ErrorEnvelope(
                error="feature_disabled",
                message="metrics is only available from loopback",
            )
            return JSONResponse(status_code=403, content=envelope.model_dump(mode="json"))
        return Response(content=render_metrics(), media_type=content_type())


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build a new FastAPI app with routes, middleware, and handlers wired."""
    app = FastAPI(title="yt-transcript-service", version="0.1.0", lifespan=lifespan)

    # Exception handlers. Order matters: more specific first.
    app.add_exception_handler(YTServiceError, _yt_service_exception_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(ValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    # Middleware. Starlette runs middleware in reverse-registration order on
    # the way out, so this is the only one and there's nothing to layer.
    app.add_middleware(AccessLogMiddleware)

    # Routes.
    app.include_router(_build_v1_router())
    _attach_meta_routes(app)

    return app


app = create_app()


__all__ = ["app", "create_app", "lifespan"]
