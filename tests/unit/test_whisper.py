"""Unit tests for the Whisper backends and dispatcher.

OpenAI's async client and faster-whisper's ``WhisperModel`` are both mocked.
``app.whisper.audio.split_audio`` is patched so we never call ffmpeg.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest

from app import whisper as whisper_dispatch
from app.domain import WhisperResult
from app.exceptions import WhisperFailedError
from app.whisper import local_backend, openai_backend


# ----- helpers -----


def _fake_openai_response(language: str = "en", duration: float = 10.0) -> SimpleNamespace:
    """Construct a verbose_json-shaped response from the OpenAI SDK."""
    segments = [
        SimpleNamespace(start=0.0, end=2.0, text="hello"),
        SimpleNamespace(start=2.0, end=5.0, text="world"),
    ]
    return SimpleNamespace(language=language, duration=duration, segments=segments)


def _fake_local_segments() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(start=0.0, end=1.5, text="loc one"),
        SimpleNamespace(start=1.5, end=3.0, text="loc two"),
    ]


@pytest.fixture
def fake_audio_file(tmp_path: Path) -> Path:
    p = tmp_path / "vid123.m4a"
    p.write_bytes(b"audio")
    return p


# ----- OpenAI backend direct -----


@pytest.mark.asyncio
async def test_openai_backend_happy_path(fake_audio_file: Path) -> None:
    """One-part transcription returns a populated WhisperResult."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(
        return_value=_fake_openai_response()
    )

    with (
        patch.object(openai_backend, "_client", return_value=mock_client),
        patch(
            "app.whisper.audio.split_audio",
            return_value=[fake_audio_file],
        ),
    ):
        result = await openai_backend.transcribe(fake_audio_file, video_id="vid123")

    assert isinstance(result, WhisperResult)
    assert result.source == "whisper_openai"
    assert result.video_id == "vid123"
    assert result.language == "en"
    assert len(result.snippets) == 2
    assert result.snippets[0].text == "hello"
    assert result.full_text == "hello world"


@pytest.mark.asyncio
async def test_openai_backend_chunks_multipart(fake_audio_file: Path, tmp_path: Path) -> None:
    """Multiple parts merge with monotonically-increasing time offsets."""
    part1 = tmp_path / "vid_part_000.m4a"
    part1.write_bytes(b"a")
    part2 = tmp_path / "vid_part_001.m4a"
    part2.write_bytes(b"b")

    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(
        side_effect=[
            _fake_openai_response(duration=5.0),
            _fake_openai_response(duration=4.0),
        ]
    )

    with (
        patch.object(openai_backend, "_client", return_value=mock_client),
        patch(
            "app.whisper.audio.split_audio",
            return_value=[part1, part2],
        ),
    ):
        result = await openai_backend.transcribe(fake_audio_file, video_id="vid")

    # 2 segments per part = 4 total
    assert len(result.snippets) == 4
    # Second part's first segment starts at offset 5.0 (first part's duration)
    assert result.snippets[2].start == pytest.approx(5.0)
    assert result.duration_seconds == pytest.approx(9.0)


@pytest.mark.asyncio
async def test_openai_backend_auth_error_is_hard_failure(fake_audio_file: Path) -> None:
    """401 maps to WhisperFailedError, not the retryable signal."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(
        side_effect=openai.AuthenticationError(
            message="bad key", response=MagicMock(), body=None
        )
    )

    with (
        patch.object(openai_backend, "_client", return_value=mock_client),
        patch("app.whisper.audio.split_audio", return_value=[fake_audio_file]),
    ):
        with pytest.raises(WhisperFailedError):
            await openai_backend.transcribe(fake_audio_file, video_id="vid")


@pytest.mark.asyncio
async def test_openai_backend_rate_limit_is_retryable(fake_audio_file: Path) -> None:
    """RateLimitError surfaces as the internal retryable signal."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = AsyncMock(
        side_effect=openai.RateLimitError(
            message="slow down", response=MagicMock(), body=None
        )
    )

    with (
        patch.object(openai_backend, "_client", return_value=mock_client),
        patch("app.whisper.audio.split_audio", return_value=[fake_audio_file]),
    ):
        with pytest.raises(openai_backend._OpenAIWhisperRetryable):
            await openai_backend.transcribe(fake_audio_file, video_id="vid")


# ----- Local backend direct -----


@pytest.mark.asyncio
async def test_local_backend_happy_path(fake_audio_file: Path) -> None:
    """Local backend returns a WhisperResult with source=whisper_local."""
    fake_model = MagicMock()
    fake_model.transcribe.return_value = (
        iter(_fake_local_segments()),
        SimpleNamespace(duration=3.0, language="en"),
    )

    with patch.object(local_backend, "_get_model", return_value=fake_model):
        result = await local_backend.transcribe(fake_audio_file, video_id="vid123")

    assert result.source == "whisper_local"
    assert result.video_id == "vid123"
    assert result.language == "en"
    assert len(result.snippets) == 2
    assert result.full_text == "loc one loc two"


@pytest.mark.asyncio
async def test_local_backend_wraps_exceptions(fake_audio_file: Path) -> None:
    """Any model failure becomes WhisperFailedError."""
    fake_model = MagicMock()
    fake_model.transcribe.side_effect = RuntimeError("oom")

    with patch.object(local_backend, "_get_model", return_value=fake_model):
        with pytest.raises(WhisperFailedError):
            await local_backend.transcribe(fake_audio_file, video_id="vid")


# ----- Dispatcher -----


@pytest.mark.asyncio
async def test_dispatch_openai_success(
    fake_audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backend=openai + clean call -> openai result."""
    monkeypatch.setattr(whisper_dispatch.settings, "whisper_backend", "openai", raising=False)

    fake_result = WhisperResult(
        video_id="vid",
        source="whisper_openai",
        language="en",
        snippets=[],
        duration_seconds=1.0,
        full_text="",
    )
    with patch.object(
        whisper_dispatch.openai_backend, "transcribe", AsyncMock(return_value=fake_result)
    ):
        result = await whisper_dispatch.transcribe(fake_audio_file, video_id="vid")
    assert result.source == "whisper_openai"


@pytest.mark.asyncio
async def test_dispatch_falls_back_on_retryable(
    fake_audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenAI raises retryable + fallback enabled -> local result."""
    monkeypatch.setattr(whisper_dispatch.settings, "whisper_backend", "openai", raising=False)
    monkeypatch.setattr(
        whisper_dispatch.settings, "whisper_fallback_on_openai_error", True, raising=False
    )

    local_result = WhisperResult(
        video_id="vid",
        source="whisper_local",
        language="en",
        snippets=[],
        duration_seconds=1.0,
        full_text="",
    )

    with (
        patch.object(
            whisper_dispatch.openai_backend,
            "transcribe",
            AsyncMock(
                side_effect=openai_backend._OpenAIWhisperRetryable(RuntimeError("boom"))
            ),
        ),
        patch.object(
            whisper_dispatch.local_backend,
            "transcribe",
            AsyncMock(return_value=local_result),
        ),
    ):
        result = await whisper_dispatch.transcribe(fake_audio_file, video_id="vid")

    assert result.source == "whisper_local"


@pytest.mark.asyncio
async def test_dispatch_does_not_fall_back_on_hard_error(
    fake_audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth/400 propagates as WhisperFailedError — local NEVER called."""
    monkeypatch.setattr(whisper_dispatch.settings, "whisper_backend", "openai", raising=False)
    monkeypatch.setattr(
        whisper_dispatch.settings, "whisper_fallback_on_openai_error", True, raising=False
    )

    local_mock = AsyncMock()
    with (
        patch.object(
            whisper_dispatch.openai_backend,
            "transcribe",
            AsyncMock(side_effect=WhisperFailedError("openai whisper: auth")),
        ),
        patch.object(whisper_dispatch.local_backend, "transcribe", local_mock),
    ):
        with pytest.raises(WhisperFailedError):
            await whisper_dispatch.transcribe(fake_audio_file, video_id="vid")

    local_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_local_only_mode_skips_openai(
    fake_audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backend=local -> OpenAI is never invoked."""
    monkeypatch.setattr(whisper_dispatch.settings, "whisper_backend", "local", raising=False)

    local_result = WhisperResult(
        video_id="vid",
        source="whisper_local",
        language="en",
        snippets=[],
        duration_seconds=1.0,
        full_text="",
    )
    openai_mock = AsyncMock()
    with (
        patch.object(whisper_dispatch.openai_backend, "transcribe", openai_mock),
        patch.object(
            whisper_dispatch.local_backend,
            "transcribe",
            AsyncMock(return_value=local_result),
        ),
    ):
        result = await whisper_dispatch.transcribe(fake_audio_file, video_id="vid")

    assert result.source == "whisper_local"
    openai_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_retryable_without_fallback_raises(
    fake_audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backend=openai + fallback disabled + retryable -> WhisperFailedError."""
    monkeypatch.setattr(whisper_dispatch.settings, "whisper_backend", "openai", raising=False)
    monkeypatch.setattr(
        whisper_dispatch.settings, "whisper_fallback_on_openai_error", False, raising=False
    )

    local_mock = AsyncMock()
    with (
        patch.object(
            whisper_dispatch.openai_backend,
            "transcribe",
            AsyncMock(
                side_effect=openai_backend._OpenAIWhisperRetryable(RuntimeError("5xx"))
            ),
        ),
        patch.object(whisper_dispatch.local_backend, "transcribe", local_mock),
    ):
        with pytest.raises(WhisperFailedError):
            await whisper_dispatch.transcribe(fake_audio_file, video_id="vid")

    local_mock.assert_not_called()
