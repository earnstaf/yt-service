"""Unit tests for :mod:`app.serialization`."""

from __future__ import annotations

from app.domain import Snippet
from app.serialization import _format_timestamp, to_srt


def _snip(start: float, duration: float, text: str) -> Snippet:
    return Snippet(start=start, duration=duration, text=text, speaker=None, deep_link="")


def test_format_timestamp_basic() -> None:
    assert _format_timestamp(0.0) == "00:00:00,000"
    assert _format_timestamp(1.5) == "00:00:01,500"
    assert _format_timestamp(60.0) == "00:01:00,000"
    assert _format_timestamp(3600.0) == "01:00:00,000"


def test_format_timestamp_rounds_milliseconds() -> None:
    # 0.0009 → 1ms when rounded (close to .001 boundary).
    assert _format_timestamp(0.0009) == "00:00:00,001"
    # 0.0014 → still rounds to 1ms.
    assert _format_timestamp(0.0014) == "00:00:00,001"
    # 0.0006 → rounds to 1ms (not floored to 0).
    assert _format_timestamp(0.0006) == "00:00:00,001"


def test_format_timestamp_negative_clamped() -> None:
    assert _format_timestamp(-1.0) == "00:00:00,000"


def test_to_srt_empty_list_returns_empty_string() -> None:
    assert to_srt([]) == ""


def test_to_srt_single_cue() -> None:
    out = to_srt([_snip(0.0, 4.2, "Welcome to the keynote")])
    assert "1\n00:00:00,000 --> 00:00:04,200\nWelcome to the keynote\n" in out
    assert out.endswith("\n")


def test_to_srt_multi_cue_index_and_blank_lines() -> None:
    cues = [
        _snip(0.0, 2.0, "first"),
        _snip(2.5, 3.0, "second"),
        _snip(10.0, 1.0, "third"),
    ]
    out = to_srt(cues)
    # Three cues separated by blank lines.
    assert "1\n00:00:00,000 --> 00:00:02,000\nfirst\n" in out
    assert "2\n00:00:02,500 --> 00:00:05,500\nsecond\n" in out
    assert "3\n00:00:10,000 --> 00:00:11,000\nthird\n" in out
    # Cues are joined with a blank line.
    assert "\n\n" in out
