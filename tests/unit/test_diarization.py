"""Unit tests for the diarization module (P2 C1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import diarization as diar
from app.domain import Snippet


def test_align_speakers_assigns_overlapping_turn() -> None:
    """A turn that overlaps a snippet should set that snippet's speaker."""
    snippets = [
        Snippet(start=0.0, duration=5.0, text="hi"),
        Snippet(start=10.0, duration=5.0, text="there"),
    ]
    turns = [
        ("SPEAKER_00", 0.0, 4.5),
        ("SPEAKER_01", 9.5, 14.5),
    ]
    result = diar._align_speakers(snippets, turns)
    assert result[0].speaker == "SPEAKER_00"
    assert result[1].speaker == "SPEAKER_01"


def test_align_speakers_no_overlap_keeps_none() -> None:
    """Snippets without overlapping turns keep ``speaker=None``."""
    snippets = [Snippet(start=0.0, duration=2.0, text="hi")]
    turns = [("SPEAKER_00", 100.0, 105.0)]
    result = diar._align_speakers(snippets, turns)
    assert result[0].speaker is None


def test_align_speakers_picks_best_overlap() -> None:
    """When two turns overlap, the one with greater overlap wins."""
    snippets = [Snippet(start=0.0, duration=10.0, text="x")]
    turns = [
        ("SPEAKER_00", 0.0, 3.0),   # 3s overlap
        ("SPEAKER_01", 3.0, 9.0),   # 6s overlap (winner)
    ]
    result = diar._align_speakers(snippets, turns)
    assert result[0].speaker == "SPEAKER_01"


def test_is_available_returns_false_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No HUGGINGFACE_TOKEN → diarization unavailable, no exception."""
    diar._pipeline = None  # type: ignore[assignment]
    diar._pipeline_load_failed = None
    monkeypatch.setattr(diar.settings, "huggingface_token", "", raising=False)
    assert diar.is_available() is False


def test_is_available_caches_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call after failure returns False without re-trying the load."""
    diar._pipeline = None  # type: ignore[assignment]
    diar._pipeline_load_failed = None
    monkeypatch.setattr(diar.settings, "huggingface_token", "", raising=False)

    assert diar.is_available() is False

    # Now set token but expect cached failure to persist
    monkeypatch.setattr(diar.settings, "huggingface_token", "hf_xxx", raising=False)
    assert diar.is_available() is False


@pytest.mark.asyncio
async def test_diarize_raises_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``diarize`` without an available pipeline raises RuntimeError."""
    diar._pipeline = None  # type: ignore[assignment]
    diar._pipeline_load_failed = "unavailable for tests"
    monkeypatch.setattr(diar.settings, "huggingface_token", "", raising=False)

    from pathlib import Path

    with pytest.raises(RuntimeError, match="unavailable"):
        await diar.diarize(Path("/tmp/fake.m4a"), [])
