"""Unit tests for ``app.whisper.audio``.

ffmpeg / ffprobe are NOT required on the test host — every subprocess call is
mocked. yt-dlp is likewise mocked; no network or file download.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions import NoAudioStreamError
from app.whisper import audio as audio_mod


# ---------- cleanup ----------


def test_cleanup_noop_on_missing_file(tmp_path: Path) -> None:
    """cleanup must never raise when the file is gone."""
    audio_mod.cleanup(tmp_path / "does_not_exist.m4a")  # no exception


def test_cleanup_removes_existing_file(tmp_path: Path) -> None:
    """Existing file is unlinked."""
    target = tmp_path / "a.m4a"
    target.write_bytes(b"data")
    assert target.exists()
    audio_mod.cleanup(target)
    assert not target.exists()


# ---------- split_audio ----------


def test_split_audio_returns_single_when_under_threshold(tmp_path: Path) -> None:
    """Files at-or-under max_bytes are returned unchanged."""
    f = tmp_path / "small.m4a"
    f.write_bytes(b"x" * 1024)
    out = audio_mod.split_audio(f, max_bytes=4096)
    assert out == [f]


def test_split_audio_runs_ffmpeg_when_oversize(tmp_path: Path) -> None:
    """Large files trigger ffprobe + ffmpeg segment + file glob."""
    f = tmp_path / "big.m4a"
    f.write_bytes(b"x" * 4096)

    # Simulate ffmpeg's output by creating part files inside the patched run.
    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[0] == "ffprobe":
            return MagicMock(stdout="120.0\n", returncode=0)
        # ffmpeg call — synthesize segment files matching the pattern.
        (tmp_path / "big_part_000.m4a").write_bytes(b"a")
        (tmp_path / "big_part_001.m4a").write_bytes(b"b")
        return MagicMock(stdout="", returncode=0)

    with patch.object(audio_mod.subprocess, "run", side_effect=fake_run):
        out = audio_mod.split_audio(f, max_bytes=2048)

    assert len(out) == 2
    assert all("big_part_" in p.name for p in out)
    # Sorted
    assert out == sorted(out)


def test_split_audio_retries_with_reencode_on_copy_failure(tmp_path: Path) -> None:
    """First ffmpeg with -c copy fails -> retry with -c:a aac."""
    f = tmp_path / "big.m4a"
    f.write_bytes(b"x" * 4096)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return MagicMock(stdout="100.0\n", returncode=0)
        # First ffmpeg call (-c copy) fails
        if "-c" in cmd and "copy" in cmd:
            raise subprocess.CalledProcessError(1, cmd, b"", b"copy unsupported")
        # Second ffmpeg call (-c:a aac) — synthesize a single part
        (tmp_path / "big_part_000.m4a").write_bytes(b"a")
        return MagicMock(stdout="", returncode=0)

    with patch.object(audio_mod.subprocess, "run", side_effect=fake_run):
        out = audio_mod.split_audio(f, max_bytes=2048)

    assert len(out) == 1
    # Verify both ffmpeg invocations happened (probe + copy + reencode = 3)
    ffmpeg_calls = [c for c in calls if c[0] == "ffmpeg"]
    assert len(ffmpeg_calls) == 2
    assert "-c:a" in ffmpeg_calls[1]


def test_split_audio_raises_runtime_error_when_no_segments(tmp_path: Path) -> None:
    """ffmpeg ran but produced nothing -> RuntimeError."""
    f = tmp_path / "big.m4a"
    f.write_bytes(b"x" * 4096)

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        if cmd[0] == "ffprobe":
            return MagicMock(stdout="60.0\n", returncode=0)
        return MagicMock(stdout="", returncode=0)

    with patch.object(audio_mod.subprocess, "run", side_effect=fake_run):
        with pytest.raises(RuntimeError):
            audio_mod.split_audio(f, max_bytes=2048)


# ---------- download_audio ----------


@pytest.mark.asyncio
async def test_download_audio_calls_ytdlp_with_spec_options(tmp_path: Path) -> None:
    """Verify the yt-dlp options dict matches spec §7.13."""
    fake_info = {"id": "abc"}
    expected_path = tmp_path / "abc.m4a"
    expected_path.write_bytes(b"audio")

    fake_ydl = MagicMock()
    fake_ydl.__enter__.return_value = fake_ydl
    fake_ydl.__exit__.return_value = False
    fake_ydl.extract_info.return_value = fake_info
    fake_ydl.prepare_filename.return_value = str(expected_path)

    captured_opts: dict[str, object] = {}

    def fake_youtube_dl(opts: dict[str, object]) -> MagicMock:
        captured_opts.update(opts)
        return fake_ydl

    with patch.object(audio_mod.yt_dlp, "YoutubeDL", side_effect=fake_youtube_dl):
        path = await audio_mod.download_audio("abc", tmp_path)

    assert path == expected_path
    assert captured_opts["format"] == "bestaudio[ext=m4a]/bestaudio"
    assert captured_opts["quiet"] is True
    assert captured_opts["noplaylist"] is True
    assert "outtmpl" in captured_opts
    assert "max_filesize" in captured_opts
    assert isinstance(captured_opts["max_filesize"], int)
    assert captured_opts["max_filesize"] > 0


@pytest.mark.asyncio
async def test_download_audio_raises_no_audio_on_failure(tmp_path: Path) -> None:
    """Any yt-dlp exception becomes NoAudioStreamError."""
    fake_ydl = MagicMock()
    fake_ydl.__enter__.return_value = fake_ydl
    fake_ydl.__exit__.return_value = False
    fake_ydl.extract_info.side_effect = RuntimeError("403 Forbidden")

    with patch.object(audio_mod.yt_dlp, "YoutubeDL", return_value=fake_ydl):
        with pytest.raises(NoAudioStreamError):
            await audio_mod.download_audio("abc", tmp_path)


@pytest.mark.asyncio
async def test_download_audio_raises_when_file_missing(tmp_path: Path) -> None:
    """yt-dlp reports success but the file is not on disk -> NoAudioStreamError."""
    fake_ydl = MagicMock()
    fake_ydl.__enter__.return_value = fake_ydl
    fake_ydl.__exit__.return_value = False
    fake_ydl.extract_info.return_value = {"id": "abc"}
    fake_ydl.prepare_filename.return_value = str(tmp_path / "nope.m4a")

    with patch.object(audio_mod.yt_dlp, "YoutubeDL", return_value=fake_ydl):
        with pytest.raises(NoAudioStreamError):
            await audio_mod.download_audio("abc", tmp_path)
