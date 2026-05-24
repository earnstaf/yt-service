"""Unit tests for :mod:`app.parsing`.

Covers every accepted shape from the spec §5.5 ``v`` parameter plus the
common failure modes. ``parse_channel_or_playlist`` is exercised only to
confirm it raises ``NotImplementedError`` until P3 wires it up.
"""

from __future__ import annotations

import pytest

from app.exceptions import InvalidVideoIdError
from app.parsing import parse_channel_or_playlist, parse_video_id

VALID_ID = "OMhKgQmeMhI"


@pytest.mark.parametrize(
    "raw",
    [
        # Bare 11-char ID.
        VALID_ID,
        # Standard watch URL.
        f"https://www.youtube.com/watch?v={VALID_ID}",
        # Watch URL with extra query params.
        f"https://www.youtube.com/watch?v={VALID_ID}&t=42s&feature=share",
        # Watch URL with fragment.
        f"https://www.youtube.com/watch?v={VALID_ID}#fragment",
        # Short URL.
        f"https://youtu.be/{VALID_ID}",
        # Short URL with ?t= param.
        f"https://youtu.be/{VALID_ID}?t=120",
        # Shorts URL.
        f"https://www.youtube.com/shorts/{VALID_ID}",
        # Embed URL.
        f"https://www.youtube.com/embed/{VALID_ID}",
        # Live URL.
        f"https://www.youtube.com/live/{VALID_ID}",
        # Mobile host.
        f"https://m.youtube.com/watch?v={VALID_ID}",
        # Scheme-less short URL.
        f"youtu.be/{VALID_ID}",
        # Scheme-less watch URL.
        f"www.youtube.com/watch?v={VALID_ID}",
        # Mixed-case host.
        f"https://YouTube.com/watch?v={VALID_ID}",
        # Apex domain (no www).
        f"https://youtube.com/watch?v={VALID_ID}",
        # /v/ legacy form.
        f"https://www.youtube.com/v/{VALID_ID}",
        # http scheme.
        f"http://www.youtube.com/watch?v={VALID_ID}",
    ],
)
def test_parse_video_id_accepts_all_forms(raw: str) -> None:
    assert parse_video_id(raw) == VALID_ID


@pytest.mark.parametrize(
    "raw",
    [
        # Empty string.
        "",
        # Whitespace only.
        "   ",
        # 10 chars (too short).
        "OMhKgQmeMh",
        # 12 chars (too long).
        "OMhKgQmeMhIX",
        # 11 chars with illegal punctuation.
        "OMhKgQmeMh!",
        # Random non-YouTube URL with a v= param.
        "https://example.com/watch?v=OMhKgQmeMhI",
        # Playlist-only URL (no video ID).
        "https://www.youtube.com/playlist?list=PLrAXtmRdnEQy6nuLMHjMZOz59Oq8B9nUj",
        # Watch URL with missing v= param.
        "https://www.youtube.com/watch?feature=share",
        # Watch URL with bad-length v= param.
        "https://www.youtube.com/watch?v=tooShort",
        # Shorts with malformed id.
        "https://www.youtube.com/shorts/has!chars",
        # Completely unrelated URL.
        "https://news.ycombinator.com/item?id=123",
        # Garbage string.
        "not-a-url-at-all",
    ],
)
def test_parse_video_id_rejects_invalid(raw: str) -> None:
    with pytest.raises(InvalidVideoIdError):
        parse_video_id(raw)


def test_parse_video_id_error_message_includes_input() -> None:
    """The error message format is part of the public contract for debug logs."""
    # Use a string that clearly isn't 11 chars and isn't a YouTube URL form.
    # ("not-a-video" happens to be 11 valid YouTube-ID chars and would be accepted.)
    bogus = "not_a_valid_video_id_too_long"
    with pytest.raises(InvalidVideoIdError) as exc_info:
        parse_video_id(bogus)
    assert repr(bogus) in str(exc_info.value)


def test_parse_video_id_rejects_non_string() -> None:
    """Non-string input is fed in by mistake more often than you'd think."""
    with pytest.raises(InvalidVideoIdError):
        parse_video_id(None)  # type: ignore[arg-type]


def test_parse_channel_handle_url() -> None:
    ref = parse_channel_or_playlist("https://www.youtube.com/@SomeChannel")
    assert ref.kind == "channel_handle"
    assert ref.value == "@SomeChannel"


def test_parse_channel_id_url() -> None:
    ref = parse_channel_or_playlist(
        "https://www.youtube.com/channel/UCBR8-60-B28hp2BmDPdntcQ"
    )
    assert ref.kind == "channel_id"
    assert ref.value == "UCBR8-60-B28hp2BmDPdntcQ"


def test_parse_playlist_url() -> None:
    ref = parse_channel_or_playlist(
        "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
    )
    assert ref.kind == "playlist"
    assert ref.value.startswith("PL")


def test_parse_bare_handle() -> None:
    ref = parse_channel_or_playlist("@SomeChannel")
    assert ref.kind == "channel_handle"
    assert ref.value == "@SomeChannel"


def test_parse_channel_rejects_non_youtube_url() -> None:
    from app.exceptions import InvalidChannelError

    with pytest.raises(InvalidChannelError):
        parse_channel_or_playlist("https://example.com/@foo")


def test_parse_channel_rejects_empty() -> None:
    from app.exceptions import InvalidChannelError

    with pytest.raises(InvalidChannelError):
        parse_channel_or_playlist("")
