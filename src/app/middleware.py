"""Request-scoped middleware and dependencies for the FastAPI app.

Two concerns live here:

- :class:`AccessLogMiddleware`: a Starlette ``BaseHTTPMiddleware`` subclass
  that emits one structured log line per request and increments the
  ``yt_requests_total`` Prometheus counter labeled by the route template and
  final response status. Route-template (rather than concrete path) labeling
  keeps the metric's cardinality bounded — ``/v1/jobs/{job_id}`` is one
  label value, not one per job id.

- :func:`enforce_rate_limit`: a coroutine route handlers call as
  ``await enforce_rate_limit(category, request, redis_client)`` after
  authentication has run. Implements a per-category, per-token (or per-IP
  fallback) sliding-window counter against Redis using ``INCR`` + ``EXPIRE``.
  On exceeded, raises :class:`app.exceptions.RateLimitedError` with a
  ``retry_after`` value in ``details`` so the exception handler can stamp
  the ``Retry-After`` response header.

Rate limit strings come straight from ``Settings`` and use the shorthand
``"N/period"`` where period is one of ``second``, ``minute``, ``hour``. The
parser tolerates whitespace and is case-insensitive.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config import get_settings
from app.exceptions import RateLimitedError
from app.logging import get_logger
from app.metrics import REQUESTS_TOTAL

_access_log = get_logger("access")

# Period name → window length in seconds. Used by :func:`_parse_limit`.
_PERIOD_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one JSON access-log line per request and bump the requests counter.

    The log line carries: timestamp (provided by the structlog processor),
    method, path, status, latency in milliseconds, video_id query parameter
    (when present), token_id (if auth succeeded), and the optional
    ``source``/``cache_hit`` markers a transcript route may stash on
    ``request.state``. The metric label uses the matched route template so
    cardinality stays bounded.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """Run the next ASGI app, time it, log, and bump the request counter."""
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            # Route template (e.g. /v1/jobs/{job_id}) keeps metric cardinality
            # bounded. When no route matched (404 to an unknown path), fall
            # back to the raw path so the log is still meaningful.
            endpoint = _resolve_endpoint(request)

            log_fields: dict[str, Any] = {
                "method": request.method,
                "path": request.url.path,
                "endpoint": endpoint,
                "status": status_code,
                "latency_ms": round(latency_ms, 2),
            }
            video_id = request.query_params.get("v") or request.query_params.get("video_id")
            if video_id:
                log_fields["video_id"] = video_id
            token = getattr(request.state, "token", None)
            if token is not None:
                log_fields["token_id"] = getattr(token, "id", None)
            source = getattr(request.state, "source", None)
            if source is not None:
                log_fields["source"] = source
            cache_hit = getattr(request.state, "cache_hit", None)
            if cache_hit is not None:
                log_fields["cache_hit"] = cache_hit

            _access_log.info("http_request", **log_fields)
            try:
                REQUESTS_TOTAL.labels(endpoint=endpoint, status=str(status_code)).inc()
            except Exception:  # pragma: no cover — metric subsystem must never break the response
                pass


def _resolve_endpoint(request: Request) -> str:
    """Return the matched route template, falling back to the concrete path.

    Starlette populates ``request.scope["route"]`` after routing. ``route.path``
    is the templated form (e.g. ``/v1/jobs/{job_id}``). Before routing runs
    (404 on an unmatched path), there is no route on the scope; in that case
    use the raw URL path.
    """
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return request.url.path


def _parse_limit(spec: str) -> tuple[int, int]:
    """Parse a ``"N/period"`` rate-limit string into ``(count, window_seconds)``.

    Accepts whitespace and any case. ``period`` must be one of ``second``,
    ``minute``, ``hour``, ``day``. Raises ``ValueError`` for any other shape;
    the caller is expected to surface this as a 500-level misconfiguration.
    """
    cleaned = spec.strip().lower()
    if "/" not in cleaned:
        raise ValueError(f"invalid rate limit spec: {spec!r}")
    count_str, period = (part.strip() for part in cleaned.split("/", 1))
    try:
        count = int(count_str)
    except ValueError as exc:
        raise ValueError(f"invalid rate limit count: {spec!r}") from exc
    if period not in _PERIOD_SECONDS:
        raise ValueError(f"invalid rate limit period: {period!r}")
    return count, _PERIOD_SECONDS[period]


def _limit_for_category(category: str) -> tuple[int, int]:
    """Look up the configured rate-limit spec for ``category`` and parse it.

    The settings attribute is ``rate_limit_<category>`` — e.g. ``read`` →
    ``settings.rate_limit_read``. Categories not present in settings raise
    ``ValueError``.
    """
    settings = get_settings()
    attr = f"rate_limit_{category}"
    spec = getattr(settings, attr, None)
    if spec is None:
        raise ValueError(f"no rate-limit configured for category {category!r}")
    return _parse_limit(spec)


def _rate_limit_key(category: str, request: Request) -> str:
    """Compose the Redis key for a given category and caller identity.

    Falls back to the client IP when no token is attached to ``request.state``.
    Anonymous endpoints (e.g. ``/healthz``) never call this function, so the
    fallback exists mostly as defense in depth.
    """
    token = getattr(request.state, "token", None)
    if token is not None and getattr(token, "id", None):
        principal = token.id
    else:
        client = request.client
        principal = client.host if client else "anonymous"
    return f"rl:{category}:{principal}"


async def enforce_rate_limit(category: str, request: Request, redis_client: Any) -> None:
    """Check the per-token rate bucket. Raises :class:`RateLimitedError` on exceeded.

    Algorithm:

    - Compute the Redis key from category + principal (token id or client IP).
    - ``INCR`` the counter. On the first increment of a new window, the count
      will come back as 1 — at that point we ``EXPIRE`` the key with the
      configured window length so the counter resets cleanly.
    - If the post-increment count exceeds the configured ``limit``, raise
      :class:`RateLimitedError` with ``details={"retry_after": <ttl>}`` so the
      handler can populate the ``Retry-After`` header.

    Lookup failures (bad config, Redis down) propagate to the caller. We
    deliberately do NOT fail-open on Redis errors — the operator should see
    the 500 and fix Redis rather than silently lose rate-limiting.
    """
    limit, window = _limit_for_category(category)
    key = _rate_limit_key(category, request)
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, window)
    if count > limit:
        ttl = await redis_client.ttl(key)
        retry_after = int(ttl) if ttl and ttl > 0 else window
        raise RateLimitedError(
            f"rate limit exceeded for {category}",
            details={"retry_after": retry_after, "limit": limit, "window_seconds": window},
        )


__all__ = [
    "AccessLogMiddleware",
    "enforce_rate_limit",
    "_parse_limit",
    "_limit_for_category",
]
