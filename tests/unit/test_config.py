"""Unit tests for ``app.config``.

Tests use ``monkeypatch.setenv`` plus ``Settings(_env_file=None)`` so the
real on-disk ``.env`` file (if any) is bypassed. Each test starts from a
fresh ``Settings`` instance, never the module-level singleton.
"""

from __future__ import annotations

import pytest

from app.config import Settings

_MINIMUM_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://yttranscript:pw@localhost/yt_transcript",
    "REDIS_URL": "redis://localhost:6379/3",
}


def _apply_min_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set only the variables Settings cannot default for, plus per-test overrides."""
    for key, value in {**_MINIMUM_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_settings_loads_with_required_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch)
    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.redis_url == "redis://localhost:6379/3"
    # Defaults from .env.example should hold.
    assert s.yt_bind_host == "127.0.0.1"
    assert s.yt_bind_port == 8765
    assert s.cache_ttl_days == 30
    assert s.summary_cache_ttl_days == 90
    assert s.max_video_duration_seconds == 14400
    assert s.whisper_backend == "openai"
    assert s.whisper_chunk_bytes == 20_971_520
    assert s.feature_sentiment is True
    assert s.feature_diarization is True
    assert s.feature_monitors is True
    assert s.rate_limit_read == "60/minute"
    assert s.webhook_max_attempts == 3


def test_empty_proxy_env_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, YT_HTTPS_PROXY="")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.yt_https_proxy is None


def test_populated_proxy_env_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, YT_HTTPS_PROXY="http://user:pw@host:8080")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.yt_https_proxy == "http://user:pw@host:8080"


def test_empty_api_keys_are_normalized_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(
        monkeypatch,
        ANTHROPIC_API_KEY="",
        OPENAI_API_KEY="",
        GEMINI_API_KEY="",
        LLMAPI_API_KEY="",
        HUGGINGFACE_TOKEN="",
        YT_BOOTSTRAP_ADMIN_TOKEN="",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.anthropic_api_key is None
    assert s.openai_api_key is None
    assert s.gemini_api_key is None
    assert s.llmapi_api_key is None
    assert s.huggingface_token is None
    assert s.yt_bootstrap_admin_token is None


def test_populated_api_key_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, ANTHROPIC_API_KEY="sk-ant-xyz")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.anthropic_api_key == "sk-ant-xyz"


def test_is_production_flips_with_yt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, YT_ENV="production")
    assert Settings(_env_file=None).is_production is True  # type: ignore[call-arg]

    _apply_min_env(monkeypatch, YT_ENV="dev")
    assert Settings(_env_file=None).is_production is False  # type: ignore[call-arg]

    _apply_min_env(monkeypatch, YT_ENV="test")
    assert Settings(_env_file=None).is_production is False  # type: ignore[call-arg]


def test_yt_env_rejects_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, YT_ENV="staging")
    with pytest.raises(Exception):  # pydantic ValidationError; broad on purpose
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_redis_db_index_parses_trailing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, REDIS_URL="redis://localhost:6379/3")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.redis_db_index == 3


def test_redis_db_index_defaults_to_zero_without_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, REDIS_URL="redis://localhost:6379")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.redis_db_index == 0


def test_redis_db_index_defaults_to_zero_with_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch, REDIS_URL="redis://localhost:6379/")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.redis_db_index == 0


def test_sqlalchemy_dsn_mirrors_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_min_env(monkeypatch)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sqlalchemy_dsn == s.database_url


def test_settings_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lowercase env var names must still bind because case_sensitive=False."""
    _apply_min_env(monkeypatch, yt_bind_port="9999")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.yt_bind_port == 9999
