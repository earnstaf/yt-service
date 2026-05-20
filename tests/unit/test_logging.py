"""Unit tests for ``app.logging``.

Tests exercise the redaction processor directly (so behavior is unit-tested
without depending on the structlog plumbing order) and also drive a configured
logger end-to-end through ``capsys`` to verify the emitted JSON.
"""

from __future__ import annotations

import json

import pytest

from app.logging import configure_logging, get_logger, redact_sensitive


def test_configure_logging_is_idempotent() -> None:
    configure_logging("info")
    configure_logging("info")  # must not raise or duplicate output
    configure_logging("debug")


def test_redact_sensitive_masks_api_key() -> None:
    out = redact_sensitive(None, "info", {"api_key": "sk-abc"})
    assert out["api_key"] == "<redacted>"


def test_redact_sensitive_preserves_token_id() -> None:
    out = redact_sensitive(None, "info", {"token_id": "tok_xyz", "token_name": "ci-token"})
    assert out["token_id"] == "tok_xyz"
    assert out["token_name"] == "ci-token"


def test_redact_sensitive_masks_token_field() -> None:
    out = redact_sensitive(None, "info", {"token": "yt_abcdefghijklmnopqrstuv"})
    assert out["token"] == "<redacted>"


def test_redact_sensitive_masks_authorization_header() -> None:
    out = redact_sensitive(None, "info", {"authorization": "Bearer xyz"})
    assert out["authorization"] == "<redacted>"


def test_redact_sensitive_masks_bearer_in_value() -> None:
    out = redact_sensitive(None, "info", {"note": "Authorization: Bearer abc123def"})
    assert out["note"] == "<redacted>"


def test_redact_sensitive_masks_yt_token_shaped_value() -> None:
    out = redact_sensitive(None, "info", {"text": "yt_abcdefghijklmnopqrstuv"})
    assert out["text"] == "<redacted>"


def test_redact_sensitive_passes_through_safe_fields() -> None:
    out = redact_sensitive(
        None,
        "info",
        {"video_id": "dQw4w9WgXcQ", "latency_ms": 42, "source": "youtube_captions"},
    )
    assert out["video_id"] == "dQw4w9WgXcQ"
    assert out["latency_ms"] == 42
    assert out["source"] == "youtube_captions"


def test_redact_sensitive_masks_full_text() -> None:
    out = redact_sensitive(None, "info", {"full_text": "Welcome to the keynote..."})
    assert out["full_text"] == "<redacted>"


def test_redact_sensitive_masks_audio_path() -> None:
    out = redact_sensitive(None, "info", {"audio_path": "/var/tmp/yt-transcript/abc.m4a"})
    assert out["audio_path"] == "<redacted>"


def test_redact_sensitive_masks_webhook_secret() -> None:
    out = redact_sensitive(None, "info", {"webhook_secret": "wh-shhh"})
    assert out["webhook_secret"] == "<redacted>"


def test_redact_sensitive_masks_password_and_secret() -> None:
    out = redact_sensitive(None, "info", {"password": "pw", "client_secret": "sss"})
    assert out["password"] == "<redacted>"
    assert out["client_secret"] == "<redacted>"


def test_logger_emits_valid_json_with_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    log = get_logger("test")
    log.info("call", api_key="sk-abc", video_id="dQw4w9WgXcQ", token_id="tok_1")

    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "expected at least one log line"
    # Pick the most recent line (the one we just emitted).
    payload = json.loads(captured[-1])

    assert payload["event"] == "call"
    assert payload["api_key"] == "<redacted>"
    assert payload["video_id"] == "dQw4w9WgXcQ"
    assert payload["token_id"] == "tok_1"
    assert payload["level"] == "info"
    assert "ts" in payload


def test_logger_redacts_bearer_value_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    log = get_logger("test")
    log.info("hdr", authorization="Bearer xyz")
    captured = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(captured[-1])
    assert payload["authorization"] == "<redacted>"
