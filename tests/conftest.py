"""Pytest configuration shared across the suite.

Seeds non-secret env defaults BEFORE any application modules import so that
``app.config.settings`` can be touched in unit tests without requiring a real
``.env`` or production env vars. Integration tests that need real Postgres/Redis
override these via fixtures or by their own ``-m integration`` runtime.
"""

from __future__ import annotations

import os

# Seed required Settings fields early. These run at collection time, before
# any `from app.config import settings` line in a test module imports the proxy.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yttranscript:test@localhost/yt_transcript_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("YT_ENV", "test")
os.environ.setdefault("YT_BIND_HOST", "127.0.0.1")
os.environ.setdefault("YT_BIND_PORT", "8765")
os.environ.setdefault("YT_LOG_LEVEL", "warning")
os.environ.setdefault("CACHE_TTL_DAYS", "30")
os.environ.setdefault("SUMMARY_CACHE_TTL_DAYS", "90")
os.environ.setdefault("MAX_VIDEO_DURATION_SECONDS", "14400")
os.environ.setdefault("WHISPER_BACKEND", "openai")
os.environ.setdefault("WHISPER_OPENAI_MODEL", "whisper-1")
os.environ.setdefault("WHISPER_LOCAL_MODEL", "base")
os.environ.setdefault("WHISPER_FALLBACK_ON_OPENAI_ERROR", "true")
os.environ.setdefault("WHISPER_CHUNK_BYTES", "20971520")
os.environ.setdefault("YTDLP_TMP_DIR", "/tmp/yt-transcript-test")
os.environ.setdefault("YTDLP_MAX_FILESIZE_MB", "500")
os.environ.setdefault("YT_HTTPS_PROXY", "")
os.environ.setdefault("LLMAPI_BASE_URL", "https://api.llmapi.ai/v1")
os.environ.setdefault("MAX_DAILY_LLM_COST_USD", "10.0")
os.environ.setdefault("FEATURE_SENTIMENT", "true")
os.environ.setdefault("FEATURE_DIARIZATION", "true")
os.environ.setdefault("FEATURE_MONITORS", "true")
os.environ.setdefault("RATE_LIMIT_READ", "60/minute")
os.environ.setdefault("RATE_LIMIT_BATCH", "10/minute")
os.environ.setdefault("RATE_LIMIT_SUMMARIZE", "30/minute")
os.environ.setdefault("RATE_LIMIT_INTELLIGENCE", "20/minute")
os.environ.setdefault("RATE_LIMIT_WHISPER", "30/hour")
os.environ.setdefault("RATE_LIMIT_MONITOR_CREATE", "10/hour")
os.environ.setdefault("WEBHOOK_MAX_ATTEMPTS", "3")
os.environ.setdefault("WEBHOOK_TIMEOUT_SECONDS", "10")


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Skip ``@pytest.mark.integration`` tests unless ``--run-integration`` is passed."""
    import pytest

    if config.getoption("--run-integration", default=False):
        return
    skip_integration = pytest.mark.skip(reason="needs --run-integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.integration (requires real Postgres/Redis).",
    )
