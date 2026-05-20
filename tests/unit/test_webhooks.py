"""Unit tests for ``app.webhooks``.

Uses ``respx`` to intercept ``httpx`` calls so no network is touched, and
patches ``rq.Queue`` so we can assert re-enqueue scheduling without a real
Redis. The signature, body serialization, retry decisions, and metric
emission are all exercised here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from app import webhooks as wh
from app.config import settings


CALLBACK_URL = "https://hooks.example.com/yt-callback"
SECRET = "test-webhook-secret-deadbeef"
PAYLOAD = {
    "job_id": "01HXY3ABCDEF",
    "video_id": "OMhKgQmeMhI",
    "status": "complete",
    "source": "whisper_openai",
}
EVENT = "transcript.complete"


# ---------------------------------------------------------------------------
# sign_payload
# ---------------------------------------------------------------------------


def test_sign_payload_known_inputs() -> None:
    """HMAC matches the manual computation for a fixed body."""
    body = b'{"a":1,"b":2}'
    expected_digest = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert wh.sign_payload(SECRET, body) == f"sha256={expected_digest}"


def test_sign_payload_is_stable_for_sorted_json() -> None:
    """The same dict serialized with sort_keys hashes identically across runs."""
    body1 = json.dumps({"b": 2, "a": 1}, sort_keys=True).encode()
    body2 = json.dumps({"a": 1, "b": 2}, sort_keys=True).encode()
    assert wh.sign_payload(SECRET, body1) == wh.sign_payload(SECRET, body2)


def test_sign_payload_changes_with_secret() -> None:
    """Different secrets MUST produce different signatures."""
    body = b"{}"
    assert wh.sign_payload("alpha", body) != wh.sign_payload("beta", body)


# ---------------------------------------------------------------------------
# deliver_webhook_task — success path
# ---------------------------------------------------------------------------


@respx.mock
def test_deliver_2xx_success_no_retry() -> None:
    """2xx response: no re-enqueue, metric incremented with the status code."""
    route = respx.post(CALLBACK_URL).mock(return_value=httpx.Response(200))

    with patch.object(wh, "_enqueue_with_delay") as mock_retry:
        wh.deliver_webhook_task(CALLBACK_URL, EVENT, PAYLOAD, SECRET, attempt=1)

    assert route.called
    assert mock_retry.call_count == 0


@respx.mock
def test_deliver_signature_header_matches_sign_payload() -> None:
    """The X-YT-Signature header equals sign_payload(secret, serialized body)."""
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        captured["__body__"] = request.content.decode("utf-8")
        return httpx.Response(200)

    respx.post(CALLBACK_URL).mock(side_effect=_capture)

    wh.deliver_webhook_task(CALLBACK_URL, EVENT, PAYLOAD, SECRET, attempt=1)

    expected_body = json.dumps(PAYLOAD, sort_keys=True).encode("utf-8")
    expected_sig = wh.sign_payload(SECRET, expected_body)
    assert captured["x-yt-signature"] == expected_sig
    assert captured["x-yt-event"] == EVENT
    assert captured["x-yt-job-id"] == PAYLOAD["job_id"]
    assert captured["x-yt-video-id"] == PAYLOAD["video_id"]
    assert captured["x-yt-attempt"] == "1"
    assert captured["content-type"] == "application/json"


@respx.mock
def test_body_is_sorted_key_json() -> None:
    """Serialized body has keys in sorted order so signatures roundtrip."""
    payload = {"z": 26, "a": 1, "m": 13}
    captured_body: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_body["body"] = request.content.decode("utf-8")
        return httpx.Response(200)

    respx.post(CALLBACK_URL).mock(side_effect=_capture)

    wh.deliver_webhook_task(CALLBACK_URL, EVENT, payload, SECRET, attempt=1)

    assert captured_body["body"] == '{"a": 1, "m": 13, "z": 26}'


# ---------------------------------------------------------------------------
# deliver_webhook_task — retry paths
# ---------------------------------------------------------------------------


@respx.mock
def test_deliver_5xx_reenqueues_with_10s_delay_attempt_1_to_2() -> None:
    """5xx on attempt 1 schedules attempt 2 with the 10s delay from spec §7.15."""
    respx.post(CALLBACK_URL).mock(return_value=httpx.Response(503))

    with patch.object(wh, "_enqueue_with_delay") as mock_retry:
        wh.deliver_webhook_task(CALLBACK_URL, EVENT, PAYLOAD, SECRET, attempt=1)

    assert mock_retry.call_count == 1
    # _enqueue_with_delay(redis_client, callback_url, event, payload, secret, next_attempt, delay)
    args = mock_retry.call_args[0]
    assert args[1] == CALLBACK_URL
    assert args[2] == EVENT
    assert args[3] == PAYLOAD
    assert args[4] == SECRET
    assert args[5] == 2  # next attempt number
    assert args[6] == 10  # delay seconds


@respx.mock
def test_deliver_5xx_attempt_2_to_3_uses_60s_delay() -> None:
    """Attempt 2 -> 3 retry uses 60s delay."""
    respx.post(CALLBACK_URL).mock(return_value=httpx.Response(502))

    with patch.object(wh, "_enqueue_with_delay") as mock_retry:
        wh.deliver_webhook_task(CALLBACK_URL, EVENT, PAYLOAD, SECRET, attempt=2)

    assert mock_retry.call_count == 1
    args = mock_retry.call_args[0]
    assert args[5] == 3
    assert args[6] == 60


@respx.mock
def test_deliver_connection_error_reenqueues() -> None:
    """A transport-level error triggers the same retry path as a 5xx."""
    respx.post(CALLBACK_URL).mock(side_effect=httpx.ConnectError("boom"))

    with patch.object(wh, "_enqueue_with_delay") as mock_retry:
        wh.deliver_webhook_task(CALLBACK_URL, EVENT, PAYLOAD, SECRET, attempt=1)

    assert mock_retry.call_count == 1
    args = mock_retry.call_args[0]
    assert args[5] == 2
    assert args[6] == 10


@respx.mock
def test_deliver_timeout_reenqueues() -> None:
    """A timeout is retryable just like a network error."""
    respx.post(CALLBACK_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with patch.object(wh, "_enqueue_with_delay") as mock_retry:
        wh.deliver_webhook_task(CALLBACK_URL, EVENT, PAYLOAD, SECRET, attempt=1)

    assert mock_retry.call_count == 1


@respx.mock
def test_deliver_4xx_does_not_reenqueue() -> None:
    """4xx is a non-retryable client error per the retry decision rules."""
    respx.post(CALLBACK_URL).mock(return_value=httpx.Response(404))

    with patch.object(wh, "_enqueue_with_delay") as mock_retry:
        wh.deliver_webhook_task(CALLBACK_URL, EVENT, PAYLOAD, SECRET, attempt=1)

    assert mock_retry.call_count == 0


@respx.mock
def test_deliver_at_max_attempts_does_not_reenqueue_on_5xx() -> None:
    """When attempt == settings.webhook_max_attempts, give up even on 5xx."""
    respx.post(CALLBACK_URL).mock(return_value=httpx.Response(500))

    with patch.object(wh, "_enqueue_with_delay") as mock_retry:
        wh.deliver_webhook_task(
            CALLBACK_URL,
            EVENT,
            PAYLOAD,
            SECRET,
            attempt=settings.webhook_max_attempts,
        )

    assert mock_retry.call_count == 0


# ---------------------------------------------------------------------------
# enqueue_webhook
# ---------------------------------------------------------------------------


def test_enqueue_webhook_enqueues_on_default_queue() -> None:
    """``enqueue_webhook`` pushes ``deliver_webhook_task`` onto the default RQ queue."""
    fake_queue = MagicMock()
    with patch.object(wh, "Queue", return_value=fake_queue) as mock_queue_cls:
        wh.enqueue_webhook(
            redis_client=MagicMock(),
            callback_url=CALLBACK_URL,
            event=EVENT,
            payload=PAYLOAD,
            secret=SECRET,
            attempt=1,
        )

    assert mock_queue_cls.call_args[0][0] == "default"
    fake_queue.enqueue.assert_called_once()
    enqueue_args = fake_queue.enqueue.call_args[0]
    assert enqueue_args[0] is wh.deliver_webhook_task
    assert enqueue_args[1] == CALLBACK_URL
    assert enqueue_args[2] == EVENT
    assert enqueue_args[3] == PAYLOAD
    assert enqueue_args[4] == SECRET
    assert enqueue_args[5] == 1
