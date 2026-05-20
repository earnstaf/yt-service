"""Single source of truth for environment-driven configuration.

This module is the ONLY place in the application that reads environment variables
or the ``.env`` file. Every other module imports the ``settings`` singleton from
here. That gives us one well-typed surface for config and lets the test suite
drive behavior with monkeypatched env vars without touching globals scattered
across packages.

The load order is:
1. ``.env`` in the current working directory (only if present).
2. Process environment variables (these always win on conflict).

Empty-string env vars (the default when ``.env.example`` leaves a value blank)
are normalized to ``None`` for the ``YT_HTTPS_PROXY`` and ``*_API_KEY`` fields so
callers can do plain truthiness checks (``if settings.anthropic_api_key:``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from env vars and an optional ``.env``.

    Field naming convention: Python attribute names are lowercase versions of
    the environment variable. pydantic-settings matches them case-insensitively
    because ``case_sensitive=False`` is set in ``model_config``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Server ----
    yt_bind_host: str = "127.0.0.1"
    yt_bind_port: int = 8765
    yt_log_level: str = "info"
    yt_env: Literal["production", "dev", "test"] = "production"

    # ---- Postgres ----
    database_url: str

    # ---- Redis ----
    redis_url: str

    # ---- Cache TTLs and limits ----
    cache_ttl_days: int = 30
    summary_cache_ttl_days: int = 90
    max_video_duration_seconds: int = 14400

    # ---- Whisper ----
    whisper_backend: Literal["openai", "local"] = "openai"
    whisper_openai_model: str = "whisper-1"
    whisper_local_model: str = "base"
    whisper_fallback_on_openai_error: bool = True
    whisper_chunk_bytes: int = 20_971_520

    # ---- yt-dlp ----
    ytdlp_tmp_dir: str = "/var/tmp/yt-transcript"
    ytdlp_max_filesize_mb: int = 500

    # ---- Optional HTTPS proxy ----
    yt_https_proxy: str | None = None

    # ---- LLM providers ----
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    llmapi_api_key: str | None = None
    llmapi_base_url: str = "https://api.llmapi.ai/v1"
    huggingface_token: str | None = None

    # ---- Cost guard ----
    # NOTE: ``max_daily_llm_cost_usd`` is enforced by the cost-guard service
    # added in Phase P4 (per JC-009 / P-11). It is parsed here so the value is
    # available to early-stage health probes and admin tooling.
    max_daily_llm_cost_usd: float = 10.00

    # ---- Feature flags ----
    feature_sentiment: bool = True
    feature_diarization: bool = True
    feature_monitors: bool = True

    # ---- Rate limits ----
    rate_limit_read: str = "60/minute"
    rate_limit_batch: str = "10/minute"
    rate_limit_summarize: str = "30/minute"
    rate_limit_intelligence: str = "20/minute"
    rate_limit_whisper: str = "30/hour"
    rate_limit_monitor_create: str = "10/hour"

    # ---- Webhooks ----
    webhook_max_attempts: int = 3
    webhook_timeout_seconds: int = 10

    # ---- Bootstrap admin token ----
    yt_bootstrap_admin_token: str | None = Field(default=None)

    # ----------- Normalizers -----------

    @field_validator(
        "yt_https_proxy",
        "anthropic_api_key",
        "openai_api_key",
        "gemini_api_key",
        "llmapi_api_key",
        "huggingface_token",
        "yt_bootstrap_admin_token",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, v: object) -> object:
        """Convert blank env vars (``KEY=``) to ``None`` for cleaner downstream checks."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    # ----------- Derived helpers -----------

    @property
    def is_production(self) -> bool:
        """True when running under ``YT_ENV=production``."""
        return self.yt_env == "production"

    @property
    def sqlalchemy_dsn(self) -> str:
        """SQLAlchemy DSN. Indirection so we can swap drivers later without callsite churn."""
        return self.database_url

    @property
    def redis_db_index(self) -> int:
        """Parse the trailing ``/N`` from ``REDIS_URL``. Defaults to 0 when absent."""
        url = self.redis_url
        # Strip the scheme prefix so the first ``/`` we hit is the path.
        without_scheme = url.split("://", 1)[-1]
        if "/" not in without_scheme:
            return 0
        path = without_scheme.split("/", 1)[1]
        # Path may contain query string; trim it.
        path = path.split("?", 1)[0].strip("/")
        if not path:
            return 0
        try:
            return int(path)
        except ValueError:
            return 0


from functools import lru_cache as _lru_cache


@_lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide ``Settings`` instance, constructing it lazily.

    Instantiating at module import time would crash on import in environments
    that lack required env vars (e.g. during ``import app.exceptions`` in a
    fresh shell). Lazy access keeps imports cheap and pushes failure to the
    first real use, which is the FastAPI app startup hook.
    """
    return Settings()  # type: ignore[call-arg]


class _SettingsProxy:
    """Attribute proxy that defers ``Settings()`` instantiation until first access.

    Provided so existing ``from app.config import settings`` callsites keep
    working without forcing eager construction. All attribute reads go through
    ``get_settings()``.
    """

    __slots__ = ()

    def __getattr__(self, name: str):  # pragma: no cover - trivial passthrough
        return getattr(get_settings(), name)

    def __setattr__(self, name: str, value):  # pragma: no cover - test-time mutation
        # Pydantic models allow attribute assignment by default; route through the cached instance
        # so monkeypatch and other tooling can override values for tests.
        setattr(get_settings(), name, value)

    def __delattr__(self, name: str) -> None:  # pragma: no cover - symmetry with __setattr__
        delattr(get_settings(), name)

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return repr(get_settings())


settings: Settings = _SettingsProxy()  # type: ignore[assignment]

