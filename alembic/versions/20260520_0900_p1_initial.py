"""P1 initial schema

Revision ID: p1_initial
Revises:
Create Date: 2026-05-20 09:00:00+00:00

Creates the full set of tables defined in spec §7.4: ``transcripts``,
``summaries``, ``topics``, ``sentiment``, ``jobs``, ``monitors``, ``tokens``,
``llm_call_log``. Even tables that won't be exercised until P2-P4 land here so
future migrations are additive only.

Per JC-015, the ``vector`` extension is created out of band as the ``postgres``
superuser per ``deploy/README.md``. This migration assumes it already exists.
If it doesn't, the ``embedding vector(1536)`` column creation will fail loudly,
which is the intended behavior.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "p1_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all P1 tables and their indexes."""
    op.create_table(
        "transcripts",
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("is_generated", sa.Boolean(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("snippets_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("chapters_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "has_diarization",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "full_text_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', full_text)", persisted=True),
            nullable=True,
        ),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.PrimaryKeyConstraint("video_id", "language"),
    )
    op.create_index("idx_transcripts_expires", "transcripts", ["expires_at"])
    op.create_index("idx_transcripts_source", "transcripts", ["source"])
    op.create_index(
        "idx_transcripts_fts",
        "transcripts",
        ["full_text_tsv"],
        postgresql_using="gin",
    )

    op.create_table(
        "summaries",
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("style", sa.Text(), nullable=False),
        sa.Column("audience_hash", sa.Text(), nullable=False),
        sa.Column("custom_hash", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("key_timestamps_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider_used", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "style", "audience_hash", "custom_hash"),
    )

    op.create_table(
        "topics",
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("topics_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("entities_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("claims_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("questions_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider_used", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("video_id"),
    )

    op.create_table(
        "sentiment",
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("granularity", sa.Text(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("overall_label", sa.Text(), nullable=False),
        sa.Column("timeline_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider_used", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("video_id", "granularity"),
    )

    op.create_table(
        "jobs",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("token_id", sa.Text(), nullable=False),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column("payload_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )

    op.create_table(
        "monitors",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("channel_url", sa.Text(), nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("include_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("callback_url", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_video_id", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "tokens",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("webhook_secret", sa.Text(), nullable=True),
        sa.Column("rate_overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "llm_call_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("video_id", sa.Text(), nullable=True),
        sa.Column("token_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Spec specifies DESC ordering on the time index. ``op.create_index`` does
    # not expose per-column DESC through positional args, so use raw SQL.
    op.execute("CREATE INDEX idx_llm_log_ts ON llm_call_log (ts DESC)")
    op.create_index("idx_llm_log_provider", "llm_call_log", ["provider", "task"])


def downgrade() -> None:
    """Drop all P1 tables in reverse-FK-safe order."""
    op.drop_index("idx_llm_log_provider", table_name="llm_call_log")
    op.drop_index("idx_llm_log_ts", table_name="llm_call_log")
    op.drop_table("llm_call_log")
    op.drop_table("tokens")
    op.drop_table("monitors")
    op.drop_table("jobs")
    op.drop_table("sentiment")
    op.drop_table("topics")
    op.drop_table("summaries")
    op.drop_index("idx_transcripts_fts", table_name="transcripts")
    op.drop_index("idx_transcripts_source", table_name="transcripts")
    op.drop_index("idx_transcripts_expires", table_name="transcripts")
    op.drop_table("transcripts")
