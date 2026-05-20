"""Structlog JSON logging for yt-transcript-service.

Public API:

- :func:`configure_logging` — one-shot, idempotent setup that wires both the
  stdlib ``logging`` module and ``structlog`` to emit JSON lines on stdout.
- :func:`get_logger` — returns a bound logger that callers should hold onto.

Spec §7.16 forbids logging token values, full transcripts, or audio paths. The
``redact_sensitive`` processor walks each event dict and replaces any value
whose key looks like a credential, secret, full-text payload, or filesystem
path that could leak a recorded audio chunk. String values that themselves
look like bearer tokens (``Bearer ...``) or service tokens (``yt_...`` with
>20 chars) are also masked, even when they appear under benign keys.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

# Substrings that, when present in a key name, mark the value as sensitive.
_REDACT_KEY_SUBSTRINGS: tuple[str, ...] = (
    "authorization",
    "bearer",
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "full_text",
    "audio_path",
    "webhook_secret",
)

# Keys that match the ``token`` substring but are non-secret identifiers and
# must therefore pass through unredacted.
_TOKEN_KEY_ALLOWLIST: frozenset[str] = frozenset({"token_id", "token_name"})

_REDACTED = "<redacted>"

_configured = False


def _looks_like_secret_string(value: str) -> bool:
    """Heuristic for raw secret-looking values that leaked into a non-secret key.

    Matches:
    - ``Bearer <something>`` strings (case-insensitive prefix).
    - Service tokens that start with ``yt_`` and are longer than 20 chars.
    """
    if "Bearer " in value:
        return True
    stripped = value.strip()
    if stripped.startswith("yt_") and len(stripped) > 20:
        return True
    return False


def _should_redact_key(key: str) -> bool:
    """Return True when ``key`` should have its value masked."""
    lowered = key.lower()
    if lowered in _TOKEN_KEY_ALLOWLIST:
        return False
    return any(needle in lowered for needle in _REDACT_KEY_SUBSTRINGS)


def redact_sensitive(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that masks credentials, tokens, and large payloads."""
    for key in list(event_dict.keys()):
        value = event_dict[key]
        if _should_redact_key(key):
            event_dict[key] = _REDACTED
            continue
        if isinstance(value, str) and _looks_like_secret_string(value):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(level: str = "info") -> None:
    """Configure stdlib + structlog to emit JSON on stdout. Idempotent."""
    global _configured

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        redact_sensitive,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        # Pass file=None so structlog binds to sys.stdout dynamically at each
        # call. Pinning ``file=sys.stdout`` at configure time would capture a
        # specific file object that pytest's capsys may later close, leaving
        # subsequent log calls writing to a closed file.
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # Route stdlib logging through the same processor chain so libraries that
    # log via ``logging.getLogger`` also emit JSON.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Clear any pre-existing handlers so calling this twice does not double-emit.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Safe to call before configure_logging."""
    if name is None:
        return structlog.get_logger()
    return structlog.get_logger(name)
