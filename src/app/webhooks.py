"""Webhook delivery primitives for yt-transcript-service.

Spec §7.15 dictates outbound HTTP POSTs to caller-supplied ``callback_url``s
signed with an HMAC-SHA256 over the JSON body using the per-token webhook
secret. Retries are exponentially spaced 10/60/300 seconds across at most
``settings.webhook_max_attempts`` attempts. Per plan P-6, retries are
implemented as self re-enqueue with delay (``Queue.enqueue_in``) so a stuck
remote endpoint never ties up an RQ worker for minutes at a time.

This module exposes three public functions:

- :func:`sign_payload` — compute the HMAC signature header value.
- :func:`enqueue_webhook` — schedule a first delivery attempt.
- :func:`deliver_webhook_task` — RQ task body. Runs synchronously inside the
  RQ worker. On retryable failure re-enqueues itself with a delay.

Logging never carries the secret or the full payload; only ``callback_url``,
``event``, ``attempt``, and ``status`` are emitted. Body and header
serialization are deterministic so receivers can verify the signature with
``json.dumps(payload, sort_keys=True)`` and the same secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

import httpx
from rq import Queue

from app.config import settings
from app.logging import get_logger
from app.metrics import WEBHOOK_DELIVERIES

_logger = get_logger("webhooks")

# Retry schedule per spec §7.15. Indexed by the OUTGOING attempt number, i.e.
# the value used when the next attempt is enqueued. Attempt 1 -> 2 waits 10s,
# 2 -> 3 waits 60s, 3 -> 4 waits 300s. Anything beyond is rejected by the cap
# in :func:`deliver_webhook_task`.
_RETRY_DELAYS_SECONDS: dict[int, int] = {1: 10, 2: 60, 3: 300}

_QUEUE_NAME = "default"


def sign_payload(secret: str, body_bytes: bytes) -> str:
    """Return ``sha256=<hexdigest>`` HMAC of ``body_bytes`` keyed by ``secret``.

    Receivers reconstruct the same value with ``json.dumps(payload,
    sort_keys=True)`` and an identical secret. The leading ``sha256=`` prefix
    matches the convention used by major webhook providers (GitHub, Stripe)
    and the spec's example header in §7.15.
    """
    digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _serialize_body(payload: dict[str, Any]) -> bytes:
    """Render ``payload`` as deterministic UTF-8 JSON bytes.

    Sorted keys mean the byte stream the signer hashes is exactly the byte
    stream the receiver hashes. ``ensure_ascii`` is left at the default
    (``True``) so any non-ASCII characters are escaped, avoiding subtle
    encoding mismatches between Python and other languages.
    """
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def enqueue_webhook(
    redis_client: Any,  # noqa: ARG001 — kept for signature compat; sync client resolved internally
    callback_url: str,
    event: str,
    payload: dict[str, Any],
    secret: str,
    attempt: int = 1,
) -> None:
    """Schedule a webhook delivery on the ``default`` RQ queue.

    The first attempt enqueues immediately. Retries (attempt > 1) are
    expected to go through :func:`_enqueue_with_delay`, but callers may pass
    a higher ``attempt`` here to inject mid-stream attempts if needed.

    The ``redis_client`` parameter is accepted (and ignored) so existing
    callers that pass the async client don't need to change; the sync Redis
    client required by RQ is resolved internally via
    :func:`app.redis_client.get_sync_redis_client`.
    """
    from app.redis_client import get_sync_redis_client  # noqa: PLC0415 — lazy

    queue = Queue(_QUEUE_NAME, connection=get_sync_redis_client())
    queue.enqueue(
        deliver_webhook_task,
        callback_url,
        event,
        payload,
        secret,
        attempt,
    )


def _enqueue_with_delay(
    redis_client: Any,
    callback_url: str,
    event: str,
    payload: dict[str, Any],
    secret: str,
    attempt: int,
    delay_seconds: int,
) -> None:
    """Schedule a retry attempt for ``delay_seconds`` in the future via RQ.

    Uses ``Queue.enqueue_in`` so the worker that ran this delivery does NOT
    sleep — it returns immediately and is free to service other webhooks.
    """
    queue = Queue(_QUEUE_NAME, connection=redis_client)
    queue.enqueue_in(
        timedelta(seconds=delay_seconds),
        deliver_webhook_task,
        callback_url,
        event,
        payload,
        secret,
        attempt,
    )


def _is_retryable_status(status_code: int) -> bool:
    """Return True for HTTP statuses that should trigger a retry.

    5xx server errors are retryable; 4xx client errors are not (the caller
    misconfigured the endpoint and re-trying won't help). 2xx and 3xx are
    treated as success by the outer flow.
    """
    return 500 <= status_code < 600


def deliver_webhook_task(
    callback_url: str,
    event: str,
    payload: dict[str, Any],
    secret: str,
    attempt: int = 1,
) -> None:
    """Deliver one webhook attempt. RQ task entry point.

    Behavior:

    - Success (2xx/3xx response): metric incremented with the response status,
      no re-enqueue.
    - Retryable failure (5xx response, network error, timeout): re-enqueue
      self with the next attempt number and the matching delay from
      ``_RETRY_DELAYS_SECONDS``. If the current attempt is already at or
      past ``settings.webhook_max_attempts``, stop without re-enqueueing.
    - Non-retryable failure (4xx response): log and stop.

    Logging redacts the payload and secret; only ``callback_url``, ``event``,
    ``attempt``, and ``status`` (or ``error``) are emitted.
    """
    # Resolve a Redis client locally so retries can re-enqueue. We import here
    # to keep this module side-effect-free at import time (the RQ worker
    # imports the module to find the task; we don't want it to require Redis
    # connectivity just to import).
    from app.redis_client import get_redis_client  # noqa: PLC0415 — lazy by design

    body = _serialize_body(payload)
    signature = sign_payload(secret, body)

    headers = {
        "X-YT-Signature": signature,
        "X-YT-Event": event,
        "X-YT-Job-Id": str(payload.get("job_id", "")),
        "X-YT-Video-Id": str(payload.get("video_id", "")),
        "X-YT-Attempt": str(attempt),
        "Content-Type": "application/json",
    }

    timeout = settings.webhook_timeout_seconds
    max_attempts = settings.webhook_max_attempts

    def _maybe_retry(reason_status: str | int) -> None:
        """Re-enqueue the next attempt if we haven't exhausted the budget."""
        from app.redis_client import get_sync_redis_client  # noqa: PLC0415 — lazy

        if attempt >= max_attempts:
            _logger.warning(
                "webhook_giving_up",
                callback_url=callback_url,
                event_name=event,
                attempt=attempt,
                status=reason_status,
            )
            return
        delay = _RETRY_DELAYS_SECONDS.get(attempt, _RETRY_DELAYS_SECONDS[max(_RETRY_DELAYS_SECONDS)])
        _enqueue_with_delay(
            get_sync_redis_client(),
            callback_url,
            event,
            payload,
            secret,
            attempt + 1,
            delay,
        )
        _logger.info(
            "webhook_retry_scheduled",
            callback_url=callback_url,
            event_name=event,
            attempt=attempt,
            next_attempt=attempt + 1,
            delay_seconds=delay,
            status=reason_status,
        )

    try:
        # ``follow_redirects=False`` is the httpx default; we set it explicitly
        # so an SSRF attempt cannot redirect through a public origin to land
        # back on a private/internal endpoint (see H11).
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(callback_url, content=body, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
        WEBHOOK_DELIVERIES.labels(status="error").inc()
        _logger.warning(
            "webhook_delivery_error",
            callback_url=callback_url,
            event_name=event,
            attempt=attempt,
            error=type(exc).__name__,
        )
        _maybe_retry("error")
        return

    status_code = response.status_code
    WEBHOOK_DELIVERIES.labels(status=str(status_code)).inc()

    if 200 <= status_code < 400:
        _logger.info(
            "webhook_delivered",
            callback_url=callback_url,
            event_name=event,
            attempt=attempt,
            status=status_code,
        )
        return

    if _is_retryable_status(status_code):
        _logger.warning(
            "webhook_retryable_failure",
            callback_url=callback_url,
            event_name=event,
            attempt=attempt,
            status=status_code,
        )
        _maybe_retry(status_code)
        return

    # Non-retryable (4xx etc). Stop without re-enqueue.
    _logger.warning(
        "webhook_non_retryable_failure",
        callback_url=callback_url,
        event_name=event,
        attempt=attempt,
        status=status_code,
    )


__all__ = ["sign_payload", "enqueue_webhook", "deliver_webhook_task"]
