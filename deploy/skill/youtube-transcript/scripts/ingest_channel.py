"""Submit a YouTube channel or playlist URL for batch ingestion (P3).

Usage:
    python ingest_channel.py <channel-or-playlist-url> [options]

Options:
    --max-videos N      Max videos to enqueue (default 100, server cap 500).
    --since YYYY-MM-DD  Skip videos older than this date.
    --include CSV       Comma-separated enrichment tokens (chapters, speakers, topics).
    --callback URL      Optional per-video completion webhook.

Environment:
    YT_SERVICE_URL    Base URL (default https://yt.ericmax.com).
    YT_SERVICE_TOKEN  Required bearer token (batch scope).

Prints the JSON response to stdout. Non-zero exit on error. Token never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

try:
    import httpx  # noqa: F401

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


DEFAULT_BASE_URL = "https://yt.ericmax.com"


def _post(url: str, token: str, body: dict) -> tuple[int, dict | str]:
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if _HAVE_HTTPX:
        import httpx  # noqa: PLC0415

        with httpx.Client(timeout=180.0) as client:
            resp = client.post(url, content=payload, headers=headers)
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, resp.text

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body_text)
        except ValueError:
            return e.code, body_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Channel URL (@handle, /channel/UC..., /c/name) or playlist URL")
    parser.add_argument("--max-videos", type=int, default=100)
    parser.add_argument("--since", default=None, help="YYYY-MM-DD")
    parser.add_argument("--include", default=None, help="comma-separated tokens")
    parser.add_argument("--callback", default=None)
    args = parser.parse_args(argv)

    token = os.environ.get("YT_SERVICE_TOKEN")
    if not token:
        print("error: YT_SERVICE_TOKEN env var is required", file=sys.stderr)
        return 2

    base_url = os.environ.get("YT_SERVICE_URL", DEFAULT_BASE_URL).rstrip("/")
    url = f"{base_url}/v1/ingest"
    body: dict = {"url": args.url, "max_videos": args.max_videos}
    if args.since is not None:
        body["since"] = args.since
    if args.include is not None:
        body["include"] = [t.strip() for t in args.include.split(",") if t.strip()]
    if args.callback is not None:
        body["callback_url"] = args.callback

    status, response = _post(url, token, body)
    if status == 200:
        if isinstance(response, dict):
            print(json.dumps(response, indent=2))
        else:
            print(response)
        return 0

    if isinstance(response, dict):
        print(json.dumps(response, indent=2), file=sys.stderr)
    else:
        print(response, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
