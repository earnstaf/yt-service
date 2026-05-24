"""Summarize a cached YouTube transcript via the yt-transcript-service API (P2).

Usage:
    python summarize.py <video-url-or-id> [options]

Options:
    --style {exec_brief|exec_deep|technical|bulleted|competitive_intel|custom}
                              Style of summary (default exec_brief).
    --audience TEXT           Audience description (default empty).
    --custom-prompt TEXT      Required when --style=custom.
    --max-tokens N            Output cap (default 800).
    --no-timestamps           Omit key_timestamps from the response.
    --provider-override P/M   Admin-only override, e.g. llmapi/claude-sonnet-4-6.

Environment:
    YT_SERVICE_URL    Base URL (default https://yt.ericmax.com).
    YT_SERVICE_TOKEN  Required bearer token.

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
    import httpx  # noqa: F401 — runtime check

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

        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, content=payload, headers=headers)
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, resp.text

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body_text)
        except ValueError:
            return e.code, body_text


def _archive_summary(response: dict) -> None:
    """Write a markdown summary to ``$YT_SERVICE_ARCHIVE_DIR`` if set.

    File path: ``<archive>/<video_id>-<style>.md``. Skipped if the file
    already exists. Failures are logged to stderr and swallowed so they
    never break the main fetch path.
    """
    archive_dir = os.environ.get("YT_SERVICE_ARCHIVE_DIR")
    if not archive_dir:
        return
    video_id = response.get("video_id")
    style = response.get("style", "exec_brief")
    if not isinstance(video_id, str) or not video_id:
        print("archive: response missing video_id; skipping", file=sys.stderr)
        return
    try:
        os.makedirs(archive_dir, exist_ok=True)
    except OSError as exc:
        print(f"archive: could not create {archive_dir}: {exc}", file=sys.stderr)
        return

    path = os.path.join(archive_dir, f"{video_id}-{style}.md")
    if os.path.exists(path):
        return

    audience = response.get("audience", "")
    provider = response.get("provider_used", "unknown")
    summary_text = response.get("summary", "")
    timestamps = response.get("key_timestamps") or []

    lines = [
        f"# {video_id} — {style}",
        "",
        f"- **Style:** {style}",
        f"- **Audience:** {audience or '(unspecified)'}",
        f"- **Provider:** {provider}",
        f"- **Source video:** https://youtu.be/{video_id}",
        "",
        "---",
        "",
        summary_text.rstrip(),
        "",
    ]
    if timestamps:
        lines.append("## Key timestamps")
        lines.append("")
        for entry in timestamps:
            if not isinstance(entry, dict):
                continue
            t = entry.get("t")
            label = entry.get("label", "")
            deep_link = entry.get("deep_link") or (
                f"https://youtu.be/{video_id}?t={int(t)}" if isinstance(t, (int, float)) else ""
            )
            if deep_link:
                lines.append(f"- [{label}]({deep_link})")
            else:
                lines.append(f"- {label}")
        lines.append("")

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        print(f"archive: wrote {path}", file=sys.stderr)
    except OSError as exc:
        print(f"archive: could not write {path}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="YouTube video URL or 11-char ID")
    parser.add_argument(
        "--style",
        default="exec_brief",
        choices=[
            "exec_brief",
            "exec_deep",
            "technical",
            "bulleted",
            "competitive_intel",
            "custom",
        ],
    )
    parser.add_argument("--audience", default="")
    parser.add_argument("--custom-prompt", default=None)
    parser.add_argument("--max-tokens", type=int, default=800)
    parser.add_argument("--no-timestamps", action="store_true")
    parser.add_argument("--provider-override", default=None)
    args = parser.parse_args(argv)

    token = os.environ.get("YT_SERVICE_TOKEN")
    if not token:
        print("error: YT_SERVICE_TOKEN env var is required", file=sys.stderr)
        return 2

    base_url = os.environ.get("YT_SERVICE_URL", DEFAULT_BASE_URL).rstrip("/")
    url = f"{base_url}/v1/summarize"
    body: dict = {
        "video_id": args.video.strip(),
        "style": args.style,
        "audience": args.audience,
        "max_tokens": args.max_tokens,
        "include_timestamps": not args.no_timestamps,
    }
    if args.custom_prompt is not None:
        body["custom_prompt"] = args.custom_prompt
    if args.provider_override is not None:
        body["provider_override"] = args.provider_override

    status, response = _post(url, token, body)
    if status == 200:
        if isinstance(response, dict):
            print(json.dumps(response, indent=2))
            _archive_summary(response)
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
