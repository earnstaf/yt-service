"""Smoke tests for the ORM schema.

These do not require a running database. They verify:

1. Every model imports cleanly from :mod:`app.models`.
2. ``Base.metadata.tables`` enumerates the expected nine tables.
3. ``Token.__repr__`` masks the ``token_hash`` and ``webhook_secret`` values so
   we don't leak credentials into stack traces or logs.

Marked ``integration`` because the broader integration suite owns DB-touching
tests and they share a config. Skipped automatically when pytest is invoked
without ``-m integration``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from app.models import (  # noqa: E402  -- after pytestmark by design
    Base,
    Job,
    LLMCallLog,
    Monitor,
    Sentiment,
    Summary,
    Token,
    Topic,
    Transcript,
)

EXPECTED_TABLES = {
    "transcripts",
    "summaries",
    "topics",
    "sentiment",
    "jobs",
    "monitors",
    "tokens",
    "llm_call_log",
}


def test_models_import_cleanly() -> None:
    """All declared models reach the Base.metadata registry."""
    classes = (Transcript, Summary, Topic, Sentiment, Job, Monitor, Token, LLMCallLog)
    for cls in classes:
        assert cls.__tablename__ in Base.metadata.tables, cls.__tablename__


def test_metadata_contains_all_expected_tables() -> None:
    """Base.metadata enumerates every table from spec §7.4."""
    actual = set(Base.metadata.tables.keys())
    missing = EXPECTED_TABLES - actual
    assert not missing, f"Missing tables: {missing}"


def test_token_repr_masks_credentials() -> None:
    """Token.__repr__ never includes the raw hash or webhook secret."""
    raw_hash = "REAL_HASH_VALUE_DO_NOT_LEAK"
    raw_secret = "REAL_WEBHOOK_SECRET_DO_NOT_LEAK"
    token = Token(
        id="tok_test",
        name="test",
        token_hash=raw_hash,
        scopes=["read"],
        webhook_secret=raw_secret,
    )
    rendered = repr(token)
    assert raw_hash not in rendered
    assert raw_secret not in rendered
    assert "tok_test" in rendered
    assert "test" in rendered


def test_token_repr_handles_missing_webhook_secret() -> None:
    """Tokens without a webhook secret still render safely."""
    token = Token(
        id="tok_test2",
        name="test2",
        token_hash="another_secret_hash",
        scopes=["read"],
        webhook_secret=None,
    )
    rendered = repr(token)
    assert "another_secret_hash" not in rendered
    assert "tok_test2" in rendered
