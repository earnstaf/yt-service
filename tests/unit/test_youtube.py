"""Unit tests for ``app.youtube``.

Every test mocks ``YouTubeTranscriptApi`` so no network is touched. The
adapter delegates to a synchronous library inside ``asyncio.to_thread``;
we exercise the public coroutine via ``pytest-asyncio``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import youtube as yt_mod
from app.exceptions import VideoUnavailableError, YouTubeBlockedError


def _fake_snippet(start: float, duration: float, text: str) -> SimpleNamespace:
    """Mimic ``youtube_transcript_api`` ``FetchedTranscriptSnippet`` shape."""
    return SimpleNamespace(start=start, duration=duration, text=text)


def _wire_happy_path(mock_api_cls: MagicMock) -> None:
    """Configure the mocked YouTubeTranscriptApi class for the happy case."""
    fake_transcript = MagicMock()
    fake_transcript.language_code = "en"
    fake_transcript.is_generated = True
    fake_transcript.fetch.return_value = [
        _fake_snippet(0.0, 2.0, "hello"),
        _fake_snippet(2.0, 3.0, "world"),
        _fake_snippet(5.0, 1.5, "again"),
    ]

    fake_list = MagicMock()
    fake_list.find_transcript.return_value = fake_transcript
    # find_generated_transcript shouldn't be called when find_transcript succeeds
    fake_list.find_generated_transcript.return_value = fake_transcript

    instance = MagicMock()
    instance.list.return_value = fake_list
    mock_api_cls.return_value = instance


@pytest.mark.asyncio
async def test_fetch_captions_happy_path() -> None:
    """Returns a CaptionsResult populated from the fake snippets."""
    with patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls:
        _wire_happy_path(mock_api_cls)
        result = await yt_mod.fetch_captions("vid123", lang="en")

    assert result is not None
    assert result.video_id == "vid123"
    assert result.language == "en"
    assert result.is_generated is True
    assert len(result.snippets) == 3
    assert result.snippets[0].text == "hello"
    assert result.snippets[0].deep_link == ""  # caller populates
    assert result.snippets[0].speaker is None
    # duration_seconds = max(start + duration) across snippets
    assert result.duration_seconds == pytest.approx(6.5)
    assert result.full_text == "hello world again"


@pytest.mark.asyncio
async def test_fetch_captions_falls_back_to_generated() -> None:
    """When find_transcript raises NoTranscriptFound, the auto track is used."""
    from youtube_transcript_api._errors import NoTranscriptFound

    fake_auto = MagicMock()
    fake_auto.language_code = "en"
    fake_auto.is_generated = True
    fake_auto.fetch.return_value = [_fake_snippet(0.0, 1.0, "auto")]

    fake_list = MagicMock()
    # NoTranscriptFound signature: (video_id, requested_language_codes, transcript_data)
    fake_list.find_transcript.side_effect = NoTranscriptFound("v", ["en"], MagicMock())
    fake_list.find_generated_transcript.return_value = fake_auto

    with patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls:
        instance = MagicMock()
        instance.list.return_value = fake_list
        mock_api_cls.return_value = instance
        result = await yt_mod.fetch_captions("v", lang="en")

    assert result is not None
    assert result.snippets[0].text == "auto"


@pytest.mark.asyncio
async def test_fetch_captions_returns_none_on_no_transcript_found() -> None:
    """No manual AND no generated transcript -> returns None."""
    from youtube_transcript_api._errors import NoTranscriptFound

    fake_list = MagicMock()
    fake_list.find_transcript.side_effect = NoTranscriptFound("v", ["en"], MagicMock())
    fake_list.find_generated_transcript.side_effect = NoTranscriptFound(
        "v", ["en"], MagicMock()
    )

    with patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls:
        instance = MagicMock()
        instance.list.return_value = fake_list
        mock_api_cls.return_value = instance
        result = await yt_mod.fetch_captions("v", lang="en")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_captions_returns_none_on_transcripts_disabled() -> None:
    """TranscriptsDisabled is a 'no caption track' signal -> returns None."""
    from youtube_transcript_api._errors import TranscriptsDisabled

    with patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls:
        instance = MagicMock()
        instance.list.side_effect = TranscriptsDisabled("v")
        mock_api_cls.return_value = instance
        result = await yt_mod.fetch_captions("v", lang="en")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_captions_raises_on_ip_blocked() -> None:
    """IpBlocked maps to YouTubeBlockedError."""
    from youtube_transcript_api._errors import IpBlocked

    with patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls:
        instance = MagicMock()
        instance.list.side_effect = IpBlocked("v")
        mock_api_cls.return_value = instance
        with pytest.raises(YouTubeBlockedError):
            await yt_mod.fetch_captions("v", lang="en")


@pytest.mark.asyncio
async def test_fetch_captions_raises_on_video_unavailable() -> None:
    """VideoUnavailable maps to VideoUnavailableError."""
    from youtube_transcript_api._errors import VideoUnavailable

    with patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls:
        instance = MagicMock()
        instance.list.side_effect = VideoUnavailable("v")
        mock_api_cls.return_value = instance
        with pytest.raises(VideoUnavailableError):
            await yt_mod.fetch_captions("v", lang="en")


@pytest.mark.asyncio
async def test_fetch_captions_passes_proxy_when_provided() -> None:
    """Explicit proxy kwarg builds a GenericProxyConfig."""
    with (
        patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls,
        patch.object(yt_mod, "GenericProxyConfig") as mock_proxy_cls,
    ):
        _wire_happy_path(mock_api_cls)
        await yt_mod.fetch_captions("v", lang="en", proxy="http://proxy:8080")

    mock_proxy_cls.assert_called_once_with(
        http_url="http://proxy:8080",
        https_url="http://proxy:8080",
    )
    # YouTubeTranscriptApi was instantiated with the proxy config kwarg
    _, kwargs = mock_api_cls.call_args
    assert "proxy_config" in kwargs


@pytest.mark.asyncio
async def test_fetch_captions_no_proxy_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without any proxy setting, the API is built with no kwargs."""
    # Force settings.yt_https_proxy to None for this test.
    monkeypatch.setattr(yt_mod.settings, "yt_https_proxy", None, raising=False)

    with patch.object(yt_mod, "YouTubeTranscriptApi") as mock_api_cls:
        _wire_happy_path(mock_api_cls)
        await yt_mod.fetch_captions("v", lang="en")

    # Called with no kwargs
    _, kwargs = mock_api_cls.call_args
    assert kwargs == {}
