"""Unit tests for `app.exceptions`.

Verifies that every concrete exception carries the spec §5.6 error code and
status, and that `to_error_envelope` reflects job-in-progress state.
"""

import pytest

from app import exceptions as ex


ALL_EXCEPTIONS: list[type[ex.YTServiceError]] = [
    ex.InvalidVideoIdError,
    ex.InvalidRequestError,
    ex.InvalidChannelError,
    ex.UnauthorizedError,
    ex.InsufficientScopeError,
    ex.FeatureDisabledError,
    ex.VideoUnavailableError,
    ex.NoAudioStreamError,
    ex.NotFoundError,
    ex.JobInProgressError,
    ex.VideoTooLongError,
    ex.BatchTooLargeError,
    ex.RateLimitedError,
    ex.YouTubeBlockedError,
    ex.WhisperFailedError,
    ex.LLMFailedError,
    ex.QueueFullError,
    ex.InternalError,
]


@pytest.mark.parametrize("cls", ALL_EXCEPTIONS)
def test_every_exception_has_class_attrs(cls: type[ex.YTServiceError]) -> None:
    assert isinstance(cls.error_code, str) and cls.error_code
    assert isinstance(cls.status_code, int) and 400 <= cls.status_code < 600


def test_error_codes_match_spec_56_strings() -> None:
    """Spot-check the codes that route handlers will hardcode."""
    assert ex.InvalidVideoIdError.error_code == "invalid_video_id"
    assert ex.UnauthorizedError.error_code == "unauthorized"
    assert ex.InsufficientScopeError.error_code == "insufficient_scope"
    assert ex.FeatureDisabledError.error_code == "feature_disabled"
    assert ex.VideoUnavailableError.error_code == "video_unavailable"
    assert ex.NoAudioStreamError.error_code == "no_audio"
    assert ex.JobInProgressError.error_code == "job_in_progress"
    assert ex.VideoTooLongError.error_code == "video_too_long"
    assert ex.BatchTooLargeError.error_code == "batch_too_large"
    assert ex.RateLimitedError.error_code == "rate_limited"
    assert ex.YouTubeBlockedError.error_code == "youtube_ip_blocked"
    assert ex.WhisperFailedError.error_code == "whisper_failed"
    assert ex.LLMFailedError.error_code == "llm_failed"
    assert ex.QueueFullError.error_code == "queue_full"
    assert ex.InternalError.error_code == "internal_error"


def test_to_error_envelope_basic_shape() -> None:
    env = ex.to_error_envelope(ex.InvalidVideoIdError("foo"))
    assert env["error"] == "invalid_video_id"
    assert env["message"] == "foo"
    assert env["job_id"] is None
    assert env["poll_url"] is None
    # H10: spec §5.5 examples do not include the `kind` discriminator on the
    # wire. The Pydantic model carries it internally for union dispatch but
    # the dict envelope omits it entirely.
    assert "kind" not in env


def test_to_error_envelope_carries_job_in_progress_state() -> None:
    exc = ex.JobInProgressError("01HX", "/v1/jobs/01HX")
    env = ex.to_error_envelope(exc)
    assert env["error"] == "job_in_progress"
    assert env["job_id"] == "01HX"
    assert env["poll_url"] == "/v1/jobs/01HX"


def test_to_error_envelope_preserves_details() -> None:
    exc = ex.InvalidRequestError("bad field", details={"field": "videos"})
    env = ex.to_error_envelope(exc)
    assert env["details"] == {"field": "videos"}
