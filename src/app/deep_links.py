"""Deep-link helpers.

Per JC-014 we compute `https://youtu.be/<id>?t=<int>` deep links in P1 — the
spec lists this under P2 but the helper is trivial and the skill needs the
field populated.

Pure functions only; no I/O, no logging.
"""

from __future__ import annotations

from dataclasses import replace

from app.domain import Snippet


def compute_deep_link(video_id: str, start_seconds: float) -> str:
    """Return a `youtu.be` link that resumes at the integer second.

    YouTube's `?t=` accepts integers only; we floor (not round) so a snippet
    starting at 49.9s links to t=49, guaranteeing the quote appears in the
    visible playback window rather than just before it.
    """
    return f"https://youtu.be/{video_id}?t={int(start_seconds)}"


def with_deep_links(snippets: list[Snippet], video_id: str) -> list[Snippet]:
    """Return a new list of snippets with `deep_link` populated.

    Snippets are frozen dataclasses, so we use `dataclasses.replace` to build
    new instances rather than mutating in place. Input list is not modified.
    """
    return [
        replace(snippet, deep_link=compute_deep_link(video_id, snippet.start))
        for snippet in snippets
    ]
