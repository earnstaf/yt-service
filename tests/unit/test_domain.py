"""Unit tests for `app.domain` and `app.deep_links`."""

import pytest
from dataclasses import FrozenInstanceError

from app.deep_links import compute_deep_link, with_deep_links
from app.domain import Snippet


def test_snippet_is_immutable() -> None:
    snippet = Snippet(start=0.0, duration=1.0, text="hi")
    with pytest.raises(FrozenInstanceError):
        snippet.text = "mutated"  # type: ignore[misc]


def test_compute_deep_link_floors_to_integer_second() -> None:
    """49.9 -> t=49: the spec wants the playhead to land just before the quote."""
    assert compute_deep_link("OMhKgQmeMhI", 49.9) == "https://youtu.be/OMhKgQmeMhI?t=49"


def test_compute_deep_link_handles_zero_and_int() -> None:
    assert compute_deep_link("vid", 0.0) == "https://youtu.be/vid?t=0"
    assert compute_deep_link("vid", 245) == "https://youtu.be/vid?t=245"


def test_with_deep_links_populates_every_snippet() -> None:
    snippets = [
        Snippet(start=0.0, duration=4.2, text="welcome"),
        Snippet(start=10.5, duration=3.1, text="next bit", speaker="SPEAKER_00"),
    ]
    enriched = with_deep_links(snippets, "OMhKgQmeMhI")
    assert enriched[0].deep_link == "https://youtu.be/OMhKgQmeMhI?t=0"
    assert enriched[1].deep_link == "https://youtu.be/OMhKgQmeMhI?t=10"
    # Speaker / text preserved.
    assert enriched[1].speaker == "SPEAKER_00"
    assert enriched[1].text == "next bit"


def test_with_deep_links_does_not_mutate_input() -> None:
    """Snippets are frozen, but we still verify the input list is untouched."""
    original = [Snippet(start=0.0, duration=1.0, text="hi")]
    enriched = with_deep_links(original, "vid")
    assert original[0].deep_link == ""
    assert enriched[0].deep_link == "https://youtu.be/vid?t=0"
