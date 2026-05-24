---
name: youtube-transcript
description: Use when a user pastes a YouTube URL (video, channel, playlist, Shorts, live) or asks for a transcript, summary, comparison, channel tracking, or exec brief of a YouTube video. Routes channel/playlist URLs to the ingest endpoint. Uses timestamped deep links when quoting passages.
---

# YouTube Transcript

A skill for fetching, quoting, and summarizing YouTube video transcripts via the yt-transcript-service backend.

## When to use this skill

- Any YouTube URL appears in the user's message: `youtube.com/watch?v=...`, `youtu.be/...`, `youtube.com/shorts/...`, `youtube.com/live/...`, `youtube.com/@channel`, `youtube.com/playlist?list=...`.
- A bare video ID (11 characters) appears in a context where the user is asking about a video.
- The user asks to "summarize this video", "what does X say about Y in this video", "give me an exec brief of this keynote", or "transcript this".
- The user asks to compare two or more videos.
- The user asks to track a channel or watch a playlist for new uploads.
- The user pastes a Shorts URL and wants the audio content extracted.

## When NOT to use this skill

- The user is asking for metadata only: view counts, subscriber counts, channel name, upload date, video title, thumbnail. Answer those from general knowledge or a web search instead.
- The user is asking a general question about YouTube the platform (algorithm, policies, monetization), not about a specific video.
- The user is asking about a video without giving a URL or ID and the prior turn does not contain one.

## How to invoke

Scripts live in the `scripts/` directory next to this file. The canonical invocation:

```bash
python scripts/fetch_transcript.py "<URL or video ID>"
```

Environment:

- `YT_SERVICE_URL` — backend base URL. Defaults to `https://yt.ericmax.com`. For local development, set to `http://127.0.0.1:8765`.
- `YT_SERVICE_TOKEN` — bearer token (required). Never log or echo this value.
- `YT_SERVICE_POLL_TIMEOUT` — polling timeout in seconds for 202 responses. Default `300` (5 minutes).
- `YT_SERVICE_ARCHIVE_DIR` — optional local directory. When set, every successful fetch writes `<video_id>.json` + `<video_id>.txt` here; every successful summary writes `<video_id>-<style>.md`. Skips writes when the target file already exists, so re-runs are idempotent. Unset = no archive (stdout-only). Recommended path on this machine: `D:\Claude Projects\Tools\yt-service\local-archive\`.

Optional flags accepted by `fetch_transcript.py`:

- `--format json|text|srt` — output format. Default `json`.
- `--lang en` — language code. Default `en`.
- `--force whisper|refresh` — force Whisper transcription or bypass cache.
- `--include chapters,speakers,topics` — comma-separated list of enrichments (most are P2+; harmless to pass in P1).

For long videos that need Whisper, the script automatically polls `/v1/jobs/<id>` every 5 seconds until the job completes, fails, or the timeout fires. Output is JSON to stdout matching the `TranscriptResponse` schema.

## Quoting passages

Always use the `deep_link` field from `snippets[]`. Format:

> "quote" — [MM:SS](deep_link)

Example:

> "Welcome to the keynote" — [0:00](https://youtu.be/OMhKgQmeMhI?t=0)

Convert the snippet's `start` (seconds) to `MM:SS` or `HH:MM:SS` for display. The `deep_link` itself is the authoritative URL; do not hand-construct it.

## Channel and playlist URLs

In Phase 1, channel and playlist URLs return a structured error from the service. The skill will surface that error to the user with a brief note that channel-wide ingest ships in Phase 3. In Phase 3+, these URLs route to `/v1/ingest` automatically via `scripts/ingest_channel.py`.

## Summary requests

`scripts/summarize.py` POSTs to `/v1/summarize` (P2+). The transcript must already be cached — call `fetch_transcript.py` first if unsure.

```bash
python scripts/summarize.py "<URL or ID>" --style exec_brief --audience "SE team"
```

### Choosing a summary style

Map the user's phrasing to a `--style` value:

| User phrasing | `--style` |
|---|---|
| "exec summary" / "executive summary" / "brief" | `exec_brief` |
| "deep dive" / "executive deep dive" / "detailed exec" | `exec_deep` |
| "technical" / "engineering" / "implementation details" | `technical` |
| "bullets" / "bulleted" / "list" | `bulleted` |
| "competitive" / "compete" / "vs <competitor>" | `competitive_intel` |
| User supplies a custom instruction | `custom` (use `--custom-prompt`) |
| Anything else | `exec_brief` (safe default) |

### Deep links in summary output

The response includes `key_timestamps[]` with `{t, label, deep_link}`. Format quotes as `[label](deep_link)`:

> "[Pricing announcement](https://youtu.be/OMhKgQmeMhI?t=412)"

Never construct deep links by hand — use the server-supplied `deep_link` field.

### Chapters and speakers (P2)

Pass `--include chapters` to `fetch_transcript.py` to get `chapters[]` on the response. Pass `--include speakers` to enable diarization. Captions-sourced transcripts cannot be diarized (response carries `diarization_status: "captions_source_unsupported"`); use `--force whisper` first to re-transcribe. Whisper-sourced diarization runs async — response includes `diarization_status: "queued"` and `diarization_job_id`; poll the job and re-fetch the transcript when complete.

## Errors and never-leak rules

- The skill MUST NOT log, echo, or include `YT_SERVICE_TOKEN` in any output, error message, or shell trace.
- On `401` or `403`, tell the user the service token is missing or invalid. Do not print the token, even truncated.
- On `502` with `error: "youtube_ip_blocked"`, suggest configuring a proxy on the backend (Webshare or Zyte SPM via the `YT_HTTPS_PROXY` env on the service).
- On `502` with `error: "whisper_failed"`, the audio could not be transcribed. Suggest retrying with `--force refresh` or check whether the video has captions available.
- On any other 4xx/5xx, surface the `error` and `message` fields from the response envelope.

## Polling behavior

- 5-minute default timeout (configurable via `YT_SERVICE_POLL_TIMEOUT`).
- 5-second poll interval.
- On `status=complete`, re-fetch `/v1/transcript?v=<id>` to get the now-cached transcript.
- On `status=failed`, surface the error envelope.
- On timeout, exit non-zero with a clear message; the job continues running on the backend and a later request will hit the cache.
