"""Fetch a YouTube transcript via the yt-transcript-service backend.

Reads the service URL and bearer token from environment variables, calls
``/v1/transcript`` with the supplied video URL or ID, and polls
``/v1/jobs/<id>`` on a 202 response until the job completes or the timeout
fires. Prints the final response body verbatim to stdout. Exits non-zero on
error. Never prints the token.

Environment:
    YT_SERVICE_URL          base URL of the service (default: https://yt.ericmax.com)
    YT_SERVICE_TOKEN        bearer token (required)
    YT_SERVICE_POLL_TIMEOUT poll timeout in seconds (default: 300)

Exit codes:
    0  success
    1  service or protocol error (4xx, 5xx, failed job)
    2  configuration error (token missing)
    3  polling timed out
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlencode

try:
    import httpx  # type: ignore[import-not-found]

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False
    import urllib.error
    import urllib.request


DEFAULT_SERVICE_URL = "https://yt.ericmax.com"
DEFAULT_POLL_TIMEOUT = 300
POLL_INTERVAL = 5
REQUEST_TIMEOUT = 30.0


def _eprint(*args: Any) -> None:
    """Print to stderr with no token risk (caller controls args)."""
    print(*args, file=sys.stderr)


def _archive_transcript(body: bytes) -> None:
    """Write JSON + plain text copies to ``$YT_SERVICE_ARCHIVE_DIR`` if set.

    Skips writes when the target file already exists (idempotent re-runs are
    cheap and don't churn the archive). Failures are logged to stderr and
    swallowed so they never break the main fetch path.
    """
    archive_dir = os.environ.get("YT_SERVICE_ARCHIVE_DIR")
    if not archive_dir:
        return
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _eprint("archive: response body was not valid JSON; skipping")
        return
    video_id = payload.get("video_id")
    if not isinstance(video_id, str) or not video_id:
        _eprint("archive: response missing video_id; skipping")
        return
    try:
        os.makedirs(archive_dir, exist_ok=True)
    except OSError as exc:
        _eprint(f"archive: could not create {archive_dir}: {exc}")
        return

    json_path = os.path.join(archive_dir, f"{video_id}.json")
    text_path = os.path.join(archive_dir, f"{video_id}.txt")

    if not os.path.exists(json_path):
        try:
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            _eprint(f"archive: wrote {json_path}")
        except OSError as exc:
            _eprint(f"archive: could not write {json_path}: {exc}")

    if not os.path.exists(text_path):
        full_text = payload.get("full_text")
        if isinstance(full_text, str):
            try:
                with open(text_path, "w", encoding="utf-8") as fh:
                    fh.write(full_text)
                    if not full_text.endswith("\n"):
                        fh.write("\n")
                _eprint(f"archive: wrote {text_path}")
            except OSError as exc:
                _eprint(f"archive: could not write {text_path}: {exc}")


def _build_query(video: str, args: argparse.Namespace) -> str:
    """Build the query string for /v1/transcript."""
    params: dict[str, str] = {"v": video, "format": args.format, "lang": args.lang}
    if args.force:
        params["force"] = args.force
    if args.include:
        params["include"] = args.include
    return urlencode(params)


def _http_get(url: str, token: str) -> tuple[int, dict[str, str], bytes]:
    """GET a URL with bearer token. Returns (status, headers, body_bytes)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if _HAS_HTTPX:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(url, headers=headers)
            return resp.status_code, dict(resp.headers), resp.content
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:  # nosec B310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, dict(exc.headers or {}), body


def _decode_json(body: bytes) -> dict[str, Any] | None:
    """Decode bytes as JSON; return None on failure."""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _print_error_envelope(status: int, body: bytes) -> None:
    """Print an error envelope to stderr. Never includes the token."""
    parsed = _decode_json(body)
    if parsed is not None:
        _eprint(f"HTTP {status}: {json.dumps(parsed, indent=2)}")
    else:
        snippet = body[:500].decode("utf-8", errors="replace")
        _eprint(f"HTTP {status}: {snippet}")


def _poll_job(
    service_url: str,
    poll_url: str,
    token: str,
    timeout: int,
) -> str:
    """Poll a job until terminal state or timeout.

    Returns one of: "complete", "failed", "timeout", "error".
    """
    full_url = f"{service_url.rstrip('/')}{poll_url}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status_code, _, body = _http_get(full_url, token)
        if status_code != 200:
            _print_error_envelope(status_code, body)
            return "error"
        payload = _decode_json(body)
        if payload is None:
            _eprint("Job status response was not valid JSON")
            return "error"
        job_status = payload.get("status")
        if job_status == "complete":
            return "complete"
        if job_status == "failed":
            _eprint(f"Job failed: {json.dumps(payload, indent=2)}")
            return "failed"
        time.sleep(POLL_INTERVAL)
    _eprint(f"Polling timed out after {timeout}s")
    return "timeout"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a YouTube transcript via yt-transcript-service.",
    )
    parser.add_argument("video", help="YouTube URL or 11-character video ID")
    parser.add_argument(
        "--format",
        choices=("json", "text", "srt"),
        default="json",
        help="Response format (default: json)",
    )
    parser.add_argument("--lang", default="en", help="Language code (default: en)")
    parser.add_argument(
        "--force",
        choices=("whisper", "refresh"),
        default=None,
        help="Force Whisper transcription or bypass cache",
    )
    parser.add_argument(
        "--include",
        default=None,
        help="Comma-separated enrichments: chapters,speakers,topics",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    token = os.environ.get("YT_SERVICE_TOKEN")
    if not token:
        _eprint("YT_SERVICE_TOKEN env var is required")
        return 2

    service_url = os.environ.get("YT_SERVICE_URL", DEFAULT_SERVICE_URL).rstrip("/")
    try:
        poll_timeout = int(os.environ.get("YT_SERVICE_POLL_TIMEOUT", str(DEFAULT_POLL_TIMEOUT)))
    except ValueError:
        _eprint("YT_SERVICE_POLL_TIMEOUT must be an integer")
        return 2

    query = _build_query(args.video, args)
    url = f"{service_url}/v1/transcript?{query}"

    status, _, body = _http_get(url, token)

    if status == 200:
        sys.stdout.write(body.decode("utf-8", errors="replace"))
        if not body.endswith(b"\n"):
            sys.stdout.write("\n")
        _archive_transcript(body)
        return 0

    if status == 202:
        payload = _decode_json(body)
        if payload is None or "poll_url" not in payload:
            _eprint("202 response missing poll_url")
            return 1
        poll_url = payload["poll_url"]
        outcome = _poll_job(service_url, poll_url, token, poll_timeout)
        if outcome == "timeout":
            return 3
        if outcome != "complete":
            return 1
        # Re-fetch the now-cached transcript.
        status2, _, body2 = _http_get(url, token)
        if status2 == 200:
            sys.stdout.write(body2.decode("utf-8", errors="replace"))
            if not body2.endswith(b"\n"):
                sys.stdout.write("\n")
            _archive_transcript(body2)
            return 0
        _print_error_envelope(status2, body2)
        return 1

    _print_error_envelope(status, body)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
