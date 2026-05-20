"""SQLAlchemy 2.0 declarative models for yt-transcript-service.

This module defines the complete persistence schema for the platform, including
tables that won't be exercised until phases P2-P4 (summaries, topics, sentiment,
monitors, llm_call_log). They are declared here so the P1 baseline migration
creates the full surface in one shot and future phase migrations remain purely
additive.

Spec source of truth: §7.4 of ``docs/specs/yt-transcript-service-spec.md``.

Conventions:

- SQLAlchemy 2.0 style: ``Mapped[...]`` annotations with ``mapped_column``.
- All timestamp columns are timezone-aware (``TIMESTAMP(timezone=True)`` →
  ``TIMESTAMPTZ`` on Postgres).
- JSON payloads use ``JSONB`` for indexability.
- The ``Token`` model masks its hash and webhook secret in ``__repr__`` per the
  logging guardrails in spec §7.16.
- The ``transcripts.full_text_tsv`` column is a stored generated column built
  from ``to_tsvector('english', full_text)``.
- The ``transcripts.embedding`` column is ``pgvector.Vector(1536)``; the
  underlying ``vector`` extension is installed out-of-band per JC-015.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all yt-transcript-service ORM models."""


class Transcript(Base):
    """Cached transcript for a ``(video_id, language)`` pair.

    Stores both the structured snippets (with start/duration/text per cue) and a
    flattened ``full_text`` field that is the basis for full-text search and
    LLM downstream processing. ``full_text_tsv`` is a stored generated column
    so callers don't have to maintain it. ``embedding`` is reserved for future
    semantic search via pgvector.
    """

    __tablename__ = "transcripts"

    video_id: Mapped[str] = mapped_column(Text, primary_key=True)
    language: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    is_generated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    snippets_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    chapters_jsonb: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    has_diarization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Stored generated column for full-text search. Postgres computes this from
    # ``full_text`` on every write. Reserved for the MCP search surface.
    full_text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR(),
        Computed("to_tsvector('english', full_text)", persisted=True),
        nullable=True,
    )

    # 1536-dim embedding (OpenAI ``text-embedding-3-small`` default). Nullable
    # because embedding generation is a separate, async path.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

    __table_args__ = (
        Index("idx_transcripts_expires", "expires_at"),
        Index("idx_transcripts_source", "source"),
        Index("idx_transcripts_fts", "full_text_tsv", postgresql_using="gin"),
    )


class Summary(Base):
    """LLM-generated summary for a transcript, keyed by style and audience.

    ``audience_hash`` and ``custom_hash`` allow the same video to have multiple
    cached summaries for different audiences or custom prompt overrides without
    collisions. Empty-string defaults keep the composite key non-null.
    """

    __tablename__ = "summaries"

    video_id: Mapped[str] = mapped_column(Text, primary_key=True)
    style: Mapped[str] = mapped_column(Text, primary_key=True)
    audience_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    custom_hash: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_timestamps_jsonb: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    provider_used: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Topic(Base):
    """Topic and entity extraction output for a video."""

    __tablename__ = "topics"

    video_id: Mapped[str] = mapped_column(Text, primary_key=True)
    topics_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False)
    entities_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False)
    claims_jsonb: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    questions_jsonb: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    provider_used: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Sentiment(Base):
    """Sentiment scoring for a video at a given granularity (overall, per-section, etc)."""

    __tablename__ = "sentiment"

    video_id: Mapped[str] = mapped_column(Text, primary_key=True)
    granularity: Mapped[str] = mapped_column(Text, primary_key=True)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    overall_label: Mapped[str] = mapped_column(Text, nullable=False)
    timeline_jsonb: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    provider_used: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Job(Base):
    """Background-job ledger entry.

    Covers Whisper transcription, enrichment (chapters/diarization), and
    intelligence (topics/sentiment) jobs. The non-spec ``created_at`` column is
    added so the workers and the admin UI can order queued jobs deterministically.
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    video_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_id: Mapped[str] = mapped_column(Text, nullable=False)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_jsonb: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Monitor(Base):
    """Channel-monitor subscription that polls for new videos on a schedule."""

    __tablename__ = "monitors"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_url: Mapped[str] = mapped_column(Text, nullable=False)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    include_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_video_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Token(Base):
    """API token record.

    ``token_hash`` stores a one-way hash of the raw token value. ``webhook_secret``
    is used to sign callback payloads. Both are sensitive and are explicitly
    masked in ``__repr__`` so they do not leak into logs or stack traces.
    """

    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    webhook_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    rate_overrides: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        """Render without leaking ``token_hash`` or ``webhook_secret`` values.

        Spec §7.16 forbids logging credential material. This repr is the last
        line of defense for accidental ``log.info(token)`` calls.
        """
        secret_marker = "***" if self.webhook_secret else None
        return (
            f"Token(id={self.id!r}, name={self.name!r}, "
            f"token_hash='***', webhook_secret={secret_marker!r}, "
            f"scopes={self.scopes!r}, revoked_at={self.revoked_at!r})"
        )


class LLMCallLog(Base):
    """Per-call ledger for LLM provider invocations.

    Powers the cost-guard service (JC-009 / P-11), provider-routing analytics,
    and error postmortems. Indexes optimize the two dominant access patterns:
    timeline-ordered tailing and per-provider/task aggregation.
    """

    __tablename__ = "llm_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    task: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    video_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_llm_log_ts", "ts"),
        Index("idx_llm_log_provider", "provider", "task"),
    )


__all__ = [
    "Base",
    "Transcript",
    "Summary",
    "Topic",
    "Sentiment",
    "Job",
    "Monitor",
    "Token",
    "LLMCallLog",
]
