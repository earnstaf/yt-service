"""URL and ID parsing helpers for yt-transcript-service.

The platform accepts video references in many shapes (bare ID, ``watch?v=``,
``youtu.be/``, ``shorts/``, ``embed/``, ``live/``, mobile host, missing
scheme). The route handlers normalize every shape to the canonical 11-char
YouTube video ID via :func:`parse_video_id` before anything else touches the
input.

Channel and playlist parsing is reserved for P3 (see
:func:`parse_channel_or_playlist`), exposed here only so the import surface
stays stable across phases.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qs, urlparse

from app.exceptions import InvalidVideoIdError

# YouTube video IDs are exactly 11 characters from the URL-safe base64 alphabet
# minus padding. We validate captures from URL paths/queries against this even
# when ``urlparse`` succeeds, because malformed inputs can still produce
# wrong-shaped captures.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Hostnames we recognize as YouTube. Compared case-insensitively (strip ``www.``
# and ``m.`` so mobile and apex hosts are handled uniformly).
_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be"})

# Path prefixes on ``youtube.com`` whose first segment after the prefix is the
# video ID. ``watch`` is special-cased separately because it carries the ID in
# the query string instead of the path.
_PATH_PREFIX_FORMS = ("shorts", "embed", "live", "v")


def _normalize_host(host: str | None) -> str | None:
    """Lowercase the host and strip ``www.`` / ``m.`` so comparisons stay simple."""
    if not host:
        return None
    host = host.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host


def _looks_like_url(value: str) -> bool:
    """Heuristic: treat anything containing ``/`` or ``://`` or a known host marker as a URL.

    Used to decide whether to try URL parsing or skip straight to bare-ID
    validation. We don't want a bare 11-char ID like ``abc/defghij11`` getting
    routed through the URL path, but we DO want ``youtu.be/...`` (no scheme)
    to parse as a URL.
    """
    if "://" in value:
        return True
    if "/" in value:
        return True
    return False


def _extract_id_from_url(raw: str) -> str | None:
    """Try to extract an 11-char candidate video ID from any YouTube URL form.

    Returns the candidate string (still un-validated against the regex) or
    ``None`` if the URL clearly isn't a YouTube video URL. Callers MUST
    validate the return against ``_VIDEO_ID_RE`` before trusting it.
    """
    # ``urlparse`` requires a scheme to populate ``netloc`` reliably. Prepend
    # one if the caller passed ``youtu.be/...`` without it.
    parse_target = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return None

    host = _normalize_host(parsed.hostname)
    if host not in _YOUTUBE_HOSTS:
        return None

    # ``youtu.be/<id>`` — entire path is the ID, with optional query/fragment.
    if host == "youtu.be":
        path = parsed.path.lstrip("/")
        # Path may have trailing segments (rare) — take only the first.
        candidate = path.split("/", 1)[0]
        return candidate or None

    # ``youtube.com/watch?v=<id>``
    path_segments = [seg for seg in parsed.path.split("/") if seg]
    if path_segments and path_segments[0] == "watch":
        query = parse_qs(parsed.query)
        values = query.get("v")
        if values:
            return values[0]
        return None

    # ``youtube.com/shorts/<id>`` / ``embed/<id>`` / ``live/<id>`` / ``v/<id>``
    if len(path_segments) >= 2 and path_segments[0] in _PATH_PREFIX_FORMS:
        return path_segments[1]

    return None


def parse_video_id(input: str) -> str:
    """Normalize any accepted YouTube reference into an 11-char video ID.

    Accepted forms:

    - bare 11-char ID (validated against the URL-safe base64 alphabet)
    - ``https://www.youtube.com/watch?v=<id>`` (extra query params/fragments OK)
    - ``https://youtu.be/<id>`` (with or without ``?t=...``)
    - ``https://www.youtube.com/shorts/<id>``
    - ``https://www.youtube.com/embed/<id>``
    - ``https://www.youtube.com/live/<id>``
    - ``https://m.youtube.com/watch?v=<id>``
    - ``youtu.be/<id>`` (scheme-less)
    - ``www.youtube.com/watch?v=<id>`` (scheme-less)
    - mixed-case host (``YouTube.com``)

    Raises :class:`app.exceptions.InvalidVideoIdError` for anything else —
    including empty strings, non-YouTube URLs, playlist-only URLs, malformed
    URLs, IDs of wrong length, or IDs containing illegal characters.
    """
    if not isinstance(input, str) or not input:
        raise InvalidVideoIdError(f"could not parse video id from: {input!r}")

    raw = input.strip()
    if not raw:
        raise InvalidVideoIdError(f"could not parse video id from: {input!r}")

    candidate: str | None

    if _looks_like_url(raw):
        candidate = _extract_id_from_url(raw)
    else:
        # Bare token — must already match the ID regex on its own.
        candidate = raw

    if candidate is None or not _VIDEO_ID_RE.match(candidate):
        raise InvalidVideoIdError(f"could not parse video id from: {input!r}")

    return candidate


def parse_channel_or_playlist(input: str) -> tuple[Literal["channel", "playlist"], str]:
    """Stub for P3. Will return the kind and the canonical channel/playlist ID.

    Declared here so the import surface is stable across phases. Channel and
    playlist parsing lands in P3 alongside the monitor service.
    """
    raise NotImplementedError("channel/playlist parsing lands in P3")


__all__ = ["parse_video_id", "parse_channel_or_playlist"]
