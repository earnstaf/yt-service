"""Output-format serializers for transcript responses.

Currently exposes a single function :func:`to_srt` that converts a list of
:class:`app.domain.Snippet` objects into SRT subtitle format. SRT is the most
portable subtitle format; every consumer (VLC, ffmpeg, web players) understands
it without a transformation step.

The SRT spec is informal but every implementation agrees on:

- One-based cue index on its own line.
- ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` timing line (comma — not period — for the
  millisecond separator).
- Cue text, then a single blank line.
- A trailing newline at end-of-file.

Milliseconds are rounded (not floored) so a 4.2005 value renders as ``201``
rather than ``200``. This matches the way most editors stamp cue boundaries
and avoids off-by-one drift over long recordings.
"""

from __future__ import annotations

from app.domain import Snippet


def _format_timestamp(seconds: float) -> str:
    """Render ``seconds`` as ``HH:MM:SS,mmm`` for SRT cues.

    Rounds milliseconds to nearest rather than flooring; carries the rollover
    into the higher units if rounding pushes a second/minute/hour past its
    natural ceiling.
    """
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(snippets: list[Snippet]) -> str:
    """Convert snippets to SRT subtitle format with ``HH:MM:SS,mmm`` timing.

    Each snippet becomes one cue. The end timestamp is ``start + duration``.
    Cue indices are 1-based per the SRT convention. A trailing newline closes
    the file so editors that line-count by ``\\n`` see the right total.
    """
    parts: list[str] = []
    for index, snippet in enumerate(snippets, start=1):
        start_ts = _format_timestamp(snippet.start)
        end_ts = _format_timestamp(snippet.start + snippet.duration)
        parts.append(f"{index}\n{start_ts} --> {end_ts}\n{snippet.text}\n")
    return "\n".join(parts) + ("\n" if parts else "")


__all__ = ["to_srt"]
