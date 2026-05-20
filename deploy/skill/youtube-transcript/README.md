# youtube-transcript skill

A Claude Code companion skill for the yt-transcript-service backend. See `SKILL.md` for the full trigger rules, quoting conventions, and error semantics. This README covers installation and environment configuration only.

## Installation

Copy this `youtube-transcript/` directory into your Claude skills path:

```bash
cp -r deploy/skill/youtube-transcript ~/.claude/skills/
```

If you use a plugin-managed skills path, copy it there instead. The directory layout (`SKILL.md` at the root, `scripts/*.py` alongside) must be preserved.

## Required environment

- `YT_SERVICE_URL` — base URL of the yt-transcript-service. Defaults to `https://yt.ericmax.com` when unset.
- `YT_SERVICE_TOKEN` — bearer token for the service. Required. The skill will exit non-zero if missing and will never print the token in any output.

## Optional environment

- `YT_SERVICE_POLL_TIMEOUT` — seconds to wait when polling a Whisper job. Defaults to `300` (5 minutes). Raise this for very long videos.

## Local development mode

To hit a locally running instance:

```bash
export YT_SERVICE_URL=http://127.0.0.1:8765
export YT_SERVICE_TOKEN=<local-token>
python scripts/fetch_transcript.py "https://youtu.be/OMhKgQmeMhI"
```

The skill artifact is identical in dev and prod; only the env vars differ.

## Phase status

- Phase 1: `fetch_transcript.py` is functional. `summarize.py` and `ingest_channel.py` are informational stubs that exit 0.
- Phase 2: `summarize.py` will call `/v1/summarize`.
- Phase 3: `ingest_channel.py` will call `/v1/ingest` for channel and playlist URLs.
