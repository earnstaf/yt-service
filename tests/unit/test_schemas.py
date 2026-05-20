"""Unit tests for `app.schemas`.

These tests pin the contracts in spec §5.5: every example payload that ships
in the spec must round-trip cleanly, and the batch size guard must trigger at
51 videos exactly.
"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas import (
    BatchRequest,
    BatchResponse,
    BatchResponseItem,
    ChapterOut,
    ErrorEnvelope,
    JobAcceptedResponse,
    TranscriptResponse,
    TranscriptSnippetOut,
)


# Spec §5.5 200 example as a Python dict. Field order matches the spec so a
# byte-comparison of `model_dump_json` would be order-stable too.
SPEC_TRANSCRIPT_200 = {
    "video_id": "OMhKgQmeMhI",
    "source": "youtube_captions",
    "language": "en",
    "is_generated": True,
    "duration_seconds": 1847.3,
    "snippet_count": 412,
    "cached_at": "2026-05-20T14:02:11Z",
    "cache_hit": True,
    "chapters": [
        {"start": 0.0, "end": 245.0, "title": "Opening keynote"},
        {"start": 245.0, "end": 612.0, "title": "Product announcements"},
    ],
    "snippets": [
        {
            "start": 0.0,
            "duration": 4.2,
            "text": "Welcome to the keynote",
            "speaker": "SPEAKER_00",
            "deep_link": "https://youtu.be/OMhKgQmeMhI?t=0",
        }
    ],
    "full_text": "Welcome to the keynote...",
}

SPEC_JOB_202 = {
    "job_id": "01HXY3ABCDEF",
    "status": "queued",
    "video_id": "OMhKgQmeMhI",
    "poll_url": "/v1/jobs/01HXY3ABCDEF",
    "estimated_seconds": 90,
}


def test_transcript_response_roundtrip_matches_spec_example() -> None:
    """Spec §5.5 200 example must deserialize, serialize, and re-deserialize identically."""
    model = TranscriptResponse.model_validate(SPEC_TRANSCRIPT_200)
    assert model.video_id == "OMhKgQmeMhI"
    assert model.cache_hit is True
    assert model.kind == "transcript"

    dumped = json.loads(model.model_dump_json())
    # Every spec key should be present in the dump.
    for key in SPEC_TRANSCRIPT_200:
        assert key in dumped, f"missing key: {key}"

    again = TranscriptResponse.model_validate(dumped)
    assert again == model


def test_job_accepted_response_roundtrip_matches_spec_example() -> None:
    """Spec §5.5 202 example must round-trip."""
    model = JobAcceptedResponse.model_validate(SPEC_JOB_202)
    assert model.job_id == "01HXY3ABCDEF"
    assert model.status == "queued"
    assert model.kind == "job_accepted"

    dumped = json.loads(model.model_dump_json())
    assert dumped["poll_url"] == "/v1/jobs/01HXY3ABCDEF"
    again = JobAcceptedResponse.model_validate(dumped)
    assert again == model


def test_batch_request_rejects_51_videos() -> None:
    with pytest.raises(ValidationError) as exc_info:
        BatchRequest(videos=[f"vid{i:03d}" for i in range(51)])
    # Confirm the validator message surfaces; outer handler maps to 413.
    assert "batch_too_large" in str(exc_info.value)


def test_batch_request_accepts_spec_example() -> None:
    """Spec §5.5 batch example shape."""
    req = BatchRequest.model_validate(
        {
            "videos": ["OMhKgQmeMhI", "https://youtu.be/abc..."],
            "lang": "en",
            "include": ["chapters", "speakers"],
            "callback_url": "https://other.service/yt-callback",
        }
    )
    assert len(req.videos) == 2
    assert req.lang == "en"
    assert req.callback_url == "https://other.service/yt-callback"


def test_batch_request_rejects_empty_video_list() -> None:
    with pytest.raises(ValidationError):
        BatchRequest(videos=[])


def test_batch_response_discriminates_union_items() -> None:
    """The discriminated union dispatches on `kind` internally but excludes it on the wire.

    H10: spec §5.5 batch examples do not include the `kind` discriminator on
    the wire. The Pydantic model retains it (with ``Field(exclude=True)``) so
    union validation still works for ``TypeAdapter`` and for ``model_validate``
    of the in-memory shape, but the serialized JSON omits it.
    """
    items: list[BatchResponseItem] = [
        TranscriptResponse.model_validate(SPEC_TRANSCRIPT_200),
        JobAcceptedResponse.model_validate(SPEC_JOB_202),
        ErrorEnvelope(error="invalid_video_id", message="bad input"),
    ]
    resp = BatchResponse(items=items)
    dumped = json.loads(resp.model_dump_json())
    # Wire form omits `kind` entirely.
    for item in dumped["items"]:
        assert "kind" not in item
    # Structural discriminators on the wire — fields unique to each branch.
    assert "snippets" in dumped["items"][0]
    assert "poll_url" in dumped["items"][1] and "snippets" not in dumped["items"][1]
    assert "error" in dumped["items"][2] and "snippets" not in dumped["items"][2]

    # Round-trip via the union adapter still works because the field default
    # supplies the discriminator value during validation of in-memory dicts.
    adapter = TypeAdapter(list[BatchResponseItem])
    rehydrated = adapter.validate_python(
        [
            {**dumped["items"][0], "kind": "transcript"},
            {**dumped["items"][1], "kind": "job_accepted"},
            {**dumped["items"][2], "kind": "error"},
        ]
    )
    assert isinstance(rehydrated[0], TranscriptResponse)
    assert isinstance(rehydrated[1], JobAcceptedResponse)
    assert isinstance(rehydrated[2], ErrorEnvelope)


def test_error_envelope_minimal_construction() -> None:
    env = ErrorEnvelope(error="invalid_video_id", message="cannot parse")
    assert env.details is None
    assert env.job_id is None
    assert env.poll_url is None
    assert env.kind == "error"


def test_error_envelope_with_job_in_progress_fields() -> None:
    env = ErrorEnvelope(
        error="job_in_progress",
        message="already running",
        job_id="01HX",
        poll_url="/v1/jobs/01HX",
    )
    assert env.job_id == "01HX"
    assert env.poll_url == "/v1/jobs/01HX"


def test_snippet_coerces_int_starts_to_float() -> None:
    snippet = TranscriptSnippetOut(
        start=0,  # int, JSON-ish
        duration=4,
        text="hi",
        deep_link="https://youtu.be/x?t=0",
    )
    assert isinstance(snippet.start, float)
    assert isinstance(snippet.duration, float)


def test_chapter_coerces_int_starts_to_float() -> None:
    chapter = ChapterOut(start=0, end=10, title="intro")
    assert isinstance(chapter.start, float)
    assert isinstance(chapter.end, float)


def test_cached_at_serializes_with_z_suffix_aware_datetime() -> None:
    """Aware UTC datetimes serialize to `...Z`, not `...+00:00`."""
    cached_at = datetime(2026, 5, 20, 14, 2, 11, tzinfo=timezone.utc)
    payload = {**SPEC_TRANSCRIPT_200, "cached_at": cached_at}
    model = TranscriptResponse.model_validate(payload)
    dumped = json.loads(model.model_dump_json())
    assert dumped["cached_at"] == "2026-05-20T14:02:11Z"


def test_cached_at_serializes_with_z_suffix_naive_datetime() -> None:
    """Naive datetimes are assumed UTC and still emit a Z suffix."""
    cached_at = datetime(2026, 5, 20, 14, 2, 11)
    payload = {**SPEC_TRANSCRIPT_200, "cached_at": cached_at}
    model = TranscriptResponse.model_validate(payload)
    dumped = json.loads(model.model_dump_json())
    assert dumped["cached_at"].endswith("Z")
