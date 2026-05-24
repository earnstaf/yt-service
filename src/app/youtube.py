"""YouTube captions adapter.

Thin wrapper around ``youtube-transcript-api`` v1.x. The library is synchronous
so every external call is wrapped in ``asyncio.to_thread`` to avoid blocking the
FastAPI event loop.

Public surface:
- ``fetch_captions(video_id, lang, proxy=None) -> CaptionsResult | None``

Error mapping (per spec §5.6 and plan C1):
- ``NoTranscriptFound`` / ``TranscriptsDisabled`` -> returns ``None`` (caller
  falls back to Whisper).
- ``IpBlocked`` -> raises :class:`app.exceptions.YouTubeBlockedError` (502).
- ``VideoUnavailable`` -> raises :class:`app.exceptions.VideoUnavailableError`
  (404).

Deep links on the returned snippets are left empty; the orchestrator populates
them via :func:`app.deep_links.with_deep_links` once it owns the video id (per
JC-014 / P-13).
"""

from __future__ import annotations

import asyncio

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    IpBlocked,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from youtube_transcript_api.proxies import GenericProxyConfig

from app.config import settings
from app.domain import CaptionsResult, Snippet
from app.exceptions import VideoUnavailableError, YouTubeBlockedError
from app.logging import get_logger

_logger = get_logger("youtube")


def _build_api(proxy: str | None) -> YouTubeTranscriptApi:
    """Construct a ``YouTubeTranscriptApi`` honoring an optional proxy.

    If ``proxy`` is provided, use it. Otherwise fall back to
    ``settings.yt_https_proxy`` when it is non-empty. When no proxy is
    configured at all, return the bare client.
    """
    effective_proxy = proxy if proxy else (settings.yt_https_proxy or None)
    if effective_proxy:
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(
                http_url=effective_proxy,
                https_url=effective_proxy,
            )
        )
    return YouTubeTranscriptApi()


def _fetch_sync(video_id: str, lang: str, proxy: str | None) -> CaptionsResult | None:
    """Synchronous worker that performs the actual captions fetch.

    Kept private and pure-blocking so the public coroutine can dispatch it via
    ``asyncio.to_thread``. Raises the library's typed errors; the public
    coroutine maps them to domain exceptions.
    """
    api = _build_api(proxy)
    transcript_list = api.list(video_id)

    # Prefer a manually-uploaded transcript in the requested language; fall back
    # to an auto-generated one. ``find_transcript`` raises ``NoTranscriptFound``
    # when nothing matches, which we map to ``None`` in the outer coroutine.
    try:
        transcript = transcript_list.find_transcript([lang])
    except NoTranscriptFound:
        transcript = transcript_list.find_generated_transcript([lang])

    fetched = transcript.fetch()

    snippets: list[Snippet] = []
    last_end = 0.0
    text_parts: list[str] = []
    for snippet in fetched:
        # youtube-transcript-api v1 exposes attributes on FetchedTranscriptSnippet
        start = float(snippet.start)
        duration = float(snippet.duration)
        text = snippet.text
        snippets.append(
            Snippet(start=start, duration=duration, text=text, speaker=None, deep_link="")
        )
        text_parts.append(text)
        last_end = max(last_end, start + duration)

    duration_seconds = last_end if snippets else None
    full_text = " ".join(text_parts)

    return CaptionsResult(
        video_id=video_id,
        language=transcript.language_code,
        is_generated=transcript.is_generated,
        snippets=snippets,
        duration_seconds=duration_seconds,
        full_text=full_text,
    )


def _fetch_video_metadata_sync(video_id: str) -> dict | None:
    """Run yt-dlp in metadata-only mode and return its info dict.

    Returns ``None`` on any failure — callers (the Whisper-enqueue pre-check)
    treat a missing duration as "proceed and let the worker catch the over-cap
    case after audio download." Per spec §5.6, the duration cap is enforced
    BEFORE audio download whenever possible (H12).
    """
    try:
        import yt_dlp  # noqa: PLC0415 — lazy: only the Whisper path needs this
    except Exception:  # noqa: BLE001 — never let a missing dep break the request
        return None

    opts: dict = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if settings.yt_https_proxy:
        opts["proxy"] = settings.yt_https_proxy
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001 — probe is best-effort
        return None
    if not isinstance(info, dict):
        return None
    return info


async def fetch_video_metadata(video_id: str) -> dict | None:
    """Async wrapper around :func:`_fetch_video_metadata_sync`.

    Returns the yt-dlp info dict (or ``None`` on failure). Callers should
    treat the duration field as best-effort and proceed if it's missing.
    """
    return await asyncio.to_thread(_fetch_video_metadata_sync, video_id)


async def fetch_captions(
    video_id: str,
    lang: str = "en",
    proxy: str | None = None,
) -> CaptionsResult | None:
    """Fetch the caption track for ``video_id`` in ``lang``.

    Returns a :class:`CaptionsResult` on success, or ``None`` when the video
    has no caption track (manually-uploaded or auto-generated) in the requested
    language. Re-raises library errors as the corresponding domain exceptions.

    The underlying ``youtube-transcript-api`` is synchronous, so the work runs
    on the default thread pool via :func:`asyncio.to_thread`.
    """
    try:
        return await asyncio.to_thread(_fetch_sync, video_id, lang, proxy)
    except (NoTranscriptFound, TranscriptsDisabled):
        return None
    except IpBlocked as exc:
        raise YouTubeBlockedError(
            "youtube blocked the request ip",
            details={"video_id": video_id},
        ) from exc
    except VideoUnavailable as exc:
        raise VideoUnavailableError(
            "video is private, deleted, or region-locked",
            details={"video_id": video_id},
        ) from exc


# ---------------------------------------------------------------------------
# Channel / playlist expansion (P3 A2)
# ---------------------------------------------------------------------------


def _channel_ref_to_url(kind: str, value: str) -> str:
    """Build a yt-dlp-ingestible URL from a ChannelRef-shaped pair."""
    if kind == "playlist":
        return f"https://www.youtube.com/playlist?list={value}"
    if kind == "channel_id":
        return f"https://www.youtube.com/channel/{value}/videos"
    if kind == "channel_handle":
        v = value if value.startswith("@") else f"@{value}"
        return f"https://www.youtube.com/{v}/videos"
    raise ValueError(f"unknown ref kind: {kind}")


def _expand_sync(url: str, max_videos: int):
    """Run yt-dlp flat extraction and return the ``entries`` list."""
    from yt_dlp import YoutubeDL  # noqa: PLC0415

    opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "noplaylist": False,
        "playlist_items": f"1-{max_videos}",
    }
    if settings.yt_https_proxy:
        opts["proxy"] = settings.yt_https_proxy
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return (info or {}).get("entries", []) or []


def _resolve_channel_sync(url: str) -> str | None:
    """Use yt-dlp metadata to map a channel handle/URL to its UC channel ID."""
    from yt_dlp import YoutubeDL  # noqa: PLC0415

    opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "noplaylist": True,
        "playlist_items": "0",  # don't enumerate videos, just metadata
    }
    if settings.yt_https_proxy:
        opts["proxy"] = settings.yt_https_proxy
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None
    cid = (info or {}).get("channel_id") or (info or {}).get("uploader_id")
    if isinstance(cid, str) and cid.startswith("UC"):
        return cid
    return None


async def resolve_channel_id(ref) -> str | None:  # noqa: ANN001
    """Resolve a ChannelRef (handle or id) to its canonical UC channel ID.

    Returns ``None`` if yt-dlp can't resolve. Caller decides whether to error.
    """
    url = _channel_ref_to_url(ref.kind, ref.value)
    return await asyncio.to_thread(_resolve_channel_sync, url)


async def expand_channel_or_playlist(ref, max_videos: int = 100, since=None):  # noqa: ANN001
    """Expand a channel/playlist URL into a list of :class:`VideoSummary`.

    Raises :class:`InvalidChannelError` on yt-dlp failure (codex H2 fix) so
    callers can surface a 400 to the user instead of returning success with an
    empty list.
    """
    from app.domain import VideoSummary  # noqa: PLC0415
    from app.exceptions import InvalidChannelError  # noqa: PLC0415

    url = _channel_ref_to_url(ref.kind, ref.value)
    try:
        raw_entries = await asyncio.to_thread(_expand_sync, url, max_videos)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("channel_expansion_failed", url=url, error=str(exc))
        raise InvalidChannelError(
            f"could not expand {url!r}: {type(exc).__name__}"
        ) from exc

    summaries: list = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("id")
        if not isinstance(vid, str) or len(vid) != 11:
            continue
        upload = entry.get("upload_date")
        if since is not None and isinstance(upload, str) and len(upload) == 8:
            try:
                from datetime import date as _date

                udate = _date(int(upload[:4]), int(upload[4:6]), int(upload[6:8]))
                if udate < since:
                    continue
            except ValueError:
                pass
        summaries.append(
            VideoSummary(
                video_id=vid,
                title=str(entry.get("title") or "")[:300],
                upload_date=upload if isinstance(upload, str) else None,
            )
        )
    return summaries
