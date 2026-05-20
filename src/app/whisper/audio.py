"""Audio download, splitting, and cleanup primitives for the Whisper pipeline.

This module owns three responsibilities and nothing else:

1. :func:`download_audio` — pull the bestaudio stream for a video id to a tmp
   directory using ``yt-dlp`` (per spec §7.13).
2. :func:`split_audio` — chunk a downloaded file into pieces under a byte
   threshold so the OpenAI Whisper endpoint accepts each part.
3. :func:`cleanup` — safe unlink in ``finally`` blocks.

The Whisper backends import from here. They never reach for ``yt-dlp`` or
``ffmpeg`` themselves.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
from pathlib import Path
from typing import Any

import yt_dlp

from app.config import settings
from app.exceptions import NoAudioStreamError


def _ytdlp_options(tmp_dir: Path) -> dict[str, Any]:
    """Build the yt-dlp options dict per spec §7.13.

    Factored out so tests can assert the exact keys without monkeypatching
    yt-dlp internals. When ``settings.yt_https_proxy`` is set the proxy is
    threaded through so yt-dlp can fetch from a host that blocks the VPS IP
    (Webshare / Zyte / similar). M4 in the code-review notes.
    """
    opts: dict[str, Any] = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": str(tmp_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "max_filesize": settings.ytdlp_max_filesize_mb * 1024 * 1024,
    }
    if settings.yt_https_proxy:
        opts["proxy"] = settings.yt_https_proxy
    return opts


def _download_sync(video_id: str, tmp_dir: Path) -> Path:
    """Run yt-dlp synchronously and return the produced file path.

    Raises :class:`NoAudioStreamError` on any yt-dlp failure or when the
    returned path does not exist on disk after download.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    opts = _ytdlp_options(tmp_dir)
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise NoAudioStreamError(
                    "yt-dlp returned no info dict",
                    details={"video_id": video_id},
                )
            filename = ydl.prepare_filename(info)
    except NoAudioStreamError:
        raise
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises many subclasses
        raise NoAudioStreamError(
            f"yt-dlp download failed: {exc}",
            details={"video_id": video_id},
        ) from exc

    path = Path(filename)
    if not path.exists():
        raise NoAudioStreamError(
            "yt-dlp reported success but file missing",
            details={"video_id": video_id, "expected_path": str(path)},
        )
    return path


async def download_audio(video_id: str, tmp_dir: Path) -> Path:
    """Download the bestaudio stream for ``video_id`` to ``tmp_dir``.

    yt-dlp is fully synchronous; we run it in a worker thread so the event
    loop is not blocked while a long download is in flight.
    """
    return await asyncio.to_thread(_download_sync, video_id, tmp_dir)


def cleanup(path: Path) -> None:
    """Best-effort unlink. Never raises if the file is already gone.

    Intended for ``finally`` blocks in the Whisper job runner so a failed
    transcribe never leaves audio behind.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Last-ditch swallow — cleanup must never mask the real error from a
        # caller's ``try`` block.
        return


def _probe_duration(path: Path) -> float:
    """Return audio duration in seconds via ``ffprobe``.

    Raises :class:`RuntimeError` if ffprobe is missing or fails. The Whisper
    backends translate that into :class:`WhisperFailedError`.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(  # noqa: S603 — argv list, no shell
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"ffprobe failed: {exc}") from exc
    out = result.stdout.strip()
    if not out:
        raise RuntimeError("ffprobe returned empty duration")
    try:
        return float(out)
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned non-numeric duration: {out!r}") from exc


def _run_ffmpeg_segment(
    path: Path,
    segment_time: float,
    pattern: Path,
    *,
    reencode: bool,
) -> None:
    """Run a single ``ffmpeg -f segment`` invocation.

    ``reencode=False`` attempts stream-copy (fast, container-dependent).
    ``reencode=True`` forces AAC re-encode at 128 kbps as a fallback.
    """
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-f",
        "segment",
        "-segment_time",
        str(int(math.ceil(segment_time))),
    ]
    if reencode:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c", "copy"]
    cmd.append(str(pattern))

    try:
        subprocess.run(  # noqa: S603 — argv list, no shell
            cmd,
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"ffmpeg segment failed (reencode={reencode}): {exc}") from exc


def split_audio(path: Path, max_bytes: int) -> list[Path]:
    """Split ``path`` into pieces no larger than ``max_bytes``.

    Returns ``[path]`` unchanged when the file already fits. Otherwise emits
    ``<stem>_part_000<ext>`` files alongside the source and returns them
    sorted. Tries stream-copy first; falls back to AAC re-encode if ffmpeg
    refuses to copy the codec.

    Raises :class:`RuntimeError` on ffprobe / ffmpeg failure. Whisper backends
    translate that into :class:`WhisperFailedError`.
    """
    size = path.stat().st_size
    if size <= max_bytes:
        return [path]

    duration = _probe_duration(path)
    # Number of parts needed, plus a safety margin so each part lands under
    # max_bytes even when bitrate is not perfectly uniform.
    parts_needed = math.ceil(size / max_bytes)
    target_seconds = duration / parts_needed
    # Pad downwards by 5% so encoder overhead does not push us over.
    target_seconds = max(target_seconds * 0.95, 1.0)

    stem = path.stem
    suffix = path.suffix or ".m4a"
    pattern = path.with_name(f"{stem}_part_%03d{suffix}")

    try:
        _run_ffmpeg_segment(path, target_seconds, pattern, reencode=False)
    except RuntimeError:
        # Stream-copy is codec-sensitive; fall back to a clean re-encode.
        _run_ffmpeg_segment(path, target_seconds, pattern, reencode=True)

    produced = sorted(path.parent.glob(f"{stem}_part_*{suffix}"))
    if not produced:
        raise RuntimeError("ffmpeg produced no segments")
    return produced
